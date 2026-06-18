"""READ-ONLY 真實成交 P&L — 當日已實現損益以「逐精確商品 FIFO」計算,為心跳 + 每日最大虧損熔斷的 AUTHORITATIVE 來源。

【設計重點:Provenance-FIFO,2026-06-19】
計算方式:讀取 orders.jsonl,按精確 productid(如 MXFG6)各自 FIFO 配對平倉。
僅納入 bot 自身成交,透過 sent→reply(orderno)→match 事件鏈(bot_ordernos)追溯來源,
確保 manual/非 bot 單結構性排除。原因:共用帳號下 bot 下 MXFG6、Sean 手動下 MXFH6 等不同月份
合約,兩者無 `sent` 事件關聯,故天然隔離,不污染 bot P&L 或熔斷輸入。
窗口 floor = 當日 08:45 TW,阻擋跨日陳舊腿(解決 +2176/+322 誤配 bug)。
逐商品 FIFO 確保結算換倉日新舊合約不互相配對。
FIFO realized 值為主要輸出(realized_day_pts),喂入 heartbeat realPnl 及 DAILY_MAX_LOSS 熔斷。

【平行交叉驗證】
trades.jsonl 的 per-trade pnl_pts_real 加總仍保留為副線(summarize_real_pnl),
提供 real_month_pnl_pts + real_day_missing_fill。
divergence_warn() 在兩者差異且無缺 fill 時記 WARNING,協助早期偵測數據異常。

【Fail-open 設計】
orders.jsonl 無法讀取時回傳 None → 熔斷視為無資料,不鎖倉、不憑空生成數字。

【快取】
~30s 快取,以 08:45 交易日窗為 key;日界一過即失效,避免熔斷讀到前日累計 P&L。

設計文件:docs/superpowers/specs/2026-06-19-pnl-provenance-fifo-design.md
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

_log = logging.getLogger("pnl_calc")

_TZ = ZoneInfo("Asia/Taipei")


def _parse_iso(ts):
    """Parse an ISO-8601 string (e.g. '2026-06-19T02:52:40+08:00') to an aware
    datetime. Returns None on any malformed / non-string input."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _trading_day_window(td):
    """[start, end) aware-datetime bounds for a trading day's 08:45 TW window.
    A day labelled 2026-06-18 spans 2026-06-18 08:45 → 2026-06-19 08:45, so a
    night-session round-trip closing after midnight still falls inside."""
    d = datetime.strptime(td, "%Y-%m-%d").date()
    start = datetime.combine(d, dtime(8, 45), _TZ)
    return start, start + timedelta(days=1)


_LINK_WINDOW_SEC = 3  # max gap from a bot 'sent' to its 'reply' (real data: same second)


def bot_ordernos(rows):
    """Set of ordernos the bot actually sent. A 'sent' event carries no orderno;
    it is paired to the first unclaimed 'reply' (which has the orderno) within
    _LINK_WINDOW_SEC seconds sharing productid + bs. Manual/non-bot fills have no
    'sent' and are therefore excluded. See provenance design 2026-06-19."""
    sents, replies = [], []
    for r in rows:
        ev = r.get("event")
        ts = _parse_iso(r.get("ts"))
        if ts is None:
            continue
        if ev == "sent":
            sents.append((ts, r.get("productid"), r.get("bs")))
        elif ev == "reply" and r.get("orderno"):
            replies.append((ts, r.get("productid"), r.get("bs"), r.get("orderno")))
    sents.sort(key=lambda x: x[0])
    replies.sort(key=lambda x: x[0])
    claimed = set()
    bot = set()
    for sts, spid, sbs in sents:
        for i, (rts, rpid, rbs, ono) in enumerate(replies):
            if i in claimed:
                continue
            if rpid == spid and rbs == sbs and 0 <= (rts - sts).total_seconds() <= _LINK_WINDOW_SEC:
                bot.add(ono)
                claimed.add(i)
                break
    return bot


_TRADES_PATH = os.path.join(os.path.dirname(__file__), "trades.jsonl")
_STATE_PATH = os.path.join(os.path.dirname(__file__), "mtx_state.json")
_CACHE = {"ts": 0.0, "val": None, "day": None}
_CACHE_SEC = 30


