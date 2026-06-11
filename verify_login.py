"""
Login-only 驗證腳本:登入正式環境 → 抓帳號 → 登出。
NO ORDER SENT. 用 .env 既有帳密/憑證與 UNITRADE_URL(同 trader 本體,免雙處維護;
未設時 fallback 現役 viploginb)。輸出只印登入結果與遮罩後帳號,不印任何 secret value。
"""
import logging
import os
from dotenv import load_dotenv
from config import CONFIG
from unitrade.unitrade import Unitrade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
load_dotenv()

LIVE_URL = os.environ.get("UNITRADE_URL") or "https://viploginb.pfctrade.com"


def _mask(s: str) -> str:
    s = str(s)
    return ("*" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else s


api = Unitrade()
print(f"[verify] 嘗試登入正式環境 URL={LIVE_URL}")

resp = api.login(
    LIVE_URL,
    CONFIG["userid"],
    CONFIG["password"],
    CONFIG["ca_path"],
    CONFIG["ca_password"],
)

if not resp.ok:
    print(f"[verify] login_result=FAIL error={resp.error}")
    raise SystemExit(1)

accounts = api.get_accounts()
print(f"[verify] login_result=OK accounts_count={len(accounts)}")
for a in accounts:
    print(f"[verify]   account={_mask(a)}")

api.logout()
print("[verify] logout done — NO ORDER SENT.")
