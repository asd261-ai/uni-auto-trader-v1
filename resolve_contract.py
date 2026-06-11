"""
READ-ONLY 調查:登入正式環境 → 查 MXF 商品檔 + 合約檔 → 印出近月真實 prod_id。
不送任何委託。用來確認正確的下單/報價代號(取代被拒的別名 MXFG5)。
URL 讀 .env UNITRADE_URL(同 trader 本體;未設 fallback 現役 viploginb)。
"""
import os
from dotenv import load_dotenv
from unitrade.unitrade import Unitrade
from config import CONFIG

load_dotenv()
LIVE_URL = os.environ.get("UNITRADE_URL") or "https://viploginb.pfctrade.com"
BASE = "MXF"  # 小台指

api = Unitrade()
resp = api.login(LIVE_URL, CONFIG["userid"], CONFIG["password"], CONFIG["ca_path"], CONFIG["ca_password"])
print(f"[resolve] login ok={resp.ok} err={'' if resp.ok else resp.error}")
if not resp.ok:
    raise SystemExit(1)

# 商品檔(dict: code -> DomesticProduct)
try:
    prods = api.get_domestic_products()
    mxf = prods.get(BASE) if isinstance(prods, dict) else None
    print(f"[resolve] product {BASE}: {mxf}")
except Exception as e:
    print(f"[resolve] get_domestic_products error: {e}")

# 合約檔(Response with .data list of DomesticContract)
try:
    c = api.get_domestic_contracts(BASE, "F")
    ok = getattr(c, "ok", None)
    data = getattr(c, "data", None) or []
    print(f"[resolve] get_domestic_contracts ok={ok} count={len(data)}")
    for dc in data[:8]:
        print(f"[resolve]   prod_id={getattr(dc,'prod_id',None)} month={getattr(dc,'month',None)}")
    if data:
        front = data[0]
        print(f"[resolve] >>> NEAR-MONTH prod_id = {getattr(front,'prod_id',None)} (month={getattr(front,'month',None)})")
except Exception as e:
    print(f"[resolve] get_domestic_contracts error: {e}")

api.logout()
print("[resolve] done — NO ORDER SENT.")
