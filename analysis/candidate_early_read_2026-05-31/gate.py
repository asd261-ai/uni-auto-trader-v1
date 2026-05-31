"""
Parameterized OOS-gate harness for the entry-filter review.

READ-ONLY. No production / trader / VPS / deploy actions. Writes CSVs only into
this analysis dir (or --out). Designed to be re-pointed at a fuller archive on
2026-06-10 (when 6/1-6/9 OOS data exists) by just changing --arch / --win.

TRUTH = real fills in <arch>/trades.jsonl, source=='mtx', perf metric = pnl_pts
(includes slippage). Worker history is joined for SIGNAL FIELDS ONLY (atr,
sigCode, sessionOpen) -- worker pnl is NEVER used for performance.

A "filter" SKIPS a subset of trades. Lift = -(sum pnl_pts of skipped subset):
skipping a net-loser is positive lift. Gate question: is the skipped subset
RELIABLY net-negative? i.e. bootstrap CI-upper of subset-mean-pnl < 0
(equivalently CI-lower of lift > 0).

Clean-recon enforced:
  - gate population = real fills in trades.jsonl only (MTX),
  - worker-only phantom rows (worker id not in real-fill set) are DROPPED,
  - MTX and FVG are evaluated SEPARATELY (default subset = mtx).

Usage (interim dry-run, this dir's defaults):
    python3 gate.py

On 2026-06-10 with fuller archive:
    python3 gate.py --arch /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-06-10 \
                    --win-start 2026-06-01 --win-end 2026-06-09 \
                    --worker worker_history_2026-06.json \
                    --out . --tag oos_6_1_to_6_9

NOTE: real fills ALREADY reflect deployed controls (HALF_SIZE ③④, ④xATR-skip).
We measure what actually traded -- do NOT double-count those controls.
"""
import argparse, json, datetime, collections, csv, os, random

CODES = '①②③④⑤⑥⑦⑧'
THIN_N = 15  # below this, label TOO THIN (n=1-day discipline)


def tw_dt(epoch_ms):
    return datetime.datetime.utcfromtimestamp(epoch_ms / 1000) + datetime.timedelta(hours=8)


def code_of(label):
    for c in CODES:
        if c in (label or ''):
            return CODES.index(c) + 1
    return None


def boot_ci_mean(vals, n=10000, lo=5, hi=95, seed=20260610):
    """Bootstrap CI of the MEAN. Returns (point_mean, ci_lo, ci_hi)."""
    if not vals:
        return (None, None, None)
    random.seed(seed)
    m = sum(vals) / len(vals)
    L = len(vals)
    means = []
    for _ in range(n):
        s = 0.0
        for _ in range(L):
            s += vals[random.randrange(L)]
        means.append(s / L)
    means.sort()
    return (m, means[int(lo / 100 * n)], means[int(hi / 100 * n)])


def lodo_sign_stability(rows):
    """Leave-one-day-out: drop each trading day, recompute subset mean & its sign.
    Returns (full_mean_sign, n_days, n_flips, detail list)."""
    if not rows:
        return (0, 0, 0, [])
    full_mean = sum(r['pnl_pts'] for r in rows) / len(rows)
    full_sign = (full_mean > 0) - (full_mean < 0)
    days = sorted(set(r['td'] for r in rows))
    flips = 0
    detail = []
    for d in days:
        kept = [r for r in rows if r['td'] != d]
        if not kept:
            detail.append((d, None, None))
            continue
        m = sum(r['pnl_pts'] for r in kept) / len(kept)
        s = (m > 0) - (m < 0)
        flipped = (s != full_sign) and (s != 0)
        if flipped:
            flips += 1
        detail.append((d, round(m, 1), 'FLIP' if flipped else 'same'))
    return (full_sign, len(days), flips, detail)


