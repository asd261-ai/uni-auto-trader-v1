import logging
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Taiwan has no DST, so a fixed +8 offset is exact and avoids any tzdata
# dependency inside the deploy container. Sean reads alerts across time zones —
# every message carries the market's clock.
_TW_TZ = timezone(timedelta(hours=8))


def _tw_stamp() -> str:
    return datetime.now(_TW_TZ).strftime("%m/%d %H:%M")

# Retry network blips and 429s — entry/exit messages are time-sensitive but
# losing one is bad UX. 4xx (bad token, bad chat, bad HTML) won't recover so we don't retry.
_MAX_ATTEMPTS = 3


def send(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return False

    text = f"{text}\n🕐 TW {_tw_stamp()}"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code == 429:
                retry_after = _capped_retry_after(resp.headers.get("Retry-After"))
                logger.warning(f"Telegram 429 (attempt {attempt}) — sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            if 400 <= resp.status_code < 500:
                logger.error(f"Telegram {resp.status_code} (no retry): {resp.text[:200]}")
                return False
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning(f"Telegram send failed (attempt {attempt}/{_MAX_ATTEMPTS}): {e}")
            if attempt < _MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))   # 1s, 2s

    logger.error("Telegram send failed after all retries — message dropped")
    return False


# 2026-07-19 audit: Telegram flood-waits can be minutes; several strategy call
# sites send synchronously on the poll thread (which owns exit checks), so an
# uncapped time.sleep(Retry-After) froze exits for the whole flood-wait.
_RETRY_AFTER_CAP_SEC = 10


def _capped_retry_after(header_val) -> int:
    try:
        return min(int(header_val), _RETRY_AFTER_CAP_SEC)
    except (TypeError, ValueError):
        return 5
