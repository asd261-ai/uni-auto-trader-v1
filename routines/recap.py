#!/usr/bin/env python3
"""MTX-1 night session recap — run by remote routine at ~05:30 TW each morning.

Data sources:
- Worker /api/history: source-of-truth signal list with P&L
- asd261 /api/logs: BROKER SDK logs (Order: BUY/SELL MXFG5 [open|close], errors)
  Note: strategy.py's `Unit closed` lines are NOT in this feed — only broker activity.
- asd261 /api/status: bot state + todayTrades counter

Trader-side comparison uses broker Order: events as a proxy, since we can't
see strategy.py's per-trade P&L from outside the VPS. The recap verifies:
- Did trader place an order for every worker signal? (count check)
- Are there any broker-side errors during the session?

The deeper P&L comparison and bug-fix verification would need either:
  (a) status_api.py on the VPS to also expose strategy.py log lines, or
  (b) a separate trader Telegram channel we can scrape via Bot API.
For now, this script gives a coarse "did the trader respond to signals" check.
"""
import json, re, urllib.request
from datetime import datetime, timezone, timedelta

TOKEN = "Rkt9-TxQ4otAqM0TRKMtC4gSkJn7w4hy"
TZ_TW = timezone(timedelta(hours=8))
BOT   = "8886951190:AAGQ8fcMlni_JDJBzAzxdStFdEzlX17frs4"
CHAT  = 6233009339


def get(url, auth=True):
    h = {"Accept": "application/json", "User-Agent": "mtx-audit/1.0"}
    if auth:
        h["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def send_telegram(text, parse_mode="HTML"):
    payload = {"chat_id": CHAT, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not resp.get("ok"):
            raise ValueError(resp)
        print("Telegram OK")
    except Exception as e:
        if parse_mode:
            print(f"Retrying without parse_mode: {e}")
            send_telegram(text, parse_mode=None)
        else:
            raise


# Window: yesterday 15:00 TW → today 05:00 TW
now_tw = datetime.now(TZ_TW)
session_end = now_tw.replace(hour=5, minute=0, second=0, microsecond=0)
session_start = session_end - timedelta(hours=14)
session_start_ms = int(session_start.timestamp() * 1000)
session_end_ms   = int(session_end.timestamp() * 1000)
sw_iso = session_start.isoformat()[:19]
ew_iso = session_end.isoformat()[:19]

logs    = get("https://api.asd261.com/api/logs?limit=500")
history = get("https://mtx-monitor.asd261-af5.workers.dev/api/history", auth=False)

# Check if logs API actually covers our session window. /api/logs returns
# the most recent N entries — at typical noise levels (~150/hr), 500 entries
# only goes back ~3h, far less than the 14h session window.
oldest_log_ts = logs[-1].get("timestamp", "") if logs else ""
logs_cover_session = oldest_log_ts and oldest_log_ts <= sw_iso

# ── Worker side ─────────────────────────────────────────────────────────────
worker_trades = sorted(
    [t for t in history if isinstance(t, dict)
     and session_start_ms <= int(t.get("id", 0)) < session_end_ms],
    key=lambda t: int(t["id"]),
)
closed_status = {"profit", "loss", "trail", "reversed"}
worker_closed = [t for t in worker_trades if t.get("status") in closed_status]
worker_pnl = sum(int(t.get("pnl") or 0) for t in worker_closed)

# ── Trader side (broker proxy) ──────────────────────────────────────────────
# Match: "Order: BUY MXFG5 ×1 [open]" or "Order: SELL MXFG5 ×1 [close]"
order_re = re.compile(r"Order:\s+(BUY|SELL)\s+\S+\s+\S+\s+\[(open|close)\]")
trader_opens, trader_closes = [], []
for entry in logs:
    msg = entry.get("message", "")
    ts  = entry.get("timestamp", "")
    if ts < sw_iso or ts >= ew_iso:
        continue
    m = order_re.search(msg)
    if not m:
        continue
    side, kind = m.group(1), m.group(2)
    rec = {"ts": ts, "side": side}
    if kind == "open":
        trader_opens.append(rec)
    else:
        trader_closes.append(rec)

# ── ERRORs ──────────────────────────────────────────────────────────────────
error_logs = [
    e for e in logs
    if e.get("level", "").upper() == "ERROR"
    and sw_iso <= e.get("timestamp", "") < ew_iso
]

# ── Verdict ─────────────────────────────────────────────────────────────────
open_count_diff  = abs(len(worker_trades) - len(trader_opens))
close_count_diff = abs(len(worker_closed) - len(trader_closes))

if not logs_cover_session:
    # Trader-side data is incomplete — can only judge from worker + ERRORs
    if len(error_logs) > 10:
        verdict = "⚠️ many errors (trader-side counts incomplete)"
    elif len(error_logs) > 0:
        verdict = "⚠️ some errors (trader-side counts incomplete)"
    else:
        verdict = "ℹ️ worker summary only (trader-side counts incomplete)"
elif open_count_diff >= 3 or close_count_diff >= 3 or len(error_logs) > 10:
    verdict = "🚨 trader desync OR many errors"
elif open_count_diff >= 1 or close_count_diff >= 1 or len(error_logs) > 0:
    verdict = "⚠️ small drift / some errors"
else:
    verdict = "✅ broker activity matches signals"

# ── Report ──────────────────────────────────────────────────────────────────
lines = [
    f"<b>MTX night recap — worker {worker_pnl:+}pt — {verdict}</b>",
    f"• Window: {session_start.strftime('%m-%d %H:%M')} → {session_end.strftime('%m-%d %H:%M')} TW",
    f"• Worker: {len(worker_trades)} signals ({len(worker_closed)} closed, {worker_pnl:+}pt)",
    f"• Trader broker orders (window): {len(trader_opens)} opens / {len(trader_closes)} closes"
    + ("" if logs_cover_session else f"  ⚠ logs only cover from {oldest_log_ts[:16] or '?'}"),
    f"• ERRORs (visible window): {len(error_logs)}",
    f"• Note: /api/logs has limited depth; per-trade P&L not exposed",
]

if worker_closed:
    lines.append("\n— Worker trades —")
    for t in worker_closed[:25]:
        lbl = t.get("sigLabel", "?")
        d   = "多" if t.get("dir") == "long" else "空"
        pnl = t.get("pnl") or 0
        sign = "+" if pnl >= 0 else ""
        lines.append(f"  {lbl} {d} @{t.get('entry')} → {t.get('status')} {sign}{pnl}pt")
    if len(worker_closed) > 25:
        lines.append(f"  …{len(worker_closed)-25} more")

if error_logs:
    lines.append("\n— ERRORs —")
    for e in error_logs[:10]:
        lines.append(f"  [{e.get('timestamp', '')[11:19]}] {e.get('message', '')[:80]}")

report = "\n".join(lines)
print(report)

if len(report) > 3800:
    midpoint = len(lines) // 2
    send_telegram("\n".join(lines[:midpoint]))
    send_telegram("\n".join(lines[midpoint:]))
else:
    send_telegram(report)
