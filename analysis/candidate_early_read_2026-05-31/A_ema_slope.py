"""
Candidate #1 intended metric: 5m EMA20 slope at (2) entry.
Bars from tvapi (mtx_5m_raw.json). slope = EMA20[t] - EMA20[t-k] over k bars.
Wrong-trend (2): long entry while EMA20 slope < 0 (entering a falling 5m trend).
"""
import json, datetime, csv, os
import load

OUT = os.path.dirname(os.path.abspath(__file__))
SLOPE_LOOKBACK = 3   # bars (=15 min) for slope sign

d = json.load(open(f"{OUT}/mtx_5m_raw.json"))
ts = d['t']; cl = d['c']
# EMA20 over 5m closes
N = 20; k = 2/(N+1)
ema = [None]*len(cl)
e = cl[0]
for i,c in enumerate(cl):
    e = c if i==0 else (c*k + e*(1-k))
    ema[i] = e

def bar_idx_at(epoch_s):
    """index of the last bar whose OPEN time <= entry epoch (the bar in progress at entry)."""
    lo, hi, res = 0, len(ts)-1, -1
    for i,tt in enumerate(ts):
        if tt <= epoch_s: res = i
        else: break
    return res

# load sig2 trades
w = load.load_worker(); wj = {x['id']: x for x in w}
t = load.load_trades()
ti = [x for x in t if load.WIN_START <= datetime.date.fromisoformat(x['trading_day']) <= load.WIN_END]
GLYPH = {'①':1,'②':2,'③':3,'④':4,'⑤':5,'⑥':6,'⑦':7,'⑧':8}
s2 = [x for x in ti if GLYPH.get(x['label'][0])==2]

rows=[]
for x in s2:
    entry_epoch = x['id']/1000  # ms->s, TW-agnostic (epoch is absolute)
    bi = bar_idx_at(entry_epoch)
    if bi is None or bi < SLOPE_LOOKBACK:
        slope = None
    else:
        slope = ema[bi] - ema[bi-SLOPE_LOOKBACK]
    aligned = None
    if slope is not None:
        # long: aligned if slope>0 ; (all s2 are long)
        aligned = (slope > 0) if x['dir']=='long' else (slope < 0)
    rows.append({
        'trading_day': x['trading_day'], 'dir': x['dir'], 'entry': x['entry'],
        'pnl_pts': x['pnl_pts'], 'win': 1 if x['pnl_pts']>0 else 0,
        'ema20_slope_15m': round(slope,1) if slope is not None else None,
        'trend_aligned': aligned,
    })

with open(f"{OUT}/A_sig2_ema_slope.csv","w",newline="") as f:
    wr=csv.DictWriter(f,fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)

def seg(pred,name):
    g=[r for r in rows if pred(r)]
    if not g: print(f"  {name:28s} n=0"); return
    n=len(g); wins=sum(r['win'] for r in g); s=sum(r['pnl_pts'] for r in g)
    thin=" [THIN]" if n<15 else ""
    print(f"  {name:28s} n={n:2d}  WR={wins/n*100:5.1f}%  mean={s/n:+7.1f}  sum={s:+6.0f}{thin}")

print("(2) x 5m-EMA20-slope (15m lookback) trend alignment:")
seg(lambda r:True, "all (2)")
seg(lambda r:r['trend_aligned'] is True,  "RIGHT-trend (slope>0)")
seg(lambda r:r['trend_aligned'] is False, "WRONG-trend (slope<0)")
print("\nper-trade:")
for r in sorted(rows,key=lambda r:r['trading_day']):
    print(f"  {r['trading_day']} {r['dir']:5s} slope={str(r['ema20_slope_15m']):>7s} aligned={str(r['trend_aligned']):5s} pnl={r['pnl_pts']:+6.0f} win={r['win']}")
