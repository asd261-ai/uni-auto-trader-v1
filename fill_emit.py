"""Trader → Worker /api/fill fire-and-forget POST (fill-anchoring, Plan B).

Reports the actual broker entry fill price for an MTX signal so the Worker can
shift that trade's stop/target by the slippage (exits relative to real entry).

Gated by FILL_ANCHOR env on the caller side (strategy only calls send() when on).
Mirror pattern from trade_log_emit.py: lazy env init, HMAC-SHA256 (BOT_AUTH_SECRET
/ HEARTBEAT_SECRET fallback), daemon thread, swallow network errors.
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
        _URL    = os.getenv("FILL_URL", "https://mtx-monitor.asd261-af5.workers.dev/api/fill")
        _SECRET = os.getenv("BOT_AUTH_SECRET") or os.getenv("HEARTBEAT_SECRET", "")


def send(record: dict) -> None:
    """Fire-and-forget POST {source, id, fill_price}. Returns immediately; never raises."""
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
                "User-Agent":   "uni-auto-trader/fill",
                "X-Timestamp":  ts,
                "X-Signature":  sig,
            },
        )
        resp = urllib.request.urlopen(req, timeout=2).read()
        logger.info(f"fill-anchor POST {record} → {resp.decode('utf-8', 'ignore')[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.debug(f"fill POST failed (silent): {e}")
    except Exception as e:  # pragma: no cover
        logger.warning(f"fill unexpected error: {e}")
