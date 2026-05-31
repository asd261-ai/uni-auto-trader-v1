"""
READ-ONLY early-read loader for MTX candidate triage (5/22->5/29 TW window).
Joins worker_history (rich signal layer: sigCode/atr/sessionOpen) to a derived
trading_day/session, and cross-checks against trades.jsonl real-fill pnl.

NO writes to production. Output CSVs land in this analysis dir only.
"""
import json, datetime, collections, csv, os

ARCH = "/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/2026-05-31"
OUT = os.path.dirname(os.path.abspath(__file__))
WIN_START = datetime.date(2026, 5, 22)
WIN_END = datetime.date(2026, 5, 29)  # inclusive (trading_day)

def tw_dt(epoch_ms):
    return datetime.datetime.utcfromtimestamp(epoch_ms/1000) + datetime.timedelta(hours=8)

def trading_day_and_session(dt):
    """
    TW futures: day session 08:45-13:45, night session 15:00-05:00(+1).
    trading_day = calendar date the session 'belongs' to.
    Night session that runs past midnight belongs to the PRIOR calendar date's
    trading day (matches trades.jsonl convention where 5/29 night logs as 5/29).
    Rule: if time-of-day < 08:00 (i.e. early-morning overnight tail), trading_day
    = previous calendar day; else trading_day = calendar day.
    Session: 08:00-14:30 -> day; otherwise -> night.
    """
    t = dt.time()
    cal = dt.date()
    # overnight tail before morning -> previous trading day, night session
    if t < datetime.time(8, 0):
        return cal - datetime.timedelta(days=1), 'night'
    if datetime.time(8, 0) <= t <= datetime.time(14, 30):
        return cal, 'day'
    return cal, 'night'

def load_worker():
    d = json.load(open(f"{ARCH}/worker_history_2026-05.json"))
    rows = []
    for x in d:
        dt = tw_dt(x['id'])
        td, sess = trading_day_and_session(dt)
        x2 = dict(x)
        x2['_dt'] = dt
        x2['_trading_day'] = td
        x2['_session'] = sess
        rows.append(x2)
    return rows

def load_trades():
    return [json.loads(l) for l in open(f"{ARCH}/trades.jsonl") if l.strip()
            and json.loads(l).get('source') == 'mtx']

def in_window(td):
    return WIN_START <= td <= WIN_END

if __name__ == '__main__':
    w = load_worker()
    t = load_trades()
    # worker session-derivation cross-check vs trades.jsonl by id
    tj = {x['id']: x for x in t}
    matched = mism = 0
    for x in w:
        if x['id'] in tj:
            tt = tj[x['id']]
            if str(x['_trading_day']) == tt['trading_day']:
                matched += 1
            else:
                mism += 1
    print(f"worker rows: {len(w)}  trades.jsonl mtx: {len(t)}")
    print(f"id-join trading_day match: {matched}  mismatch: {mism}")
    # session cross-check
    sm = ss = 0
    for x in w:
        if x['id'] in tj:
            if x['_session'] == tj[x['id']]['session']: sm += 1
            else: ss += 1
    print(f"id-join session match: {sm}  mismatch: {ss}")

    wi = [x for x in w if in_window(x['_trading_day'])]
    ti = [x for x in t if WIN_START <= datetime.date.fromisoformat(x['trading_day']) <= WIN_END]
    print(f"\nIN-WINDOW {WIN_START}..{WIN_END}")
    print(f"  worker rows in-window: {len(wi)}")
    print(f"  trades.jsonl mtx in-window: {len(ti)}")
    print(f"  worker by status: {dict(collections.Counter(x['status'] for x in wi))}")
    print(f"  worker by sigCode: {dict(sorted(collections.Counter(x['sigCode'] for x in wi).items()))}")
    print(f"  worker by day: {dict(sorted(collections.Counter(str(x['_trading_day']) for x in wi).items()))}")
