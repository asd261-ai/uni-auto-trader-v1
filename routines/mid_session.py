#!/usr/bin/env python3
"""MTX-1 mid-session audit — run by remote routine at ~21:00 TW each night."""
import json, urllib.request
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


status  = get("https://api.asd261.com/api/status")
logs    = get("https://api.asd261.com/api/logs?limit=300")
history = get("https://mtx-monitor.asd261-af5.workers.dev/api/history", auth=False)

now_tw = datetime.now(TZ_TW)
session_start = now_tw.replace(hour=15, minute=0, second=0, microsecond=0)
session_start_ms = int(session_start.timestamp() * 1000)

state        = status.get("state", "UNKNOWN")
today_trades = status.get("todayTrades", "?")
last_trade   = status.get("lastTradeAt")

if last_trade:
    try:
        lt_dt = datetime.fromisoformat(str(last_trade).replace("Z", "+00:00"))
    except Exception:
        lt_dt = datetime.fromtimestamp(int(last_trade) / 1000, tz=timezone.utc)
    staleness = int((datetime.now(timezone.utc) - lt_dt).total_seconds() / 60)
else:
    staleness = 9999

error_logs = [
    e for e in logs
    if e.get("level", "").upper() == "ERROR"
    and e.get("timestamp", "") >= session_start.isoformat()[:19]
]
error_count = len(error_logs)

worker_trades = [t for t in history if int(t.get("id", 0)) >= session_start_ms]
wt_count = len(worker_trades)

is_red = (state != "RUNNING") or (error_count > 5) or \
         (staleness > 120 and wt_count >= 3)
is_yellow = (not is_red) and (1 <= error_count <= 5)
color = "🔴 RED" if is_red else ("🟡 YELLOW" if is_yellow else "🟢 GREEN")

lines = [
    f"<b>MTX mid-session — {color}</b>",
    f"• Time (TW): {now_tw.strftime('%H:%M')}  |  Session: 15:00→05:00",
    f"• State: {state}",
    f"• todayTrades: {today_trades}",
    f"• lastTradeAt staleness: {staleness} min",
    f"• ERRORs since 15:00: {error_count}",
    f"• Worker trades since 15:00: {wt_count}",
]

if is_red or is_yellow:
    if staleness > 120 and wt_count >= 3:
        lines.append(f"⚠ Staleness {staleness}m vs {wt_count} worker signals — possible missed trades")
        for t in worker_trades[-5:]:
            lines.append(f"  [{t.get('sigLabel','')}] {t.get('dir','')} @ {t.get('entry','')} → {t.get('status','')}")
    if error_logs:
        lines.append("— Recent ERRORs —")
        for e in error_logs[-5:]:
            lines.append(f"  [{e.get('timestamp','')[:19]}] {e.get('message','')[:80]}")

report = "\n".join(lines)
print(report)
send_telegram(report)
