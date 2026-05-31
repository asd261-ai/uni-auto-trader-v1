"""
READ-ONLY segmentation for MTX candidate early-read (5/22->5/29 TW).
Source of truth: trades.jsonl real-fill pnl (pnl_pts), enriched with
atr/sessionOpen/entry from worker_history by id join (100% coverage in-window).
Writes per-candidate CSVs to this dir. NO production writes.
"""
import load, collections, datetime, csv, os, statistics

OUT = os.path.dirname(os.path.abspath(__file__))
GLYPH = {'①':1,'②':2,'③':3,'④':4,'⑤':5,'⑥':6,'⑦':7,'⑧':8}

def build():
    w = load.load_worker()
    wj = {x['id']: x for x in w}
    t = load.load_trades()
    ti = [x for x in t if load.WIN_START <= datetime.date.fromisoformat(x['trading_day']) <= load.WIN_END]
    rows = []
    for x in ti:
        wk = wj.get(x['id'], {})
        rows.append({
            'id': x['id'],
            'trading_day': x['trading_day'],
            'session': x['session'],
            'sig': GLYPH.get(x['label'][0]),
            'label': x['label'],
            'dir': x['dir'],
            'entry': x['entry'],
            'exit': x['exit'],
            'pnl_pts': x['pnl_pts'],
            'reason': x['reason'],
            'atr': wk.get('atr'),
            'sessionOpen': wk.get('sessionOpen'),
            'win': 1 if x['pnl_pts'] > 0 else 0,
        })
    return rows

def stat(rows, name):
    n = len(rows)
    if n == 0:
        return {'name': name, 'n': 0, 'wr': None, 'mean': None, 'sum': 0}
    wins = sum(r['win'] for r in rows)
    pnls = [r['pnl_pts'] for r in rows]
    return {'name': name, 'n': n, 'wr': wins/n, 'mean': statistics.mean(pnls),
            'sum': sum(pnls), 'wins': wins}

def pr(s):
    if s['n'] == 0:
        print(f"  {s['name']:42s} n=0")
        return
    thin = " [THIN]" if s['n'] < 15 else ""
    print(f"  {s['name']:42s} n={s['n']:3d}  WR={s['wr']*100:5.1f}%  mean={s['mean']:+7.1f}  sum={s['sum']:+7.0f}{thin}")

