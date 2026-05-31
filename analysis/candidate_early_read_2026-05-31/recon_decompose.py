"""READ-ONLY: decompose delta = entry_slip + exit_slip + sign-quirks. Confirm no-match nature."""
import json, datetime, collections, os, csv

ARCH="/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-05-31"
OUT=os.path.dirname(os.path.abspath(__file__))
WIN_START=datetime.date(2026,5,22); WIN_END=datetime.date(2026,5,29)
def tw(ms): return datetime.datetime.utcfromtimestamp(ms/1000)+datetime.timedelta(hours=8)
def tds(dt):
    t=dt.time();cal=dt.date()
    if t<datetime.time(8,0):return cal-datetime.timedelta(days=1),'night'
    if datetime.time(8,0)<=t<=datetime.time(14,30):return cal,'day'
    return cal,'night'
worker=json.load(open(f"{ARCH}/worker_history_2026-05.json"))
for x in worker: x['_td'],_=tds(tw(x['id']))
trades=[json.loads(l) for l in open(f"{ARCH}/trades.jsonl") if l.strip()]
mtx_by_id={t['id']:t for t in trades if t.get('source')=='mtx'}
mtx_ids=set(mtx_by_id)
def inwin(td):return WIN_START<=td<=WIN_END
def wclosed(x):return x['status']!='open' and x.get('pnl') is not None
wi=[x for x in worker if inwin(x['_td']) and wclosed(x)]

# For matched pairs, compute pnl from PRICES both ways to find where divergence lives.
# worker theoretical pnl SHOULD = (close-entry) for long. real pnl_pts = (exit-entry_real).
# delta = worker_pnl - real_pnl
rows=[]
for x in wi:
    if x['id'] not in mtx_by_id: continue
    t=mtx_by_id[x['id']]
    sgn = 1 if x['dir']=='long' else -1
    wpnl=x['pnl']; rpnl=t['pnl_pts']; delta=wpnl-rpnl
    # entry slip in pnl terms: real entry worse than worker entry reduces real pnl
    entry_slip_pnl = sgn*(t['entry']-x['entry'])*(-1)  # if real entry higher for long => paid more => real pnl lower => positive delta contribution
    # simpler: contribution to delta from entry = sgn*(real_entry - worker_entry)
    entry_contrib = sgn*(t['entry']-x['entry'])
    # exit contribution: worker uses closePrice, real uses exit
    wclose=x.get('closePrice'); rexit=t.get('exit')
    exit_contrib = (sgn*(x['entry']-x['entry']))  # placeholder
    # reconstruct: worker_pnl_from_price = sgn*(wclose - wentry) when wclose present
    wpnl_price = sgn*(wclose - x['entry']) if wclose is not None else None
    rpnl_price = sgn*(rexit - t['entry']) if rexit is not None else None
    rows.append({'id':x['id'],'sig':x['sigCode'],'dir':x['dir'],'reason':t['reason'],'wstatus':x['status'],
                 'wpnl':wpnl,'rpnl':rpnl,'delta':delta,'entry_contrib':entry_contrib,
                 'wentry':x['entry'],'rentry':t['entry'],'wclose':wclose,'rexit':rexit,
                 'wpnl_price':wpnl_price,'rpnl_price':rpnl_price,
                 'wpnl_matches_price': (wpnl_price==wpnl) if wpnl_price is not None else None,
                 'rpnl_matches_price': (rpnl_price==rpnl) if rpnl_price is not None else None})

# Does worker pnl == price-derived worker pnl? (tests if worker pnl is consistent w/ its own closePrice)
wp=[r for r in rows if r['wpnl_matches_price'] is not None]
print(f"worker pnl == sgn*(wclose-wentry): {sum(1 for r in wp if r['wpnl_matches_price'])}/{len(wp)}")
rp=[r for r in rows if r['rpnl_matches_price'] is not None]
print(f"real pnl_pts == sgn*(rexit-rentry): {sum(1 for r in rp if r['rpnl_matches_price'])}/{len(rp)}")

# delta decomposition where both prices present
both=[r for r in rows if r['wpnl_price'] is not None and r['rpnl_price'] is not None]
print(f"\nboth prices present: {len(both)}")
ec=sum(1 for r in both if r['delta']==r['entry_contrib'])
print(f"  delta fully explained by entry slip alone (exit identical): {ec}")
# residual = delta - entry_contrib = exit-driven divergence
for r in both: r['exit_resid']=r['delta']-r['entry_contrib']
nz_exit=[r for r in both if r['exit_resid']!=0]
print(f"  rows with nonzero exit residual: {len(nz_exit)}")
if nz_exit:
    resids=[r['exit_resid'] for r in nz_exit]
    print(f"   exit_resid mean={sum(resids)/len(resids):.1f} min={min(resids)} max={max(resids)}")

# entry slip stats (the known leakage)
ecs=[r['entry_contrib'] for r in rows]
nzec=[e for e in ecs if e!=0]
print(f"\nentry_contrib (real vs worker entry, signed pnl impact):")
print(f"  n={len(ecs)} nonzero={len(nzec)} mean={sum(ecs)/len(ecs):.2f} mean_nonzero={sum(nzec)/len(nzec) if nzec else 0:.2f}")
print(f"  negative (real entry worse=>drags real pnl)... wait sign check:")
# entry_contrib = sgn*(rentry-wentry). For long, rentry>wentry => paid more => worse => real pnl LOWER => delta should be +.
# entry_contrib for long = (rentry-wentry) >0 ; and that ADDS to delta. consistent.
print(f"  entry_contrib>0 (worker pnl > real from entry): {sum(1 for e in ecs if e>0)}")
print(f"  entry_contrib<0: {sum(1 for e in ecs if e<0)}")
print(f"  entry_contrib==0 (same fill): {sum(1 for e in ecs if e==0)}")

# THE LOSS-REASON QUIRK: worker 'loss'/sign — check the big +271 outlier mechanics
print(f"\n=== loss-reason rows where wclose vs rexit differ wildly ===")
for r in sorted(rows,key=lambda r:-abs(r['delta']))[:10]:
    print(f"  id={r['id']} sig{r['sig']} {r['dir']} reason={r['reason']} wstatus={r['wstatus']} "
          f"wpnl={r['wpnl']} rpnl={r['rpnl']} delta={r['delta']:+.0f} | wentry={r['wentry']} rentry={r['rentry']} "
          f"wclose={r['wclose']} rexit={r['rexit']} | wpnl_price={r['wpnl_price']} rpnl_price={r['rpnl_price']}")

# write decomposition
with open(f"{OUT}/recon_decompose.csv",'w',newline='') as f:
    fld=['id','sig','dir','reason','wstatus','wpnl','rpnl','delta','entry_contrib','exit_resid','wentry','rentry','wclose','rexit','wpnl_price','rpnl_price','wpnl_matches_price','rpnl_matches_price']
    w=csv.DictWriter(f,fieldnames=fld,extrasaction='ignore'); w.writeheader()
    for r in sorted(rows,key=lambda r:-abs(r['delta'])): w.writerow(r)
print(f"\nwrote recon_decompose.csv ({len(rows)} matched pairs)")
