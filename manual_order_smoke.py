"""MANUAL smoke script — sends a REAL 1-lot market buy on the LIVE account, then cancels.

⚠️ THERE IS NO TEST ENVIRONMENT. CONFIG loads the live viploginm account; running
this places a real order (2026-07-10: unittest discover imported the old
test_order.py version of this file and fired a live MXFG6 market order — only the
night-session order-type rejection HHO0038 prevented a fill).

Safety rails (both required):
  1. Named manual_* (not test_*) so unittest/pytest discovery never imports it.
  2. All side effects live under __main__ and require ORDER_SMOKE_ACK=1:
       ORDER_SMOKE_ACK=1 python3 manual_order_smoke.py
"""
import os
import time
import logging


def main():
    from dotenv import load_dotenv
    from config import CONFIG
    from unitrade.unitrade import Unitrade, DOrderObject, DReplaceObject

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    load_dotenv()

    api = Unitrade()

    # 1. 登入
    resp = api.login(CONFIG["url"], CONFIG["userid"], CONFIG["password"],
                     CONFIG["ca_path"], CONFIG["ca_password"])
    if not resp.ok:
        print(f"❌ 登入失敗: {resp.error}")
        return 1

    accounts = api.get_accounts()
    actno = accounts[0]
    print(f"✅ 登入成功 | 帳號: {actno}")

    received_replies = []

    def on_reply(reply):
        received_replies.append(reply)
        print(f"📋 委託回報 | 狀態: {reply.orderstatus} | 書號: {reply.orderno} | "
              f"商品: {reply.productid} | 買賣: {reply.bs}")

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
        return 1

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
    return 0


if __name__ == "__main__":
    if os.getenv("ORDER_SMOKE_ACK") != "1":
        print("⛔ 這會在真帳號送出真實市價單。確定要跑：ORDER_SMOKE_ACK=1 python3 manual_order_smoke.py")
        raise SystemExit(1)
    raise SystemExit(main())
