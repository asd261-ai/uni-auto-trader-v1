"""READ-ONLY drill into no-match(worker_only) and disagree mechanics."""
import json, datetime, collections, os

ARCH = "/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-05-31"
OUT = os.path.dirname(os.path.abspath(__file__))
WIN_START = datetime.date(2026, 5, 22); WIN_END = datetime.date(2026, 5, 29)

def tw_dt(ms): return datetime.datetime.utcfromtimestamp(ms/1000)+datetime.timedelta(hours=8)
def tds(dt):
    t=dt.time(); cal=dt.date()
    if t<datetime.time(8,0): return cal-datetime.timedelta(days=1),'night'
    if datetime.time(8,0)<=t<=datetime.time(14,30): return cal,'day'
    return cal,'night'

worker=json.load(open(f"{ARCH}/worker_history_2026-05.json"))
for x in worker:
    x['_dt']=tw_dt(x['id']); x['_td'],x['_sess']=tds(x['_dt'])
trades=[json.loads(l) for l in open(f"{ARCH}/trades.jsonl") if l.strip()]
mtx=[t for t in trades if t.get('source')=='mtx']
mtx_ids={t['id'] for t in mtx}
mtx_by_id={t['id']:t for t in mtx}

def inwin(td): return WIN_START<=td<=WIN_END
def wclosed(x): return x['status']!='open' and x.get('pnl') is not None

# --- worker_only no-match: worker closed in-window but id NOT a real mtx trade ---
wi_closed=[x for x in worker if inwin(x['_td']) and wclosed(x)]
worker_only=[x for x in wi_closed if x['id'] not in mtx_ids]
print(f"worker_only no-match: {len(worker_only)}")
print(f"  by status: {dict(collections.Counter(x['status'] for x in worker_only))}")
print(f"  by sigCode: {dict(sorted(collections.Counter(x['sigCode'] for x in worker_only).items()))}")
print(f"  by trading_day: {dict(sorted(collections.Counter(str(x['_td']) for x in worker_only).items()))}")
print(f"  by session: {dict(collections.Counter(x['_sess'] for x in worker_only).items())}")

# is 'reversed' the dominant status? reversed = worker flipped position, may not be a discrete trade fill
print(f"\n  worker_only status=reversed count: {sum(1 for x in worker_only if x['status']=='reversed')}")
print(f"  ALL wi_closed status=reversed: {sum(1 for x in wi_closed if x['status']=='reversed')}")
print(f"  reversed that DID match a real mtx trade: {sum(1 for x in wi_closed if x['status']=='reversed' and x['id'] in mtx_ids)}")

# Cross-tab: for each worker status, how many match a real mtx fill
print(f"\n  wi_closed status x has_real_fill:")
ct=collections.Counter()
for x in wi_closed:
    ct[(x['status'], x['id'] in mtx_ids)]+=1
for (st,hit),c in sorted(ct.items()):
    print(f"    {st:10s} real_fill={hit}: {c}")

# Are worker_only ids found in trades.jsonl at ALL (any source/any window)?
all_trade_ids={t['id'] for t in trades}
print(f"\n  worker_only ids in trades.jsonl(any): {sum(1 for x in worker_only if x['id'] in all_trade_ids)}")

# --- the 240 mtx: how many have a worker-closed-in-window row? (sample completeness) ---
mtx_in=[t for t in mtx if WIN_START<=datetime.date.fromisoformat(t['trading_day'])<=WIN_END]
wi_closed_ids={x['id'] for x in wi_closed}
print(f"\n  mtx_in-window: {len(mtx_in)}  of which in wi_closed: {sum(1 for t in mtx_in if t['id'] in wi_closed_ids)}")
# mtx_in not in wi_closed -> worker row exists but status open OR pnl None OR td differs
miss=[t for t in mtx_in if t['id'] not in wi_closed_ids]
print(f"  mtx_in NOT captured as wi_closed: {len(miss)}")
for t in miss[:20]:
    w=next((x for x in worker if x['id']==t['id']),None)
    print(f"    id={t['id']} rpnl={t['pnl_pts']} reason={t['reason']} -> worker: status={w['status'] if w else 'NONE'} pnl={w['pnl'] if w else '-'} wtd={str(w['_td']) if w else '-'} ttd={t['trading_day']}")

# --- disagree mechanic: split by reason. For non-loss, does delta correlate w/ entry slip? ---
agree=dis=0
print(f"\n=== disagree by reason ===")
rec=[]
for x in wi_closed:
    if x['id'] not in mtx_by_id: continue
    t=mtx_by_id[x['id']]
    d=x['pnl']-t['pnl_pts']
    entry_slip = (t['entry']-x['entry']) if x['dir']=='long' else (x['entry']-t['entry'])  # +ve = paid worse
    rec.append((d,x['status'],t['reason'],entry_slip,x['dir'],x.get('closePrice'),t.get('exit')))
by_reason=collections.defaultdict(list)
for d,st,rs,es,dr,wc,ex in rec: by_reason[rs].append((d,es,wc,ex))
for rs,lst in sorted(by_reason.items()):
    dz=[d for d,_,_,_ in lst]; n=len(dz)
    dnz=[d for d in dz if d!=0]
    print(f"  reason={rs:8s} n={n} agree(delta0)={n-len(dnz)} mean_delta={sum(dz)/n:.1f}")

# entry-slip vs delta for non-loss (exit-driven) trades where wclose matches rexit roughly
print(f"\n=== entry-slip attribution (exclude 'loss' where exit prices diverge) ===")
nl=[r for r in rec if r[2]!='loss' and r[0]!=0]
if nl:
    # for trail/profit, worker close ~ real exit? compute delta vs entry_slip
    es_match=sum(1 for d,st,rs,es,dr,wc,ex in nl if abs(d-es)<=2)
    print(f"  non-loss disagree n={len(nl)}; delta ~= entry_slip(±2): {es_match}")
    for d,st,rs,es,dr,wc,ex in sorted(nl,key=lambda r:-abs(r[0]))[:15]:
        print(f"    delta={d:+.0f} entry_slip={es:+.0f} reason={rs} dir={dr} wclose={wc} rexit={ex}  delta-eslip={d-es:+.0f}")
