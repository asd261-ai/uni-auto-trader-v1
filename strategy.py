import threading
import time
import logging
from typing import Optional
import requests

import telegram_notify as tg

logger = logging.getLogger(__name__)

HISTORY_URL = "https://mtx-monitor.asd261-af5.workers.dev/api/history"
POLL_INTERVAL = 15  # seconds

ENTRY_EMOJI = {"long": "🟢", "short": "🔴"}
EXIT_EMOJI  = {"profit": "✅", "loss": "❌", "reversed": "🔄"}


class MTXStrategy:
    """
    Polls MTX-1 Monitor /api/history and executes trades via AutoTrader.
    - Detects new 'open' trades from the Monitor
    - Enters position via Unitrade API
    - Monitors stop/target on each tick for fast exit
    """

    def __init__(self, trader, dry_run: bool = True):
        self.trader = trader
        self.dry_run = dry_run
        self._tg_token = trader.config.get("telegram_token", "")
        self._tg_chat = trader.config.get("telegram_chat_id", "")

        # Position state (protected by lock)
        self._lock = threading.Lock()
        self._position = None       # None | "long" | "short"
        self._entry_price = None
        self._stop = None
        self._target = None
        self._trade_id = None       # Monitor trade ID currently tracked

        self._last_seen_id = None   # last /api/history[0].id we processed
        self._running = False

        if dry_run:
            logger.info("[DRY RUN] Strategy in simulation mode — no real orders")

    # ── 啟動 / 停止 ──────────────────────────────────────────────

    def start(self):
        # Seed last_seen_id so we don't re-enter existing open trades on startup
        try:
            latest = self._fetch_latest_trade()
            if latest:
                self._last_seen_id = latest["id"]
                logger.info(f"Startup: latest trade id={latest['id']} status={latest.get('status')} — skipping")
        except Exception as e:
            logger.warning(f"Startup fetch failed: {e}")

        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info(f"MTXStrategy started | dry_run={self.dry_run} | poll={POLL_INTERVAL}s")

    def stop(self):
        self._running = False

    # ── 行情 tick 回調（由 AutoTrader._on_tick 呼叫）────────────

    def on_tick(self, price: float):
        with self._lock:
            if self._position is None:
                return
            self._check_exit(price)

    # ── 內部：poll 迴圈 ───────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                self._check_new_signal()
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def _check_new_signal(self):
        trade = self._fetch_latest_trade()
        if not trade:
            return

        trade_id = trade["id"]
        status = trade.get("status", "")
        direction = trade.get("dir", "")

        # Only act on brand-new open trades
        if trade_id == self._last_seen_id:
            return
        if status != "open":
            self._last_seen_id = trade_id
            return

        logger.info(f"New open trade | id={trade_id} dir={direction} "
                    f"entry={trade.get('entry')} stop={trade.get('stop')} target={trade.get('target')} "
                    f"label={trade.get('sigLabel')}")

        self._last_seen_id = trade_id
        self._enter(trade)

    def _enter(self, trade: dict):
        direction = trade.get("dir")
        product = self.trader.config["product"]
        stop = trade.get("stop")
        target = trade.get("target")
        entry = trade.get("entry")

        with self._lock:
            # Close opposite position first
            if self._position == "long" and direction == "short":
                self._close("reversed")
            elif self._position == "short" and direction == "long":
                self._close("reversed")

            if direction == "long":
                self._execute_order("BUY", product, 1)
                self._position = "long"
            elif direction == "short":
                self._execute_order("SELL", product, 1)
                self._position = "short"
            else:
                logger.warning(f"Unknown direction: {direction}")
                return

            self._entry_price = entry
            self._stop = stop
            self._target = target
            self._trade_id = trade["id"]
            logger.info(f"Position opened | {self._position} entry={entry} stop={stop} target={target}")

            label = trade.get("sigLabel", "")
            emoji = ENTRY_EMOJI.get(self._position, "📌")
            dry_tag = " [DRY RUN]" if self.dry_run else ""
            self._notify(
                f"{emoji} <b>進場{dry_tag}</b>\n"
                f"信號：{label}\n"
                f"方向：{'多' if self._position == 'long' else '空'}\n"
                f"進場：{entry}　停損：{stop}　停利：{target}"
            )

    def _check_exit(self, price: float):
        if self._position == "long":
            if self._stop and price <= self._stop:
                logger.info(f"Stop hit | price={price} stop={self._stop}")
                self._close("loss")
            elif self._target and price >= self._target:
                logger.info(f"Target hit | price={price} target={self._target}")
                self._close("profit")

        elif self._position == "short":
            if self._stop and price >= self._stop:
                logger.info(f"Stop hit | price={price} stop={self._stop}")
                self._close("loss")
            elif self._target and price <= self._target:
                logger.info(f"Target hit | price={price} target={self._target}")
                self._close("profit")

    def _close(self, reason: str):
        product = self.trader.config["product"]
        prev_position = self._position
        prev_entry = self._entry_price

        if prev_position == "long":
            self._execute_order("SELL", product, 1, opencloseflag="1")
        elif prev_position == "short":
            self._execute_order("BUY", product, 1, opencloseflag="1")

        logger.info(f"Position closed | reason={reason} | was={prev_position} entry={prev_entry}")

        emoji = EXIT_EMOJI.get(reason, "⏹")
        reason_zh = {"profit": "停利出場", "loss": "停損出場", "reversed": "反向平倉"}.get(reason, reason)
        dry_tag = " [DRY RUN]" if self.dry_run else ""
        self._notify(
            f"{emoji} <b>出場{dry_tag}</b>\n"
            f"原因：{reason_zh}\n"
            f"方向：{'多' if prev_position == 'long' else '空'}\n"
            f"進場：{prev_entry}"
        )

        self._position = None
        self._entry_price = None
        self._stop = None
        self._target = None
        self._trade_id = None

    # ── 下單（dry_run 攔截）───────────────────────────────────────

    def _execute_order(self, side: str, product: str, qty: int, opencloseflag: str = ""):
        if self.dry_run:
            logger.info(f"[DRY RUN] {side} {product} x{qty} opencloseflag={opencloseflag!r}")
            return

        if side == "BUY":
            resp = self.trader.buy(product, qty, opencloseflag=opencloseflag)
        else:
            resp = self.trader.sell(product, qty, opencloseflag=opencloseflag)

        if not resp.issend:
            logger.error(f"Order failed | {side} {product}: {resp.errormsg}")

    # ── 通知 ─────────────────────────────────────────────────────

    def _notify(self, text: str):
        tg.send(self._tg_token, self._tg_chat, text)

    # ── HTTP helpers ──────────────────────────────────────────────

    def _fetch_latest_trade(self) -> Optional[dict]:
        resp = requests.get(HISTORY_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
