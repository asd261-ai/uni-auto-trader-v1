"""Trader → Worker /api/trade_log fire-and-forget POST.

Cloud backup for closed-trade records. Called from MTXStrategy._record_trade
after appending to local trades.jsonl. Worker dedups by (id, reason) so ghost
duplicates from MTX KV restore-loop don't pollute the cloud copy.

Mirror pattern from heartbeat.py / signal_emit.py:
- Lazy-init env (defer until first send to dodge load_dotenv ordering)
- HMAC-SHA256 signed (BOT_AUTH_SECRET, same secret as heartbeat + signal)
- Daemon thread so main loop never blocks
- Swallows all network errors at debug level
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_URL    = None
_SECRET = None


def _init_config() -> None:
    global _URL, _SECRET
    if _URL is None:
        _URL    = os.getenv("TRADE_LOG_URL", "https://mtx-monitor.asd261-af5.workers.dev/api/trade_log")
        _SECRET = os.getenv("BOT_AUTH_SECRET", "")


def send(record: dict) -> None:
    """Fire-and-forget POST. Returns immediately; never raises.
    No-op when BOT_AUTH_SECRET unset (dev/test environments).
    """
    _init_config()
    if not _SECRET:
        return
    threading.Thread(target=_do_send, args=(record,), daemon=True).start()


def _do_send(record: dict) -> None:
    try:
        body = json.dumps(record, separators=(",", ":")).encode("utf-8")
        ts   = str(int(time.time() * 1000))
        sig  = hmac.new(_SECRET.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
        req  = urllib.request.Request(
            _URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "uni-auto-trader/trade-log",
                "X-Timestamp":  ts,
                "X-Signature":  sig,
            },
        )
        urllib.request.urlopen(req, timeout=2).read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.debug(f"trade_log POST failed (silent): {e}")
    except Exception as e:  # pragma: no cover
        logger.warning(f"trade_log unexpected error: {e}")