def _today_trading_day() -> str:
    """Current trading day 'YYYY-MM-DD' on the 08:45 TW boundary (pre-08:45 = prior day)."""
    now = datetime.now(_TZ)
    d = now.date()
    if now.time() < dtime(8, 45):
        d = d - timedelta(days=1)
    return d.isoformat()


def summarize_real_pnl(records, today_td: str) -> dict:
    """Pure: sum each trade's own pnl_pts_real by trading day + month.

    - Only `pnl_pts_real` is summed; None (paper trade or genuinely-missing fill) is
      EXCLUDED (never falls back to the signal value, so paper/unfilled trades can
      neither inflate nor wrongly trip the loss lock) and counted in real_day_missing_fill.
    - No cross-trade pairing: a large value on another trading day can never leak into
      today (the old whole-history FIFO bug). trades.jsonl is bot-only, so manual
      shared-account trades are structurally absent.
    """
    month = today_td[:7]
    day_pnl = 0.0
    day_cnt = 0
    day_missing = 0
    month_pnl = 0.0
    for r in records:
        td = r.get("trading_day")
        if not isinstance(td, str):
            continue
        pr = r.get("pnl_pts_real")
        if td == today_td:
            if pr is None:
                day_missing += 1
            else:
                day_pnl += pr
                day_cnt += 1
        if td[:7] == month and pr is not None:
            month_pnl += pr
    return {
        "real_trading_day_pnl_pts": round(day_pnl, 1),
        "real_trading_day_trades":  day_cnt,
        "real_month_pnl_pts":       round(month_pnl, 1),
        "real_day_missing_fill":    day_missing,
    }


