import time
import logging
import os
from unitrade.unitrade import Unitrade, DOrderObject, DReplaceObject
import order_log
import order_guard  # process-level live-order gate (BOARD T010, 7/10 near-miss class fix)
from feed_schema import parse_broker_position, parse_fill, SCHEMA_FAIL
from order_reject import is_reject_status
from orderno_claim import OrdernoClaimer
from sdk_timeout import call_with_timeout, SDKCallTimeout

SDK_READ_TIMEOUT_SEC = float(os.getenv("SDK_READ_TIMEOUT_SEC", "5"))
# dquote auto-resubscribe: recover the tick feed in-place before the tick-stale kill
# restarts the process. Observe-first: off = log the would-fire only, on = actually
# unsubscribe+subscribe. See docs/superpowers/specs/2026-06-23-dquote-resubscribe-design.md.
DQUOTE_RESUB              = os.getenv("DQUOTE_RESUB", "off").lower() == "on"
DQUOTE_RESUB_MIN_INTERVAL = float(os.getenv("DQUOTE_RESUB_MIN_INTERVAL", "30"))  # min secs between resubscribe calls (any trigger)

logger = logging.getLogger(__name__)


class AutoTrader:
    def __init__(self, config: dict):
        self.config = config
        self.api = Unitrade()
        self.actno: str = ""
        self._running = False
        self._connected = False  # 首次連線後才為 True，用於區分首次 vs 重連
        self.strategy = None  # 由外部注入 MTXStrategy
        self._last_resub_ts = 0.0  # last dquote resubscribe attempt (min-interval guard)
        # P4-2 (2026-07-20 design): sent→reply orderno claiming distinguishes the
        # bot's own fills/rejects from Sean's manual same-product events on the
        # shared account. Modes: off = legacy | observe (default) = log
        # would-skip only | on = actually filter foreign events. Arm ladder:
        # observe ≥1 week zero false-foreign, then ask-first.
        self._orderno_filter_mode = os.getenv("FILL_ORDERNO_FILTER", "observe").strip().lower()
        self._claimer = OrdernoClaimer()

    # ── 啟動 / 停止 ──────────────────────────────────────────────

    def start(self):
        self._login()
        self._resolve_product()
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

    def _resolve_product(self):
        """Resolve the real front-month contract code (e.g. MXFF6) from the broker.

        viploginm rejects the near-month alias (e.g. MXFG5) with 商品代號錯誤; orders,
        quote subscription, and recon must use the actual contract prod_id. Picks the
        nearest (smallest-month) listed contract so rollover is automatic. Fails loud
        rather than trade a code the broker will reject.

        UNITRADE_PRODUCT (if set) FORCES the contract and skips broker min-month
        resolution. Needed at settlement rollover: the broker keeps listing the
        just-settled month, so min-month would wrongly pick the EXPIRED contract
        (2026-06-17 night kill-loop on settled MXFF6). Operator sets it to the
        correct contract (e.g. MXFG6 after June settlement); clear it to restore
        auto-resolve. Resolved before the broker query so it works even if the
        contract API is flaky."""
        override = os.getenv("UNITRADE_PRODUCT", "").strip()
        if override:
            old = self.config.get("product")
            self.config["product"] = override
            logger.info(
                f"Product OVERRIDE via UNITRADE_PRODUCT={override} (was={old}) "
                f"— skipping broker min-month resolve"
            )
            return
        base = os.getenv("UNITRADE_PRODUCT_BASE") or self.config["product"][:3]
        resp = self.api.get_domestic_contracts(base, "F")
        if not getattr(resp, "ok", False):
            raise RuntimeError(f"Contract resolve failed for {base}: {getattr(resp, 'error', '?')}")
        data = getattr(resp, "data", None) or []
        if not data:
            raise RuntimeError(f"No contracts returned for {base}")
        try:
            front = min(data, key=lambda d: int(getattr(d, "month", "999999")))
        except (ValueError, TypeError):
            front = data[0]
        code = getattr(front, "prod_id", None)
        if not code:
            raise RuntimeError(f"Front contract has no prod_id for {base}: {front}")
        old = self.config.get("product")
        self.config["product"] = code
        logger.info(f"Resolved front-month: {base} → {code} (month={getattr(front, 'month', '?')}, was={old})")

    def _register_callbacks(self):
        self.api.dquote.on_tick_data_trade = self._on_tick
        # Broker SDK (2026-06) began invoking on_tickdatabeforebidoffe (a bid/offer-pre
        # tick callback, SDK-mis-spelled like on_disonnected below) on every quote
        # message. The bot doesn't consume it, so with no handler the SDK raised
        # AttributeError on every tick → pure log spam (179×/session 2026-06-25; the
        # trade-tick feed was unaffected, last_tick_age stayed ~0s). Register a no-op to
        # silence it. The attribute name must match exactly what the SDK getattr's.
        self.api.dquote.on_tickdatabeforebidoffe = self._on_quote_noop
        self.api.dtrade.on_reply           = self._on_reply
        self.api.dtrade.on_match           = self._on_match
        self.api.dtrade.on_connected       = self._on_connected
        self.api.dtrade.on_disonnected     = self._on_disconnected  # 官方 typo，少一個 c
        self.api.on_error                  = self._on_error

    def _subscribe(self):
        ok, err = self.api.dquote.subscribe_trade_bid_offer(self.config["product"])
        if not ok:
            # Bug 2 (regression-resistant): dquote subscribe occasionally fails with
            # [Errno 11]. Strategy can still run via Worker-pull sync; tick feed is
            # optional. Do NOT raise — that leaves a zombie process (C extension keeps
            # PID alive while strategy.start() never runs).
            logger.warning(f"dquote subscribe failed (continuing without tick feed): {err}")
            return
        logger.info(f"Subscribed to {self.config['product']}")

    def resubscribe_dquote(self, reason: str) -> bool:
        """Recover the dquote tick feed by unsubscribe+subscribe.

        Invoked on a dtrade reconnect (trigger B) and by the tick-stale-driven policy
        in the poll loop (trigger A). Observe-first: DQUOTE_RESUB off -> log the
        would-fire and make NO SDK call. A min-interval guard prevents A+B overlap and
        reconnect-storm spam. Never raises (preserves the no-zombie philosophy of
        _subscribe); on failure the tick-stale kill is the backstop. Returns True iff a
        subscribe succeeded.
        """
        product = self.config["product"]
        if not DQUOTE_RESUB:
            logger.info(f"[dquote-resub would-fire] reason={reason} product={product}")
            return False
        now = time.time()
        if now - self._last_resub_ts < DQUOTE_RESUB_MIN_INTERVAL:
            return False
        self._last_resub_ts = now
        # Best-effort unsubscribe first (SDK may reject a duplicate subscribe); ignore outcome.
        try:
            call_with_timeout(self.api.dquote.unsubscribe_trade_bid_offer, product,
                              timeout=SDK_READ_TIMEOUT_SEC)
        except Exception as e:
            logger.debug(f"dquote unsubscribe (pre-resub) ignored: {e}")
        try:
            ok, err = call_with_timeout(self.api.dquote.subscribe_trade_bid_offer, product,
                                        timeout=SDK_READ_TIMEOUT_SEC)
        except Exception as e:
            logger.warning(f"[dquote-resub] subscribe call failed (reason={reason}): {e}")
            return False
        if not ok:
            logger.warning(f"[dquote-resub] subscribe not-ok (reason={reason}): {err}")
            return False
        logger.info(f"[dquote-resub] resubscribed to {product} (reason={reason})")
        return True

    # ── 事件回調 ──────────────────────────────────────────────────

    def _on_quote_noop(self, *args, **kwargs):
        """No-op for broker-SDK quote callbacks the bot does not consume (e.g. the
        SDK-added on_tickdatabeforebidoffe bid/offer-pre tick). Accepts any signature so
        the SDK never AttributeErrors; does nothing, so trade-tick handling via
        on_tick_data_trade is unchanged. (2026-06-25)"""
        pass

    def _on_tick(self, tick):
        logger.debug(f"Tick | {tick.commodityid} price={tick.matchprice} qty={tick.matchquantity} total={tick.matchtotalqty}")
        if self.strategy:
            self.strategy.on_tick(tick.matchprice)

    def _on_reply(self, reply):
        logger.info(f"Reply | {reply.productid} {reply.bs} status={reply.orderstatus} orderno={reply.orderno}")
        order_log.log_event("reply", productid=reply.productid, bs=reply.bs,
                            orderno=reply.orderno, orderstatus=reply.orderstatus)
        # P4-2: claim the orderno against our outstanding sents (mirrors offline
        # bot_ordernos). Unclaimed = foreign (Sean's manual order on the shared
        # account) — in `on` mode its reject must not trigger our rollback.
        is_ours = self._claimer.note_reply(time.time(), reply.productid, reply.bs, reply.orderno)
        if self.strategy and is_reject_status(reply.orderstatus):
            if not is_ours and self._orderno_filter_mode in ("observe", "on"):
                logger.warning(f"[orderno-filter {self._orderno_filter_mode.upper()}] "
                               f"foreign reject orderno={reply.orderno} "
                               f"{'SKIPPED' if self._orderno_filter_mode == 'on' else 'would-skip (still routed)'}")
                if self._orderno_filter_mode == "on":
                    return
            try:
                self.strategy.on_order_rejected(reply.productid, reply.bs, reply.orderstatus)
            except Exception as e:
                logger.debug(f"on_order_rejected error (non-fatal): {e}")

    def _on_match(self, match):
        # Schema gate: reject malformed fills before they reach the P&L log or the
        # fill FIFO. A bad matchprice would contaminate orders.jsonl P&L (and could
        # trip DAILY_MAX_LOSS) and, with FILL_ANCHOR, re-anchor stops to a junk price.
        bs = getattr(match, "bs", "")
        fill = parse_fill(bs, getattr(match, "matchprice", None), getattr(match, "matchqty", None))
        if fill is None:
            logger.error(
                f"FILL_REJECTED: malformed match product={getattr(match, 'productid', '?')!r} "
                f"bs={bs!r} price={getattr(match, 'matchprice', '?')!r} qty={getattr(match, 'matchqty', '?')!r}"
            )
            return
        price, qty = fill
        logger.info(f"Match | {match.productid} {bs} price={price} qty={qty} orderno={match.orderno}")
        order_log.log_event("match", productid=match.productid, bs=bs,
                            orderno=match.orderno, matchprice=price, matchqty=qty)
        # P4-2: a fill whose orderno was never claimed is a manual/foreign fill.
        # In `on` mode keep it away from on_fill's FIFO attribution (orders.jsonl
        # above still records it — pnl_calc provenance handles the ledger side).
        if not self._claimer.is_bot_fill(match.orderno) and \
                self._orderno_filter_mode in ("observe", "on"):
            logger.warning(f"[orderno-filter {self._orderno_filter_mode.upper()}] "
                           f"foreign fill orderno={match.orderno} price={price} "
                           f"{'SKIPPED' if self._orderno_filter_mode == 'on' else 'would-skip (still attributed)'}")
            if self._orderno_filter_mode == "on":
                return
        # Fill-anchoring (Plan B): let the strategy attribute this fill to a pending
        # entry/exit and (if FILL_ANCHOR) report the real entry price to the Worker.
        if self.strategy:
            try:
                self.strategy.on_fill(match.productid, bs, price)
            except Exception as e:
                logger.debug(f"on_fill error (non-fatal): {e}")

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
        # Trigger B: a dtrade reconnect means connectivity is restored — the dquote feed
        # (a separate client with no auto-resubscribe) may still be dead. Attempt a
        # resubscribe so the feed recovers without waiting for the tick-stale kill.
        self.resubscribe_dquote("dtrade-reconnect")

    def _on_disconnected(self):
        status = getattr(self.api.dtrade, "last_disconnect_status", "unknown")
        secs   = getattr(self.api.dtrade, "last_disconnect_seconds", "?")
        logger.warning(f"dtrade disconnected | status={status} | duration={secs}s")
        if self.strategy:
            self.strategy.on_disconnect()

    def _query_broker_position(self):
        """查詢券商端目前持倉，回傳 {productid, bs, qty} | None | SCHEMA_FAIL。

        None = 該商品已平倉 / 不在回傳中。SCHEMA_FAIL = SDK 物件欄位漂移
        （見 feed_schema.parse_broker_position）；呼叫端必須當「對帳暫停」處理，
        絕不可當成平倉。

        SDK signature: daccount.get_position(actno, groupid='', trader='') -> DPositionResponse
        DPositionResponse has fields (ok, error, data=[DPosition,...]).
        """
        try:
            try:
                resp = call_with_timeout(
                    self.api.daccount.get_position, self.actno,
                    timeout=SDK_READ_TIMEOUT_SEC,
                )
            except SDKCallTimeout:
                logger.error(f"broker get_position timed out after {SDK_READ_TIMEOUT_SEC}s — skipping recon cycle")
                return SCHEMA_FAIL
            if not resp or not getattr(resp, "ok", False):
                # No reliable read — NOT flat (2026-07-19 audit: returning None
                # here made recon read broker_net=0 → false lost-position alert
                # during any mid-session API outage). SCHEMA_FAIL → skip cycle.
                err = getattr(resp, "error", "unknown") if resp else "no response"
                logger.warning(f"Position query: broker not ok ({err}) — skipping recon cycle")
                return SCHEMA_FAIL
            positions = getattr(resp, "data", None) or []
            product = self.config["product"]
            for p in positions:
                r = parse_broker_position(p, product)
                if r is SCHEMA_FAIL:
                    logger.error(
                        "BROKER_SCHEMA_DRIFT: DPosition missing expected "
                        "open-position fields or returned out-of-range qty"
                    )
                    return SCHEMA_FAIL
                if r is not None:
                    return r
            return None  # 該商品不在回傳中 / 已平倉
        except Exception as e:
            logger.error(f"Position query failed: {e}")
        return SCHEMA_FAIL  # exception = no reliable read, never "flat" (2026-07-19 audit)

    def _query_broker_margin_excess(self, currency: str = "TWD"):
        """Available order-excess margin (DMargin.twdordexcess) in NT$, or None.

        This is the figure the broker checks for FUF1239 ("未沖銷部位及委託保證金
        超過使用額度") — when it drops below a new order's requirement the order
        is rejected. On a shared account, Sean's manual positions can drain it
        below what the bot needs.

        SDK signature: daccount.get_margin(actno, currency) -> DMarginResponse
        (ok, error, data=List[DMargin]). READ-ONLY: reuses the already-logged-in
        api session — never re-logs-in (a second session on the shared account
        could disturb the live trader).

        Returns None on any failure / unexpected shape (caller treats None as
        "no reliable read" and does NOT alert — fail-safe).
        """
        try:
            try:
                resp = call_with_timeout(
                    self.api.daccount.get_margin, self.actno, currency,
                    timeout=SDK_READ_TIMEOUT_SEC,
                )
            except SDKCallTimeout:
                logger.error(f"broker get_margin timed out after {SDK_READ_TIMEOUT_SEC}s — skipping margin cycle")
                return None
            if not resp or not getattr(resp, "ok", False):
                err = getattr(resp, "error", "unknown") if resp else "no response"
                logger.info(f"Margin query: broker not ok ({err})")
                return None
            data = getattr(resp, "data", None)
            if data is None:
                return None
            items = data if isinstance(data, (list, tuple)) else [data]
            for m in items:
                val = getattr(m, "twdordexcess", None)
                if val is None:
                    val = getattr(m, "ordcexcess", None)  # original-currency fallback
                if val is not None:
                    return float(val)
            return None
        except Exception as e:
            logger.debug(f"Margin query failed (silent): {e}")
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
        # Process identity gate: only the systemd service (TRADER_SERVICE=1, unit
        # file only) or an explicitly-acked human may send live orders. Degrade to
        # the existing rejection path (issend=False) so the poll loop stays alive.
        try:
            order_guard.assert_order_allowed()
        except order_guard.OrderGuardError as e:
            logger.critical(f"[order-guard] BLOCKED order send | {productid} {bs} x{qty}: {e}")
            order_log.log_event("guard_blocked", productid=productid, bs=bs, qty=qty,
                                ordertype=ordertype, price=price, opencloseflag=opencloseflag)
            return order_guard.GuardRejectedResp(str(e))
        order = DOrderObject()
        order.actno = self.actno
        order.productid = productid
        order.bs = bs
        order.ordertype = ordertype  # L:限價 M:市價 P:範圍市價
        order.price = price
        order.orderqty = qty
        # 市價(M)/範圍市價(P)在 viploginm 不允許 ROD(HHO0038:市價單不允許當日有效委託)→ 用 IOC;
        # 只有限價(L)可用 ROD。 R:ROD I:IOC F:FOK
        order.ordercondition = "R" if ordertype == "L" else "I"
        order.opencloseflag = opencloseflag  # "":自動 "0":新倉 "1":平倉
        order.dtrade = "N"
        order_log.log_event("sent", productid=productid, bs=bs, qty=qty,
                            ordertype=ordertype, price=price, opencloseflag=opencloseflag)
        resp = self.api.dtrade.order(order)
        if not resp.issend:
            logger.error(f"Order failed: {resp.errormsg}")
        else:
            # P4-2: register for orderno claiming (mirrors the 'sent' event above).
            self._claimer.note_sent(time.time(), productid, bs)
        return resp