def summarize(rows, tag, boot_n):
    pnl = [r['pnl_pts'] for r in rows]
    n = len(pnl)
    if n == 0:
        return {'tag': tag, 'n': 0, 'wins': 0, 'win_rate': None, 'sum': 0,
                'mean': None, 'median': None, 'ci_lo': None, 'ci_hi': None,
                'lodo_days': 0, 'lodo_flips': 0, 'thin': True}
    s = sum(pnl)
    wins = sum(1 for p in pnl if p > 0)
    mean, lo, hi = boot_ci_mean(pnl, n=boot_n)
    sp = sorted(pnl)
    med = sp[n // 2] if n % 2 else (sp[n // 2 - 1] + sp[n // 2]) / 2
    _, ldays, lflips, _ = lodo_sign_stability(rows)
    return {'tag': tag, 'n': n, 'wins': wins, 'win_rate': round(100 * wins / n, 1),
            'sum': s, 'mean': round(mean, 1), 'median': med,
            'ci_lo': round(lo, 1), 'ci_hi': round(hi, 1),
            'lodo_days': ldays, 'lodo_flips': lflips, 'thin': n < THIN_N}


def directional_read(r):
    """DIRECTIONAL verdict template per candidate. NOT a formal gate verdict."""
    if r['n'] == 0:
        return 'NO DATA'
    if r['thin']:
        return 'TOO THIN (n<%d; n=1-day discipline -> not actionable)' % THIN_N
    ci_hi = r['ci_hi']
    # gate-relevant: skip helps iff subset reliably net-negative -> CI-upper < 0
    if ci_hi is not None and ci_hi < 0:
        base = 'trending-skip-helps (CI-upper<0: subset reliably negative)'
    elif r['mean'] is not None and r['mean'] < 0:
        base = 'trending-NO-GO (mean<0 but CI straddles 0: not reliable)'
    else:
        base = 'trending-NO-GO (mean>=0: skipping would FORGO profit)'
    # LODO fragility overlay
    if r['lodo_days'] and r['lodo_flips'] > 0:
        base += ' | LODO-FRAGILE (%d/%d day-drops flip sign -> single-day-driven)' % (
            r['lodo_flips'], r['lodo_days'])
    return base


def load(arch, worker_fname, win_start, win_end, src):
    """Return (real_fill_rows, phantom_diag). real fills enriched w/ joined atr/sigCode."""
    trades = [json.loads(l) for l in open(f"{arch}/trades.jsonl") if l.strip()]
    target = [t for t in trades if t.get('source') == src]
    real_ids = set(t['id'] for t in target)

    worker = json.load(open(f"{arch}/{worker_fname}"))
    w_by_id = {x['id']: x for x in worker}

    def inwin(td):
        return win_start <= td <= win_end

    rf = []
    for t in target:
        td = datetime.date.fromisoformat(t['trading_day'])
        if not inwin(td):
            continue
        if t.get('session') == 'break':  # settlement/break rows are not strategy fills
            continue
        wx = w_by_id.get(t['id'])
        rf.append({
            'id': t['id'], 'td': t['trading_day'], 'session': t['session'],
            'dir': t['dir'], 'code': code_of(t['label']),
            'pnl_pts': t['pnl_pts'], 'reason': t.get('reason'),
            'entry': t['entry'], 'exit': t.get('exit'),
            'atr': wx.get('atr') if wx else None,
            'sigCode': wx.get('sigCode') if wx else None,
        })

    # phantom diagnostic: worker CLOSED rows in-window whose id is NOT a real fill
    def wclosed(x):
        return x['status'] != 'open' and x.get('pnl') is not None
    phantom = []
    for x in worker:
        td, _ = (None, None)
        dt = tw_dt(x['id'])
        t = dt.time()
        cal = dt.date()
        if t < datetime.time(8, 0):
            td = cal - datetime.timedelta(days=1)
        else:
            td = cal
        if win_start <= td <= win_end and wclosed(x) and x['id'] not in real_ids:
            phantom.append(x)
    ph_cc = collections.Counter((x.get('sigCode'), x.get('dir')) for x in phantom)
    ph_34short = sum(v for k, v in ph_cc.items() if k[0] in (3, 4) and k[1] == 'short')
    phantom_diag = {
        'n_phantom': len(phantom),
        'pct_34_short': round(100 * ph_34short / max(1, len(phantom)), 0),
        'by_code_dir': dict(sorted(ph_cc.items(), key=lambda k: (k[0][0] or 0))),
    }
    return rf, phantom_diag


def subset(rf, code=None, direction=None, session=None):
    out = rf
    if code is not None:
        out = [r for r in out if r['code'] == code]
    if direction is not None:
        out = [r for r in out if r['dir'] == direction]
    if session is not None:
        out = [r for r in out if r['session'] == session]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arch', default="/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-05-31")
    ap.add_argument('--worker', default="worker_history_2026-05.json")
    ap.add_argument('--win-start', default="2026-05-22")
    ap.add_argument('--win-end', default="2026-05-29")
    ap.add_argument('--src', default="mtx", choices=['mtx', 'fvg'])
    ap.add_argument('--out', default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument('--tag', default="interim_5_22_to_5_29")
    ap.add_argument('--boot-n', type=int, default=10000)
    args = ap.parse_args()

    ws = datetime.date.fromisoformat(args.win_start)
    we = datetime.date.fromisoformat(args.win_end)
    rf, phantom = load(args.arch, args.worker, ws, we, args.src)

    print("=" * 78)
    print(f"OOS-GATE HARNESS  [DIRECTIONAL — NOT A VERDICT]  tag={args.tag}")
    print(f"  arch={args.arch}")
    print(f"  window {ws}..{we}  source={args.src}  boot_n={args.boot_n}")
    print("=" * 78)
    print(f"gate population (real fills, phantom-EXCLUDED by construction): n={len(rf)}")
    print(f"trading days in window: {sorted(set(r['td'] for r in rf))}")
    print(f"PHANTOM DIAGNOSTIC (worker-only closed, dropped from gate): "
          f"n={phantom['n_phantom']}  ({phantom['pct_34_short']:.0f}% are ③/④ short)")
    print(f"  phantom by (sigCode,dir): {phantom['by_code_dir']}")
    print()

    # ---- alternative-cut matrix: find where the bleed actually concentrates ----
    cuts = [
        ('③ x all',     dict(code=3)),
        ('③ x night',   dict(code=3, session='night')),
        ('③ x day',     dict(code=3, session='day')),
        ('short x all',  dict(direction='short')),
        ('short x night', dict(direction='short', session='night')),
        ('short x day',  dict(direction='short', session='day')),
    ]
    print("ALTERNATIVE-CUT MATRIX (where does the loss concentrate?)")
    hdr = f"  {'cut':<16} {'n':>4} {'WR%':>6} {'sum':>8} {'mean':>8} {'median':>7} {'CI90_mean':>18} {'LODO flips':>11}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    matrix_rows = []
    for name, kw in cuts:
        r = summarize(subset(rf, **kw), name, args.boot_n)
        matrix_rows.append(r)
        ci = f"[{r['ci_lo']:+.1f},{r['ci_hi']:+.1f}]" if r['mean'] is not None else "—"
        wr = f"{r['win_rate']:.0f}" if r['win_rate'] is not None else "—"
        mean = f"{r['mean']:+.1f}" if r['mean'] is not None else "—"
        med = f"{r['median']:+.0f}" if r['median'] is not None else "—"
        lodo = f"{r['lodo_flips']}/{r['lodo_days']}" if r['n'] else "—"
        thin = " THIN" if r['thin'] else ""
        print(f"  {name:<16} {r['n']:>4} {wr:>6} {r['sum']:>+8.0f} {mean:>8} {med:>7} {ci:>18} {lodo:>11}{thin}")

    # ---- the two PRIMARY candidates (the proposed cut points) ----
    print()
    print("PRIMARY CANDIDATES (the proposed SKIP filters) — DIRECTIONAL read")
    candidates = [
        ('C_3day  (③ AND day)',        dict(code=3, session='day')),
        ('C_shortday (short AND day)',  dict(direction='short', session='day')),
    ]
    cand_rows = []
    for name, kw in candidates:
        r = summarize(subset(rf, **kw), name, args.boot_n)
        cand_rows.append(r)
        print(f"\n  >>> {name}")
        if r['n'] == 0:
            print("      n=0 — no trades match in this window.")
        else:
            ci = f"[{r['ci_lo']:+.1f}, {r['ci_hi']:+.1f}]"
            print(f"      n={r['n']}  WR={r['win_rate']:.0f}%  sum={r['sum']:+.0f}  "
                  f"mean={r['mean']:+.1f}  median={r['median']:+.0f}")
            print(f"      90% bootstrap CI of mean pnl_pts: {ci}  "
                  f"(skip helps iff CI-upper<0)")
            _, ld, lf, det = lodo_sign_stability(subset(rf, **kw))
            print(f"      LODO sign-stability: {lf}/{ld} day-drops flip the sign")
            if lf:
                flip_days = [d for d, m, tagx in det if tagx == 'FLIP']
                print(f"        flip-causing days: {flip_days}")
        print(f"      EARLY READ: {directional_read(r)}")

    # ---- write CSVs (per-row detail stays on disk) ----
    os.makedirs(args.out, exist_ok=True)
    matrix_csv = f"{args.out}/gate_altcut_matrix_{args.tag}.csv"
    cand_csv = f"{args.out}/gate_candidates_{args.tag}.csv"
    rf_csv = f"{args.out}/gate_realfill_{args.tag}.csv"
    fields = ['tag', 'n', 'wins', 'win_rate', 'sum', 'mean', 'median', 'ci_lo', 'ci_hi',
              'lodo_days', 'lodo_flips', 'thin']
    with open(matrix_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in matrix_rows:
            w.writerow(r)
    with open(cand_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields + ['early_read'])
        w.writeheader()
        for r in cand_rows:
            rr = dict(r)
            rr['early_read'] = directional_read(r)
            w.writerow(rr)
    with open(rf_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'td', 'session', 'dir', 'code',
                                          'sigCode', 'atr', 'pnl_pts', 'reason', 'entry', 'exit'])
        w.writeheader()
        for r in rf:
            w.writerow(r)
    print()
    print("=" * 78)
    print("DISCIPLINE REMINDER: DIRECTIONAL only. Formal gate fires 2026-06-10 on")
    print("6/1-6/9 OOS data. Require OOS CI-lower of lift > 0 (== CI-upper of subset")
    print("mean < 0) AND LODO sign-stable before any production change.")
    print(f"wrote: {os.path.basename(matrix_csv)}, {os.path.basename(cand_csv)}, "
          f"{os.path.basename(rf_csv)}")
    print("=" * 78)


if __name__ == '__main__':
    main()
