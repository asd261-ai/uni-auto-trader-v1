"""緊急平倉腳本(通用版,close_one.py 的 superset)

用法:
    python3 flat.py BUY 1      # 以 BUY 1 口平掉 short 持倉
    python3 flat.py SELL 1     # 以 SELL 1 口平掉 long 持倉
    python3 flat.py BUY 2      # 平 2 口(例如 MTX short 1 + FVG short 1)
    python3 flat.py --query    # 唯讀:登入印出 NET=<signed net> 後離開,不下單

機器可讀輸出(Discord /flat 用):
    --query 模式印 `NET=<n>`(B=+、S=-、0=平倉;讀不到印 NET=UNKNOWN 且 exit 2)
    平倉模式收尾印 `RESID=<n>`(平完殘餘淨部位;RESID!=0 → exit 3,fail loud)

必須在 bot 停止後執行(避免 API session 衝突):
    sudo systemctl stop uni-trader.service
    cd /home/ubuntu/uni-auto-trader-v1 && .venv/bin/python flat.py BUY 1
    sudo systemctl start uni-trader.service

若忘記停 bot:Unitrade API 一帳號一 session,腳本可能搶不到 login。

設計動機:close_one.py 寫死 SELL 1,只能平 long。FVG live 後 short 持倉(④ 訊號類)
也可能要手動緊急平,所以需要可指定方向的工具。trader.py 已記錄每筆 broker reply
到 orders.jsonl,這個腳本平倉時 broker 回報會走獨立 callback,不會污染 audit log。
"""
import sys
import time
import requests
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from config import CONFIG
from flat_query import UNKNOWN, query_net
from unitrade.unitrade import Unitrade, DOrderObject

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
load_dotenv()


def send_tg(token: str, chat_id: str, text: str):
    text = f"{text}\n🕐 TW {datetime.now(timezone(timedelta(hours=8))).strftime('%m/%d %H:%M')}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


# ── CLI 參數解析 ──
query_mode = len(sys.argv) == 2 and sys.argv[1] == "--query"

if not query_mode:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    bs_arg  = sys.argv[1].upper()
    qty_arg = int(sys.argv[2])

    if bs_arg not in ("BUY", "SELL"):
        print(f"Invalid direction '{bs_arg}'. Must be BUY or SELL.")
        sys.exit(1)
    if qty_arg < 1 or qty_arg > 10:
        print(f"Invalid qty {qty_arg}. Must be 1-10 (sanity bound).")
        sys.exit(1)

    bs_code = "B" if bs_arg == "BUY" else "S"
    bs_zh   = "買入" if bs_arg == "BUY" else "賣出"

    print(f"Plan: {bs_arg} {qty_arg} {CONFIG['product']} market (opencloseflag=1)")
    confirm = input("Confirm? [y/N] ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        sys.exit(0)

# ── 連線 ──
api = Unitrade()
resp = api.login(CONFIG["url"], CONFIG["userid"], CONFIG["password"], CONFIG["ca_path"], CONFIG["ca_password"])
if not resp.ok:
    if query_mode:
        print("NET=UNKNOWN")
    print(f"Login failed: {resp.error}")
    sys.exit(2 if query_mode else 1)

actno = api.get_accounts()[0]
print(f"Logged in | account={actno}")

# ── --query:唯讀印 NET= 後離開,不下單 ──
if query_mode:
    net = query_net(api, actno, CONFIG["product"])
    print(f"NET={net}")
    api.logout()
    sys.exit(0 if net != UNKNOWN else 2)

# ── 下單 ──

replies: list = []
matches: list = []


def on_reply(r):
    replies.append(r)
    print(f"Reply | {r.productid} {r.bs} status={r.orderstatus} orderno={r.orderno}")


def on_match(m):
    matches.append(m)
    print(f"Match | {m.productid} {m.bs} price={m.matchprice} qty={m.matchqty}")


api.dtrade.on_reply = on_reply
api.dtrade.on_match = on_match

order = DOrderObject()
order.actno          = actno
order.productid      = CONFIG["product"]
order.bs             = bs_code
order.orderqty       = qty_arg
order.ordercondition = "R"     # market
order.opencloseflag  = "1"     # close
order.dtrade         = "N"

print(f"Sending {bs_arg} {qty_arg} {CONFIG['product']} market close ...")
order_resp = api.dtrade.order(order)
print(f"issend={order_resp.issend} seq={order_resp.seq} err={order_resp.errormsg}")

if not order_resp.issend:
    print("RESID=UNKNOWN")   # 沒下成單,殘餘部位未驗證 — 呼叫端必須人工檢查
    print("Order failed, exiting")
    api.logout()
    sys.exit(1)

# 等委託 + 成交回報(最多 8 秒)
for _ in range(16):
    if matches:
        break
    time.sleep(0.5)

time.sleep(1)

status      = replies[0].orderstatus if replies else "unknown"
orderno     = replies[0].orderno     if replies else "?"
match_price = matches[0].matchprice  if matches else 0.0
match_qty   = matches[0].matchqty    if matches else 0

print(f"\nResult | status={status} orderno={orderno} match_price={match_price} qty={match_qty}")

# ── 殘餘部位驗證(RESID=):用同一個 broker session 重讀,最多 10 次/約 5 秒 ──
resid = UNKNOWN
for _ in range(10):
    resid = query_net(api, actno, CONFIG["product"])
    if resid == 0:
        break
    time.sleep(0.5)
print(f"RESID={resid}")

# Telegram 通知
tg_token   = CONFIG.get("telegram_token", "")
tg_chat_id = CONFIG.get("telegram_chat_id", "")
if tg_token and tg_chat_id:
    msg = (
        f"🆘 <b>緊急手動平倉 (flat.py)</b>\n"
        f"商品:{CONFIG['product']}\n"
        f"方向:{bs_zh}(平倉)\n"
        f"口數:{qty_arg}\n"
        f"委託狀態:{status}\n"
        f"委託書號:{orderno}\n"
        f"成交價:{match_price if match_price else '待確認'}\n"
        f"成交口數:{match_qty}\n"
        f"殘餘淨部位:{resid}"
    )
    send_tg(tg_token, tg_chat_id, msg)
    print("Telegram sent")
else:
    print("No Telegram config found")

api.logout()
print("Done")
sys.exit(0 if resid == 0 else 3)   # RESID!=0 / UNKNOWN → fail loud,呼叫端必須人工檢查