if __name__ == '__main__':
    rows = build()
    # write master enriched CSV
    with open(f"{OUT}/inwindow_trades_enriched.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader(); wr.writerows(rows)
    print(f"Master in-window enriched CSV: {len(rows)} rows -> inwindow_trades_enriched.csv\n")

    print("="*80)
    print("OVERALL in-window (real-fill, trades.jsonl):")
    pr(stat(rows, "ALL"))
    for sig in sorted(set(r['sig'] for r in rows if r['sig'])):
        pr(stat([r for r in rows if r['sig']==sig], f"sig {sig}"))
    print()

    # ---------- DELIVERABLE A: candidate #1 (2) x trend alignment ----------
    print("="*80)
    print("DELIVERABLE A: candidate #1 -- (2) x trend alignment")
    s2 = [r for r in rows if r['sig']==2]
    pr(stat(s2, "all (2) breakout"))
    # PROXY: entry vs sessionOpen. long (2) with entry<sessionOpen => entered below
    # session open => down-biased session => 'wrong-trend' long.
    # short (2) with entry>sessionOpen => 'wrong-trend' short (none expected; (2) is breakout-long usually)
    def wrongtrend(r):
        if r['sessionOpen'] is None: return None
        if r['dir']=='long':  return r['entry'] < r['sessionOpen']
        else:                 return r['entry'] > r['sessionOpen']
    for r in s2: r['_wrong'] = wrongtrend(r)
    pr(stat([r for r in s2 if r['_wrong'] is True],  "(2) WRONG-trend (proxy: vs sessionOpen)"))
    pr(stat([r for r in s2 if r['_wrong'] is False], "(2) RIGHT-trend (proxy: vs sessionOpen)"))
    print(f"  (2) dir split: {dict(collections.Counter(r['dir'] for r in s2))}")
    with open(f"{OUT}/A_sig2_trend.csv","w",newline="") as f:
        wr=csv.DictWriter(f,fieldnames=list(s2[0].keys())); wr.writeheader(); wr.writerows(s2)
    print()

    # ---------- DELIVERABLE B: 6 candidates sample triage ----------
    print("="*80)
    print("DELIVERABLE B: 6-candidate viability triage")
    # 1: (2) multi-TF -> from A
    print("[1] (2) x multi-TF trend filter")
    pr(stat(s2, "(2) total")); pr(stat([r for r in s2 if r['_wrong'] is True], "(2) wrong-trend subset"))
    # 2: (3) x day skip
    print("[2] (3) x Day-session skip")
    s3 = [r for r in rows if r['sig']==3]
    pr(stat(s3, "(3) all")); pr(stat([r for r in s3 if r['session']=='day'], "(3) DAY-session"))
    pr(stat([r for r in s3 if r['session']=='night'], "(3) night-session"))
    # 3: ATR>=100 skip
    print("[3] All x ATR>=100 skip")
    atr_known=[r for r in rows if r['atr'] is not None]
    pr(stat([r for r in atr_known if r['atr']>=100], "ATR>=100"))
    pr(stat([r for r in atr_known if r['atr']<100], "ATR<100"))
    print(f"     ATR distribution: min={min(r['atr'] for r in atr_known)} max={max(r['atr'] for r in atr_known)} "
          f"median={statistics.median(r['atr'] for r in atr_known)}")
    # 4: short x day skip
    print("[4] Short x Day-session skip")
    pr(stat([r for r in rows if r['dir']=='short' and r['session']=='day'], "short & DAY"))
    pr(stat([r for r in rows if r['dir']=='short'], "all shorts (ref)"))
    # 5: pyramid x day skip -- pyramid = 2nd same-dir unit same trading_day same session
    print("[5] Pyramid x Day-session skip")
    pyr=[]
    bykey=collections.defaultdict(list)
    for r in sorted(rows, key=lambda r:r['id']):
        bykey[(r['trading_day'], r['session'], r['dir'])].append(r)
    for k,grp in bykey.items():
        for i,r in enumerate(grp):
            r['_pyramid'] = (i>0)  # 2nd+ same-dir unit in same session/day
    pyr_all=[r for r in rows if r.get('_pyramid')]
    pr(stat(pyr_all, "pyramids (2nd+ same-dir/session)"))
    pr(stat([r for r in pyr_all if r['session']=='day'], "pyramids x DAY"))
    # 6: replace-then-skip events -- (4) night same-dir refire. worker layer needed.
    print("[6] Replace-then-skip: (4) x night x atr>58 same-dir refire (status=replaced/reversed, pre pnl>0)")
    w=load.load_worker()
    wi=[x for x in w if load.in_window(x['_trading_day'])]
    rev4=[x for x in wi if x['sigCode']==4 and x['_session']=='night' and x['status'] in ('reversed','replaced')
          and (x.get('atr') or 0)>58]
    rev4_pos=[x for x in rev4 if (x['pnl'] or 0)>0]
    print(f"     (4) night atr>58 reversed/replaced events: n={len(rev4)}; with pre-pnl>0: n={len(rev4_pos)}")

    # ---------- deployed controls sanity ----------
    print("="*80)
    print("DEPLOYED-CONTROL SANITY")
    # (4) night shorts net (the population the ATR>58 gate targets)
    s4 = [r for r in rows if r['sig']==4]
    pr(stat(s4, "(4) all"))
    pr(stat([r for r in s4 if r['dir']=='short' and r['session']=='night'], "(4) short night (gate target pop)"))
    pr(stat([r for r in s4 if r['dir']=='short' and r['session']=='night' and (r['atr'] or 0)>58], "(4) short night atr>58"))
    pr(stat([r for r in s4 if r['dir']=='short' and r['session']=='night' and (r['atr'] or 0)<=58], "(4) short night atr<=58"))

    print("\nDone. CSVs in", OUT)
