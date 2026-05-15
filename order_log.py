"""Append-only audit log for outgoing broker orders and broker callbacks.

Each event is one JSON line in orders.jsonl alongside the code.
Reconciliation purpose: Trader's own truth, independent of MTX KV.
"""
import json
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

_LOG_PATH = os.path.join(os.path.dirname(__file__), "orders.jsonl")
_LOCK = threading.Lock()
_TZ = ZoneInfo("Asia/Taipei")


def log_event(event: str, **fields) -> None:
    record = {"ts": datetime.now(_TZ).isoformat(timespec="seconds"), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False)
    with _LOCK:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
