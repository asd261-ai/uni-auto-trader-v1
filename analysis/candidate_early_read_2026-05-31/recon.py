"""
READ-ONLY reconciliation: worker_history (theoretical pnl) vs trades.jsonl (real fill pnl_pts).
Join by id (epoch-ms). Classify into agree / disagree / no-match. NO production writes.
Outputs CSVs into this analysis dir only.
"""
import json, datetime, collections, csv, os

ARCH = "/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-05-31"
OUT = os.path.dirname(os.path.abspath(__file__))
WIN_START = datetime.date(2026, 5, 22)
WIN_END = datetime.date(2026, 5, 29)

def tw_dt(epoch_ms):
    return datetime.datetime.utcfromtimestamp(epoch_ms/1000) + datetime.timedelta(hours=8)

def trading_day_and_session(dt):
    t = dt.time(); cal = dt.date()
    if t < datetime.time(8, 0):
        return cal - datetime.timedelta(days=1), 'night'
    if datetime.time(8, 0) <= t <= datetime.time(14, 30):
        return cal, 'day'
    return cal, 'night'

# ---- load ----
worker = json.load(open(f"{ARCH}/worker_history_2026-05.json"))
for x in worker:
    dt = tw_dt(x['id'])
    x['_dt'] = dt
    x['_trading_day'], x['_session'] = trading_day_and_session(dt)

trades = [json.loads(l) for l in open(f"{ARCH}/trades.jsonl") if l.strip()]
mtx = [t for t in trades if t.get('source') == 'mtx']
fvg = [t for t in trades if t.get('source') == 'fvg']

# index trades.jsonl by id
trades_by_id = {}
for t in trades:
    trades_by_id.setdefault(t['id'], []).append(t)

worker_by_id = {}
for x in worker:
    worker_by_id.setdefault(x['id'], []).append(x)

def in_win(td):
    return WIN_START <= td <= WIN_END

# ---- full-file id overlap diagnostics ----
worker_ids = set(worker_by_id)
mtx_ids = {t['id'] for t in mtx}
fvg_ids = {t['id'] for t in fvg}
print("=== FULL FILE ===")
print(f"worker rows: {len(worker)}  unique ids: {len(worker_ids)}")
print(f"trades.jsonl total: {len(trades)}  mtx: {len(mtx)}  fvg: {len(fvg)}")
print(f"worker ids INT trades(all): {len(worker_ids & set(trades_by_id))}")
print(f"worker ids INT mtx: {len(worker_ids & mtx_ids)}  INT fvg: {len(worker_ids & fvg_ids)}")
print(f"worker statuses: {dict(collections.Counter(x['status'] for x in worker))}")

# ---- in-window MTX closed reconciliation ----
# worker "closed" = status not 'open' and pnl is not None
# trades.jsonl mtx in-window closed
def worker_closed(x):
    return x['status'] != 'open' and x.get('pnl') is not None

wi_closed = [x for x in worker if in_win(x['_trading_day']) and worker_closed(x)]
mtx_in = [t for t in mtx if WIN_START <= datetime.date.fromisoformat(t['trading_day']) <= WIN_END]

print(f"\n=== IN-WINDOW {WIN_START}..{WIN_END} (MTX) ===")
print(f"worker closed in-window: {len(wi_closed)}")
print(f"trades.jsonl mtx in-window: {len(mtx_in)}")

# Reconcile per id over union of in-window closed worker + in-window mtx trades
agree, disagree, nomatch = [], [], []
seen = set()

# Build in-window mtx index by id
mtx_in_by_id = {}
for t in mtx_in:
    mtx_in_by_id.setdefault(t['id'], []).append(t)

# 1) iterate worker closed in-window
for x in wi_closed:
    tid = x['id']
    matches = trades_by_id.get(tid, [])
    # prefer a real mtx fill match
    real = next((m for m in matches if m.get('source') == 'mtx'), None)
    if real is None:
        real = next((m for m in matches if m.get('source') == 'fvg'), None)
    if real is None:
        nomatch.append({'side':'worker_only','id':tid,'wpnl':x['pnl'],'sigCode':x['sigCode'],
                        'dir':x['dir'],'status':x['status'],'td':str(x['_trading_day'])})
        seen.add(('w',tid))
        continue
    delta = x['pnl'] - real['pnl_pts']
    rec = {'id':tid,'wpnl':x['pnl'],'rpnl':real['pnl_pts'],'delta':delta,
           'source':real.get('source'),'sigCode':x['sigCode'],'dir':x['dir'],
           'wstatus':x['status'],'reason':real.get('reason'),
           'wentry':x['entry'],'rentry':real['entry'],'rexit':real.get('exit'),
           'wclose':x.get('closePrice'),'td':str(x['_trading_day'])}
    if delta == 0:
        agree.append(rec)
    else:
        disagree.append(rec)
    seen.add(('w',tid)); seen.add(('t',tid))

