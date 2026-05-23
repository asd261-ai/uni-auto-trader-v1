"""READ-ONLY 真實成交 P&L,從 orders.jsonl 的 match 事件 FIFO 配對算出。

Additive:不碰下單流程、不碰訊號版 _trading_day_pnl_pts/_month_pnl_pts。
Restart-safe:讀持久化的 orders.jsonl(含持倉那口的進場成交價)。
快取 ~30s,避免每 3s heartbeat 都讀檔。

只配對單腳 MXF 市價成交(排除 'MXFF6/G6' 之類價差單)。每口視為 1 lot。
交易日邊界 08:45 TW;round-trip 的損益歸到「平倉時刻」所在交易日/月。
"""
import json
import os
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")
_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "orders.jsonl")
_CACHE = {"ts": 0.0, "val": None}
_CACHE_SEC = 30


def _read_matches(base: str):
    """Return {productid: [(ts, bs, price), ...]} grouped by EXACT contract.

    Grouping by productid keeps each contract month in its own FIFO queue so a
    different month (e.g. MXFG6 next to MXFF6) or a manual hand-trade can never
    cross-pair and corrupt the bot's P&L / loss-lock input.
    """
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
                if "/" in pid or not pid.startswith(base):   # single-leg of base only
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
        if pos and pos[0][0] != side:           # opposite → close
            oside, oprice = pos.pop(0)
            pnl = (price - oprice) if oside == "L" else (oprice - price)
            closed.append((ts, pnl))
        else:                                   # same side / flat → open
            pos.append((side, price))
    return closed, pos


def _trading_day_start_iso() -> str:
    now = datetime.now(_TZ)
    b = now.replace(hour=8, minute=45, second=0, microsecond=0)
    if now.time() < dtime(8, 45):
        b = b - timedelta(days=1)
    return b.isoformat(timespec="seconds")


def _compute(base: str):
    by_pid = _read_matches(base)
    closed = []
    pos_all = []  # (pid, side, price)
    for pid in sorted(by_pid):
        c, p = _fifo(by_pid[pid])        # FIFO per contract — never cross-pair months
        closed.extend(c)
        pos_all.extend((pid, s, pr) for s, pr in p)
    day_start = _trading_day_start_iso()
    month = datetime.now(_TZ).strftime("%Y-%m")
    day_pnl = sum(p for ts, p in closed if ts >= day_start)
    day_cnt = sum(1 for ts, p in closed if ts >= day_start)
    month_pnl = sum(p for ts, p in closed if ts[:7] == month)
    open_desc = ",".join(f"{pid}:{s}@{pr:.0f}" for pid, s, pr in pos_all) if pos_all else "flat"
    return {
        "real_trading_day_pnl_pts": round(day_pnl, 1),
        "real_trading_day_trades":  day_cnt,
        "real_month_pnl_pts":       round(month_pnl, 1),
        "real_open":                open_desc,
    }


def heartbeat_fields(base: str = "MXF") -> dict:
    """Cached real-fill P&L fields for the heartbeat payload. Never raises."""
    now = time.time()
    if _CACHE["val"] is None or now - _CACHE["ts"] > _CACHE_SEC:
        try:
            _CACHE["val"] = _compute(base)
        except Exception:
            _CACHE["val"] = {"real_trading_day_pnl_pts": None,
                             "real_month_pnl_pts": None, "real_open": "err"}
        _CACHE["ts"] = now
    return _CACHE["val"]
