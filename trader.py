import time
import logging
from unitrade.unitrade import Unitrade, DOrderObject, DReplaceObject

logger = logging.getLogger(__name__)


class AutoTrader:
    def __init__(self, config: dict):
        self.config = config
        self.api = Unitrade()
        self.actno: str = ""
        self._running = False
        self._connected = False  # 首次連線後才為 True，用於區分首次 vs 重連
        self.strategy = None  # 由外部注入 MTXStrategy

    # ── 啟動 / 停止 ──────────────────────────────────────────────

    def start(self):
        self._login()
        self._register_callbacks()
        self._subscribe()
        self._running = True
        logger.info(f"AutoTrader started | product={self.config['product']} | account={self.actno}")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self.api.dquote.unsubscribe_trade_bid_offer(self.config["product"])
        self.api.logout()
        logger.info("AutoTrader stopped")

    # ── 初始化步驟 ────────────────────────────────────────────────

    def _login(self):
        resp = self.api.login(
            self.config["url"],
            self.config["userid"],
            self.config["password"],
            self.config["ca_path"],
            self.config["ca_password"],
        )
        if not resp.ok:
            raise RuntimeError(f"Login failed: {resp.error}")

        accounts = self.api.get_accounts()
        if not accounts:
            raise RuntimeError("No accounts found after login")
        self.actno = accounts[0]
        logger.info(f"Logged in | account={self.actno}")

    def _register_callbacks(self):
        self.api.dquote.on_tick_data_trade = self._on_tick
        self.api.dtrade.on_reply           = self._on_reply
        self.api.dtrade.on_match           = self._on_match
        self.api.dtrade.on_connected       = self._on_connected
        self.api.dtrade.on_disonnected     = self._on_disconnected  # 官方 typo，少一個 c
        self.api.on_error                  = self._on_error

    def _subscribe(self):
        ok, err = self.api.dquote.subscribe_trade_bid_offer(self.config["product"])
        if not ok:
            raise RuntimeError(f"Subscribe failed: {err}")
        logger.info(f"Subscribed to {self.config['product']}")

    # ── 事件回調 ──────────────────────────────────────────────────

    def _on_tick(self, tick):
        logger.debug(f"Tick | {tick.commodityid} price={tick.matchprice} qty={tick.matchquantity} total={tick.matchtotalqty}")
        if self.strategy:
            self.strategy.on_tick(tick.matchprice)

    def _on_reply(self, reply):
        logger.info(f"Reply | {reply.productid} {reply.bs} status={reply.orderstatus} orderno={reply.orderno}")

    def _on_match(self, match):
        logger.info(f"Match | {match.productid} {match.bs} price={match.matchprice} qty={match.matchqty} orderno={match.orderno}")

    def _on_error(self, error):
        logger.error(f"API error: {error}")

    def _on_connected(self):
        if not self._connected:
            # 首次連線，記錄後跳過
            self._connected = True
            logger.info("dtrade connected (initial)")
            return
        # 重連
        logger.warning("dtrade reconnected — querying broker position")
        broker_pos = self._query_broker_position()
        if self.strategy:
            self.strategy.on_reconnect(broker_pos)

    def _on_disconnected(self):
        status = getattr(self.api.dtrade, "last_disconnect_status", "unknown")
        secs   = getattr(self.api.dtrade, "last_disconnect_seconds", "?")
        logger.warning(f"dtrade disconnected | status={status} | duration={secs}s")
        if self.strategy:
            self.strategy.on_disconnect()

    def _query_broker_position(self) -> dict | None:
        """查詢券商端目前持倉，回傳 {productid, bs, qty} 或 None。"""
        try:
            self.api.daccount.start()
            positions = self.api.daccount.get_position()
            if not positions:
                return None
            # 只取 MXFG5（或設定中的 product）
            product = self.config["product"]
            for p in positions:
                if getattr(p, "productid", "") == product:
                    return {
                        "productid": p.productid,
                        "bs":        getattr(p, "bs", ""),
                        "qty":       getattr(p, "qty", 0),
                    }
        except Exception as e:
            logger.error(f"Position query failed: {e}")
        return None

    # ── 下單工具 ──────────────────────────────────────────────────

    def buy(self, productid: str, qty: int, ordertype: str = "M", price: float = 0, opencloseflag: str = ""):
        return self._send_order(productid, "B", qty, ordertype, price, opencloseflag)

    def sell(self, productid: str, qty: int, ordertype: str = "M", price: float = 0, opencloseflag: str = ""):
        return self._send_order(productid, "S", qty, ordertype, price, opencloseflag)

    def cancel(self, orderno: str):
        obj = DReplaceObject()
        obj.replacetype = "4"
        obj.actno = self.actno
        obj.orderno = orderno
        resp = self.api.dtrade.replace_order(obj)
        if not resp.issend:
            logger.error(f"Cancel failed: {resp.errormsg}")
        return resp

    def _send_order(self, productid: str, bs: str, qty: int, ordertype: str, price: float, opencloseflag: str = ""):
        order = DOrderObject()
        order.actno = self.actno
        order.productid = productid
        order.bs = bs
        order.ordertype = ordertype  # L:限價 M:市價 P:範圍市價
        order.price = price
        order.orderqty = qty
        order.ordercondition = "R"   # R:ROD I:IOC F:FOK
        order.opencloseflag = opencloseflag  # "":自動 "0":新倉 "1":平倉
        order.dtrade = "N"
        resp = self.api.dtrade.order(order)
        if not resp.issend:
            logger.error(f"Order failed: {resp.errormsg}")
        return resp