# 2) in-window mtx trades with no worker-closed counterpart
for t in mtx_in:
    tid = t['id']
    if ('t',tid) in seen:
        continue
    # is there ANY worker row (maybe open / or out of window)?
    wrows = worker_by_id.get(tid, [])
    wstat = wrows[0]['status'] if wrows else None
    wpnl = wrows[0].get('pnl') if wrows else None
    wtd = str(wrows[0]['_trading_day']) if wrows else None
    nomatch.append({'side':'trade_only','id':tid,'rpnl':t['pnl_pts'],'source':'mtx',
                    'dir':t['dir'],'label':t.get('label'),'reason':t.get('reason'),
                    'td':t['trading_day'],'worker_status':wstat,'worker_pnl':wpnl,
                    'worker_td':wtd})

print(f"\nAGREE: {len(agree)}  DISAGREE: {len(disagree)}  NO-MATCH: {len(nomatch)}")

# ---- delta distribution ----
if disagree:
    deltas = sorted(r['delta'] for r in disagree)
    n = len(deltas)
    mean = sum(deltas)/n
    med = deltas[n//2] if n%2 else (deltas[n//2-1]+deltas[n//2])/2
    print(f"\nDISAGREE delta = wpnl - rpnl:")
    print(f"  n={n} mean={mean:.2f} median={med} min={deltas[0]} max={deltas[-1]}")
    print(f"  delta>0 (real worse): {sum(1 for d in deltas if d>0)}")
    print(f"  delta<0 (real better): {sum(1 for d in deltas if d<0)}")
    print(f"  |delta| histogram:")
    buckets = collections.Counter()
    for d in deltas:
        ad = abs(d)
        if ad<=5: buckets['0-5']+=1
        elif ad<=15: buckets['6-15']+=1
        elif ad<=30: buckets['16-30']+=1
        elif ad<=60: buckets['31-60']+=1
        else: buckets['60+']+=1
    for k in ['0-5','6-15','16-30','31-60','60+']:
        print(f"    {k}: {buckets[k]}")
    # outliers |delta|>60
    print(f"\n  OUTLIERS |delta|>60:")
    for r in sorted(disagree, key=lambda r:-abs(r['delta'])):
        if abs(r['delta'])>60:
            print(f"    id={r['id']} sig{r['sigCode']} {r['dir']} wpnl={r['wpnl']} rpnl={r['rpnl']} delta={r['delta']} reason={r['reason']} wentry={r['wentry']} rentry={r['rentry']} rexit={r['rexit']} wclose={r['wclose']} td={r['td']}")

# ---- no-match subclassing ----
print(f"\n=== NO-MATCH SUBCLASSES ===")
nm_trade = [r for r in nomatch if r['side']=='trade_only']
nm_worker = [r for r in nomatch if r['side']=='worker_only']
print(f"trade_only (in trades.jsonl, no worker-closed match): {len(nm_trade)}")
print(f"worker_only (worker closed, no trade): {len(nm_worker)}")
# trade_only breakdown by whether worker has the id at all
to_no_worker = [r for r in nm_trade if r['worker_status'] is None]
to_worker_open = [r for r in nm_trade if r['worker_status']=='open']
to_worker_other = [r for r in nm_trade if r['worker_status'] not in (None,'open')]
print(f"  trade_only & NO worker id at all: {len(to_no_worker)}")
print(f"  trade_only & worker status=open: {len(to_worker_open)}")
print(f"  trade_only & worker other status (out-of-window td?): {len(to_worker_other)}")

# ---- write CSVs ----
def wcsv(name, rows, fields):
    with open(f"{OUT}/{name}", 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in rows: w.writerow(r)

wcsv('recon_agree.csv', agree, ['id','wpnl','rpnl','delta','source','sigCode','dir','wstatus','reason','wentry','rentry','rexit','wclose','td'])
wcsv('recon_disagree.csv', sorted(disagree,key=lambda r:-abs(r['delta'])), ['id','wpnl','rpnl','delta','source','sigCode','dir','wstatus','reason','wentry','rentry','rexit','wclose','td'])
wcsv('recon_nomatch.csv', nomatch, ['side','id','wpnl','rpnl','sigCode','dir','status','label','reason','source','td','worker_status','worker_pnl','worker_td'])
print(f"\nCSVs written to {OUT}")
