import logging
import time
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Retry network blips and 429s — entry/exit messages are time-sensitive but
# losing one is bad UX. 4xx (bad token, bad chat, bad HTML) won't recover so we don't retry.
_MAX_ATTEMPTS = 3


def send(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return False

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
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
