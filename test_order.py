"""
單次測試腳本：送 1 口 MXFG5 市價買單，確認委託成功後立即刪單
只在測試環境執行，不影響正式帳號
"""
import time
import logging
from dotenv import load_dotenv
from config import CONFIG
from unitrade.unitrade import Unitrade, DOrderObject, DReplaceObject

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
load_dotenv()

api = Unitrade()

# 1. 登入
resp = api.login(CONFIG["url"], CONFIG["userid"], CONFIG["password"], CONFIG["ca_path"], CONFIG["ca_password"])
if not resp.ok:
    print(f"❌ 登入失敗: {resp.error}")
    exit(1)

accounts = api.get_accounts()
actno = accounts[0]
print(f"✅ 登入成功 | 帳號: {actno}")

received_replies = []

def on_reply(reply):
    received_replies.append(reply)
    print(f"📋 委託回報 | 狀態: {reply.orderstatus} | 書號: {reply.orderno} | 商品: {reply.productid} | 買賣: {reply.bs}")

def on_match(match):
    print(f"✅ 成交回報 | 商品: {match.productid} | 價格: {match.matchprice} | 口數: {match.matchqty}")

api.dtrade.on_reply = on_reply
api.dtrade.on_match = on_match

# 2. 送市價買單 1 口
order = DOrderObject()
order.actno          = actno
order.productid      = CONFIG["product"]
order.bs             = "B"
order.ordertype      = "M"   # 市價
order.price          = 0
order.orderqty       = 1
order.ordercondition = "R"   # ROD
order.opencloseflag  = ""
order.dtrade         = "N"

print(f"\n📤 送出買單 | {CONFIG['product']} 市價 x1 ...")
order_resp = api.dtrade.order(order)
print(f"   issend: {order_resp.issend} | seq: {order_resp.seq} | errormsg: {order_resp.errormsg}")

if not order_resp.issend:
    print("❌ 下單失敗，結束")
    api.logout()
    exit(1)

# 3. 等委託回報（最多 5 秒）
print("⏳ 等待委託回報...")
for _ in range(10):
    if received_replies:
        break
    time.sleep(0.5)

# 4. 如果有委託書號，送刪單
orderno = received_replies[0].orderno if received_replies else ""
if orderno:
    print(f"\n🗑  送出刪單 | 書號: {orderno}")
    cancel = DReplaceObject()
    cancel.replacetype = "4"
    cancel.actno       = actno
    cancel.orderno     = orderno
    cancel_resp = api.dtrade.replace_order(cancel)
    print(f"   issend: {cancel_resp.issend} | errormsg: {cancel_resp.errormsg}")
else:
    print("⚠️  未收到委託書號，無法刪單（可能已成交或環境問題）")

time.sleep(2)
api.logout()
print("\n完成")
