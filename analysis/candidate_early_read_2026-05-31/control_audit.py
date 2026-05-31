"""
READ-ONLY audit: do the 3 LIVE controls' justifications rest on phantom /
worker-optimistic data? Sanity-check premises on CLEAN real-fill data
(5/22->5/29, phantom-filtered: worker id not in real-fill -> dropped).

NO production writes. Output stays in this analysis dir.
"""
import json, datetime, collections, csv, os, random

ARCH = "/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-05-31"
OUT = os.path.dirname(os.path.abspath(__file__))
W0, W1 = datetime.date(2026, 5, 22), datetime.date(2026, 5, 29)
CODES = '①②③④⑤⑥⑦⑧'

def tw(ms): return datetime.datetime.utcfromtimestamp(ms/1000)+datetime.timedelta(hours=8)
def tds(dt):
    t=dt.time();cal=dt.date()
    if t<datetime.time(8,0):return cal-datetime.timedelta(days=1),'night'
    if datetime.time(8,0)<=t<=datetime.time(14,30):return cal,'day'
    return cal,'night'
def code_of(label):
    for c in CODES:
        if c in (label or ''): return CODES.index(c)+1
    return None
def inwin(td): return W0<=td<=W1

# ---- load ----
trades=[json.loads(l) for l in open(f"{ARCH}/trades.jsonl") if l.strip()]
mtx=[x for x in trades if x.get('source')=='mtx']
real_ids=set(x['id'] for x in mtx)
worker=json.load(open(f"{ARCH}/worker_history_2026-05.json"))
for x in worker:
    x['_td'],x['_sess']=tds(tw(x['id']))

# real-fill in window, enrich with code/session/atr(joined from worker)
w_by_id={x['id']:x for x in worker}
rf=[]
for x in mtx:
    td=datetime.date.fromisoformat(x['trading_day'])
    if not inwin(td): continue
    wx=w_by_id.get(x['id'])
    rf.append({
        'id':x['id'],'td':x['trading_day'],'session':x['session'],'dir':x['dir'],
        'code':code_of(x['label']),'reason':x['reason'],
        'pnl_pts':x['pnl_pts'],'entry':x['entry'],'exit':x.get('exit'),
        'atr': wx.get('atr') if wx else None,
        'w_pnl': wx.get('pnl') if wx else None,
        'w_status': wx.get('status') if wx else None,
    })
print(f"real-fill mtx in-window: {len(rf)}  (phantom already excluded: real-fill is ground truth)")

# phantom audit: worker closed rows in-window NOT in real fills
def wclosed(x): return x['status']!='open' and x.get('pnl') is not None
w_inwin=[x for x in worker if inwin(x['_td'])]
w_closed=[x for x in w_inwin if wclosed(x)]
phantom=[x for x in w_closed if x['id'] not in real_ids]
print(f"worker closed in-window: {len(w_closed)}  phantom (no real fill): {len(phantom)}")
ph_cc=collections.Counter((x['sigCode'],x['dir']) for x in phantom)
print(f"  phantom by (code,dir): {dict(sorted(ph_cc.items(),key=lambda k:(k[0][0] or 0)))}")
ph_short_34=sum(v for k,v in ph_cc.items() if k[0] in (3,4) and k[1]=='short')
print(f"  phantom ③/④ short: {ph_short_34}/{len(phantom)} = {100*ph_short_34/max(1,len(phantom)):.0f}%")

def boot_ci(vals,n=2000,lo=5,hi=95,seed=42):
    if not vals: return (None,None,None)
    random.seed(seed); m=sum(vals)/len(vals); means=[]
    for _ in range(n):
        s=[vals[random.randrange(len(vals))] for _ in vals]
        means.append(sum(s)/len(s))
    means.sort()
    return (m, means[int(lo/100*n)], means[int(hi/100*n)])