def _read_matches(base: str):
    """Return {productid: [(ts, bs, price), ...]} grouped by EXACT contract.

    Retained for reconcile_real_fill.py (the observe-first cross-check tool). NOTE:
    a FIFO over this whole-history stream is NOT a trustworthy live P&L source —
    accumulated unmatched legs + manual shared-account fills poison it (the 2026-06-18
    +2176/+322 bug). Live P&L now comes from summarize_real_pnl(trades.jsonl) instead.
    """
    _ORDERS_PATH = os.path.join(os.path.dirname(__file__), "orders.jsonl")
    out = {}
    if not os.path.exists(_ORDERS_PATH):
        return out
    try:
        with open(_ORDERS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event") != "match":
                    continue
                pid = r.get("productid", "") or ""
                if "/" in pid or not pid.startswith(base):
                    continue
                bs = r.get("bs")
                price = r.get("matchprice")
                if bs not in ("B", "S") or price is None:
                    continue
                qty = int(r.get("matchqty") or 1)
                for _ in range(max(1, qty)):
                    out.setdefault(pid, []).append((r.get("ts", ""), bs, float(price)))
    except Exception:
        return out
    return out


def _fifo(fills):
    pos = []      # open legs: (side 'L'/'S', entry_price)
    closed = []   # round-trips: (close_ts_iso, pnl_pts)
    for ts, bs, price in fills:
        side = "L" if bs == "B" else "S"
        if pos and pos[0][0] != side:
            oside, oprice = pos.pop(0)
            pnl = (price - oprice) if oside == "L" else (oprice - price)
            closed.append((ts, pnl))
        else:
            pos.append((side, price))
    return closed, pos


def realized_day_pts(rows, start, end):
    """(realized_points, round_trips) from bot match fills inside [start, end),
    FIFO'd per EXACT contract. Provenance (bot_ordernos) drops manual/non-bot
    fills; the window drops stale prior-day legs; per-product grouping prevents
    cross-contract pairing on a settlement-day rollover. Open legs stay open
    (unrealized, not counted)."""
    bot = bot_ordernos(rows)
    by_pid = {}
    for r in rows:
        if r.get("event") != "match" or r.get("orderno") not in bot:
            continue
        ts = _parse_iso(r.get("ts"))
        if ts is None or not (start <= ts < end):
            continue
        bs = r.get("bs")
        price = r.get("matchprice")
        if bs not in ("B", "S") or price is None:
            continue
        qty = int(r.get("matchqty") or 1)
        for _ in range(max(1, qty)):
            by_pid.setdefault(r.get("productid"), []).append((ts, bs, float(price)))
    total = 0.0
    trips = 0
    for fills in by_pid.values():
        fills.sort(key=lambda x: x[0])
        closed, _open = _fifo(fills)
        total += sum(pnl for _ts, pnl in closed)
        trips += len(closed)
    return round(total, 1), trips


def _read_orders_raw(path=None):
    """Parsed orders.jsonl rows (bad JSON lines skipped). Raises if the file is
    absent/unreadable so _compute can fail-open to None. An empty file -> []."""
    p = path or os.path.join(os.path.dirname(__file__), "orders.jsonl")
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _read_trades():
    out = []
    if not os.path.exists(_TRADES_PATH):
        return out
    try:
        with open(_TRADES_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return out
    return out


def _real_open() -> str:
    """Open-position description from mtx_state.json (the bot's tracked units) — the
    authoritative open state, replacing the old contaminated orders.jsonl FIFO leftover."""
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        units = st.get("mtx_units") or []
        if not units:
            return "flat"
        prod = st.get("product", "?")
        parts = []
        for u in units:
            d = "L" if u.get("dir") == "long" else "S"
            px = u.get("entry_fill") or u.get("entry") or "?"
            parts.append(f"{prod}:{d}@{px}")
        return ",".join(parts)
    except Exception:
        return "flat"


def _compute(base=None):
    td = _today_trading_day()
    xcheck = summarize_real_pnl(_read_trades(), td)   # per-trade sum + month + missing
    start, end = _trading_day_window(td)
    try:
        day_pnl, day_trades = realized_day_pts(_read_orders_raw(), start, end)
    except Exception:
        day_pnl, day_trades = None, None              # fail-open: breaker sees no-data
    if divergence_warn(day_pnl, xcheck["real_trading_day_pnl_pts"],
                       xcheck["real_day_missing_fill"]):
        _log.warning("PNL_DIVERGENCE: orders-FIFO=%s vs per-trade=%s (missing_fill=0) "
                     "— check provenance/data", day_pnl, xcheck["real_trading_day_pnl_pts"])
    return {
        "real_trading_day_pnl_pts": day_pnl,                              # FIFO -> breaker
        "real_trading_day_trades":  day_trades,
        "real_month_pnl_pts":       xcheck["real_month_pnl_pts"],         # display (per-trade)
        "real_day_missing_fill":    xcheck["real_day_missing_fill"],
        "real_day_pnl_pertrade":    xcheck["real_trading_day_pnl_pts"],   # visibility
        "real_open":                _real_open(),
    }


_DIVERGENCE_TOL_PTS = 1.0


def divergence_warn(fifo_pts, pertrade_pts, missing_fill):
    """True when the authoritative orders-FIFO value and the per-trade pnl_pts_real
    sum disagree by more than _DIVERGENCE_TOL_PTS with ZERO missing fills — a
    disagreement not explained by unstamped exits, so a provenance/data smell worth
    a log line. Never blocks the breaker."""
    if fifo_pts is None or pertrade_pts is None:
        return False
    if missing_fill != 0:
        return False
    return abs(fifo_pts - pertrade_pts) > _DIVERGENCE_TOL_PTS


def heartbeat_fields(base: str = "MXF") -> dict:
    """Cached real-fill P&L fields for the heartbeat payload. Never raises.

    `base` is retained for call-site compatibility but no longer filters: trades.jsonl
    is bot-only and each record is self-paired, so contract scoping is unnecessary.
    Cache keys on the 08:45 trading-day window so a stale pre-boundary value is never
    served right after the reset (would phantom-re-lock the fresh day; regression
    2026-06-02). See [[project-shared-account-margin-contention]].
    """
    now = time.time()
    day = _today_trading_day()
    if (_CACHE["val"] is None
            or now - _CACHE["ts"] > _CACHE_SEC
            or _CACHE.get("day") != day):
        try:
            _CACHE["val"] = _compute(base)
        except Exception:
            _CACHE["val"] = {"real_trading_day_pnl_pts": None,
                             "real_trading_day_trades": None,
                             "real_month_pnl_pts": None,
                             "real_open": "err",
                             "real_day_missing_fill": None,
                             "real_day_pnl_pertrade": None}
        _CACHE["ts"] = now
        _CACHE["day"] = day
    return _CACHE["val"]
