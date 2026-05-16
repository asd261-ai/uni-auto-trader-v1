import os
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    "url": os.getenv("UNITRADE_URL", "https://test167.pfctrade.com"),
    "userid": os.getenv("UNITRADE_USERID", ""),
    "password": os.getenv("UNITRADE_PASSWORD", ""),
    "ca_path": os.getenv("UNITRADE_CA_PATH", ""),
    "ca_password": os.getenv("UNITRADE_CA_PASSWORD", ""),
    "product": os.getenv("UNITRADE_PRODUCT", "MXFG5"),
    "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    # Health bot for server/disconnect/reconciliation/MAX-LOSS alerts.
    # Falls back to the trading bot if unset (handled in strategy.py).
    "health_telegram_token": os.getenv("HEALTH_TELEGRAM_BOT_TOKEN", ""),
    "health_telegram_chat_id": os.getenv("HEALTH_TELEGRAM_CHAT_ID", ""),
}