def summarize(rows,tag):
    pnl=[r['pnl_pts'] for r in rows]
    n=len(pnl); s=sum(pnl)
    wins=sum(1 for p in pnl if p>0)
    m,lo,hi=boot_ci(pnl)
    thin=' [TOO THIN]' if n<15 else ''
    line=f"{tag}: n={n}{thin} sum={s:+.0f} mean={ (m if m is not None else 0):+.1f} CI90=[{lo:+.1f},{hi:+.1f}] win={wins}/{n}" if m is not None else f"{tag}: n=0"
    print('  '+line)
    return {'tag':tag,'n':n,'sum':s,'mean':m,'ci_lo':lo,'ci_hi':hi,'wins':wins}

results=[]

print("\n=========== CONTROL 1: ④ short × ATR>58 night skip ===========")
# Real-fill that ACTUALLY traded matching the skip predicate: code4 short, night, atr>58
# (post-deploy these get skipped; pre-deploy / day-session they trade)
c4=[r for r in rf if r['code']==4 and r['dir']=='short']
results.append(summarize(c4,"all ④ short real-fill"))
c4_atr=[r for r in c4 if r['atr'] is not None]
print(f"  ④ short with atr joined: {len(c4_atr)}/{len(c4)} (rest atr unknown - id not in worker_history)")
c4_hi=[r for r in c4_atr if r['atr']>58]
c4_lo=[r for r in c4_atr if r['atr']<=58]
c4_hi_night=[r for r in c4_hi if r['session']=='night']
c4_hi_day=[r for r in c4_hi if r['session']=='day']
results.append(summarize(c4_hi,"④ short ATR>58 (all sess)"))
results.append(summarize(c4_hi_night,"④ short ATR>58 NIGHT (the skip target)"))
results.append(summarize(c4_hi_day,"④ short ATR>58 DAY (kept by refinement)"))
results.append(summarize(c4_lo,"④ short ATR<=58"))

print("\n=========== CONTROL 2: HALF_SIZE_CODES=3,4 (③④ short) ===========")
c3=[r for r in rf if r['code']==3 and r['dir']=='short']
c4all=[r for r in rf if r['code']==4 and r['dir']=='short']
results.append(summarize(c3,"③ short real-fill"))
results.append(summarize(c4all,"④ short real-fill"))
results.append(summarize(c3+c4all,"③+④ short combined"))
# contrast: ⑧ long (the known edge)
c8=[r for r in rf if r['code']==8 and r['dir']=='long']
results.append(summarize(c8,"⑧ long real-fill (edge contrast)"))

print("\n=========== CONTROL 3: pyramid tighten (pyramid add-on units) ===========")
# pyramid add-ons aren't directly labeled in trades.jsonl. Identify same-day same-dir
# stacked opens (2nd+ unit of same code/dir within a session) as pyramid proxy.
# Group real fills by (trading_day, session, code, dir), order by id(=open time).
groups=collections.defaultdict(list)
for r in sorted(rf,key=lambda r:r['id']):
    groups[(r['td'],r['session'],r['code'],r['dir'])].append(r)
firsts=[]; addons=[]
for k,v in groups.items():
    firsts.append(v[0])
    addons.extend(v[1:])
results.append(summarize(addons,"pyramid-proxy add-on units (2nd+ same grp)"))
results.append(summarize(firsts,"first-of-group units (contrast)"))
print(f"  NOTE: proxy = stacked same-day/session/code/dir opens; not exact Worker pyramid flag")

with open(f"{OUT}/control_audit_summary.csv","w",newline='') as f:
    wr=csv.DictWriter(f,fieldnames=['tag','n','sum','mean','ci_lo','ci_hi','wins']); wr.writeheader()
    for r in results: wr.writerow(r)
# dump enriched real-fill for re-run
with open(f"{OUT}/control_audit_realfill.csv","w",newline='') as f:
    wr=csv.DictWriter(f,fieldnames=['id','td','session','dir','code','reason','pnl_pts','atr','entry','exit','w_pnl','w_status']); wr.writeheader()
    for r in rf: wr.writerow(r)
print(f"\nwrote control_audit_summary.csv ({len(results)} rows) + control_audit_realfill.csv ({len(rf)} rows)")
