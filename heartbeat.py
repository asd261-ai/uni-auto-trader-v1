"""Trader → Worker heartbeat sender (HMAC-SHA256 signed).

Fire-and-forget. Called from MTXStrategy._poll_loop on every iteration so an
external watchdog routine can detect zombie / stuck trader processes via the
Worker's GET /api/heartbeat endpoint.

Design constraints:
- MUST NOT block the poll loop — actual network call runs in a daemon thread
- MUST NOT raise — any failure is logged at debug level and silently ignored
- MUST NOT depend on trader state — receives the payload, signs, sends, done
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

_URL    = os.getenv("HEARTBEAT_URL", "https://mtx-monitor.asd261-af5.workers.dev/api/heartbeat")
_SECRET = os.getenv("HEARTBEAT_SECRET", "")


def send(payload: dict) -> None:
    """Fire-and-forget HMAC-signed POST. Returns immediately; never raises.

    Without HEARTBEAT_SECRET configured, this is a no-op (lets dev/test
    environments run without watchdog plumbing).
    """
    if not _SECRET:
        return
    threading.Thread(target=_do_send, args=(payload,), daemon=True).start()


def _do_send(payload: dict) -> None:
    try:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ts   = str(int(time.time() * 1000))
        sig  = hmac.new(_SECRET.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
        req  = urllib.request.Request(
            _URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "uni-auto-trader/heartbeat",  # avoid Cloudflare 1010 on default urllib UA
                "X-Timestamp":  ts,
                "X-Signature":  sig,
            },
        )
        urllib.request.urlopen(req, timeout=2).read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.debug(f"Heartbeat send failed (silent): {e}")
    except Exception as e:  # pragma: no cover — surface unexpected paths but don't crash
        logger.warning(f"Heartbeat unexpected error: {e}")
