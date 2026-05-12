import threading
import time
import logging
from typing import Optional, List
from datetime import datetime, timezone, timedelta, time as dtime
import requests

import telegram_notify as tg

logger = logging.getLogger(__name__)

HISTORY_URL = "https://mtx-monitor.asd261-af5.workers.dev/api/history"
POLL_INTERVAL = 15  # seconds
POINT_VALUE   = 50  # MXF: NT$50 per point
TZ_TW = timezone(timedelta(hours=8))

ENTRY_EMOJI = {"long": "🟢", "short": "🔴"}
EXIT_EMOJI  = {"profit": "✅", "loss": "❌", "reversed": "🔄"}


def _get_session(dt: datetime) -> str:
    t = dt.time()
    if dtime(8, 45) <= t < dtime(13, 45):
        return "day"
    if t >= dtime(15, 0) or t < dtime(5, 0):
        return "night"
    return "break"


class MTXStrategy:
    """
    Polls MTX-1 Monitor /api/history and executes trades via AutoTrader.
    - Detects new 'open' trades from the Monitor
    - Enters position via Unitrade API
    - Monitors stop/target on each tick for fast exit
    - Sends Telegram notifications with P&L on every exit
    - Sends session summary at end of day/night session
    """

    def __init__(self, trader, dry_run: bool = True):
        self.trader   = trader
        self.dry_run  = dry_run
        self._tg_token = trader.config.get("telegram_token", "")
        self._tg_chat  = trader.config.get("telegram_chat_id", "")

        # Position state (protected by lock)
        self._lock        = threading.Lock()
        self._position    = None    # None | "long" | "short"
        self._entry_price = None
        self._stop        = None
        self._target      = None
        self._trade_id    = None
        self._sig_label   = ""

        self._last_seen_id  = None
        self._running       = False

        # Session tracking
        self._current_session: Optional[str] = None
        self._session_trades: List[dict]      = []

        if dry_run:
            logger.info("[DRY RUN] Strategy in simulation mode — no real orders")

    # ── 啟動 / 停止 ──────────────────────────────────────────────

    def start(self):
        try:
            latest = self._fetch_latest_trade()
            if latest:
                self._last_seen_id = latest["id"]
                logger.info(f"Startup: latest trade id={latest['id']} status={latest.get('status')} — skipping")
        except Exception as e:
            logger.warning(f"Startup fetch failed: {e}")

        now = datetime.now(TZ_TW)
        self._current_session = _get_session(now)
        logger.info(f"Current session: {self._current_session}")

        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info(f"MTXStrategy started | dry_run={self.dry_run} | poll={POLL_INTERVAL}s")

    def stop(self):
        self._running = False

    # ── 行情 tick 回調 ────────────────────────────────────────────

    def on_tick(self, price: float):
        with self._lock:
            if self._position is None:
                return
            self._check_exit(price)

    # ── Poll 迴圈 ─────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                self._check_session_change()
                self._check_new_signal()
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def _check_session_change(self):
        now     = datetime.now(TZ_TW)
        session = _get_session(now)

        if session == self._current_session:
            return

        # Session just ended (day → break or night → break)
        if self._current_session in ("day", "night") and session == "break":
            self._send_session_summary(self._current_session)
            self._session_trades = []

        self._current_session = session
        if session in ("day", "night"):
            label = "日盤" if session == "day" else "夜盤"
            logger.info(f"{label}開始")

    def _check_new_signal(self):
        trade = self._fetch_latest_trade()
        if not trade:
            return

        trade_id = trade["id"]
        status   = trade.get("status", "")

        if trade_id == self._last_seen_id:
            return
        if status != "open":
            self._last_seen_id = trade_id
            return

        logger.info(f"New open trade | id={trade_id} dir={trade.get('dir')} "
                    f"entry={trade.get('entry')} stop={trade.get('stop')} target={trade.get('target')} "
                    f"label={trade.get('sigLabel')}")
        self._last_seen_id = trade_id
        self._enter(trade)

    # ── 進場 ─────────────────────────────────────────────────────

    def _enter(self, trade: dict):
        direction = trade.get("dir")
        product   = self.trader.config["product"]
        stop      = trade.get("stop")
        target    = trade.get("target")
        entry     = trade.get("entry")

        with self._lock:
            if self._position == "long" and direction == "short":
                self._close("reversed", exit_price=entry)
            elif self._position == "short" and direction == "long":
                self._close("reversed", exit_price=entry)

            if (self._position == "long"  and direction == "long") or \
               (self._position == "short" and direction == "short"):
                logger.info(f"Skipping same-direction signal (already {self._position}) | label={trade.get('sigLabel')}")
                return

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
            self._stop        = stop
            self._target      = target
            self._trade_id    = trade["id"]
            self._sig_label   = trade.get("sigLabel", "")
            logger.info(f"Position opened | {self._position} entry={entry} stop={stop} target={target}")

            emoji   = ENTRY_EMOJI.get(self._position, "📌")
            dry_tag = " [模擬]" if self.dry_run else ""
            self._notify(
                f"{emoji} <b>進場{dry_tag}</b>\n"
                f"信號：{self._sig_label}\n"
                f"方向：{'多' if self._position == 'long' else '空'}\n"
                f"進場：{entry}　停損：{stop}　停利：{target}"
            )

    # ── 出場條件檢查 ──────────────────────────────────────────────

    def _check_exit(self, price: float):
        if self._position == "long":
            if self._stop and price <= self._stop:
                logger.info(f"Stop hit | price={price} stop={self._stop}")
                self._close("loss", exit_price=price)
            elif self._target and price >= self._target:
                logger.info(f"Target hit | price={price} target={self._target}")
                self._close("profit", exit_price=price)
        elif self._position == "short":
            if self._stop and price >= self._stop:
                logger.info(f"Stop hit | price={price} stop={self._stop}")
                self._close("loss", exit_price=price)
            elif self._target and price <= self._target:
                logger.info(f"Target hit | price={price} target={self._target}")
                self._close("profit", exit_price=price)

    # ── 出場執行 ──────────────────────────────────────────────────

    def _close(self, reason: str, exit_price: Optional[float] = None):
        product       = self.trader.config["product"]
        prev_position = self._position
        prev_entry    = self._entry_price
        prev_label    = self._sig_label

        if prev_position == "long":
            self._execute_order("SELL", product, 1, opencloseflag="1")
        elif prev_position == "short":
            self._execute_order("BUY", product, 1, opencloseflag="1")

        # 計算損益
        pnl_pts = 0
        if exit_price and prev_entry:
            if prev_position == "long":
                pnl_pts = exit_price - prev_entry
            else:
                pnl_pts = prev_entry - exit_price
        pnl_ntd = int(pnl_pts * POINT_VALUE)

        logger.info(f"Position closed | reason={reason} | was={prev_position} entry={prev_entry} exit={exit_price} pnl={pnl_pts:+.0f}pts")

        # 記錄到 session
        self._session_trades.append({
            "label":     prev_label,
            "direction": prev_position,
            "entry":     prev_entry,
            "exit":      exit_price,
            "pnl_pts":   pnl_pts,
            "reason":    reason,
        })

        # Telegram 出場通知
        emoji      = EXIT_EMOJI.get(reason, "⏹")
        reason_zh  = {"profit": "停利出場", "loss": "停損出場", "reversed": "反向平倉"}.get(reason, reason)
        dry_tag    = " [模擬]" if self.dry_run else ""
        pnl_sign   = "+" if pnl_pts >= 0 else ""
        pnl_line   = f"損益：<b>{pnl_sign}{pnl_pts:.0f} pts（{pnl_sign}NT${pnl_ntd:,}）</b>" if exit_price else ""
        self._notify(
            f"{emoji} <b>出場{dry_tag}</b>\n"
            f"原因：{reason_zh}\n"
            f"方向：{'多' if prev_position == 'long' else '空'}\n"
            f"進場：{prev_entry}　出場：{exit_price}\n"
            + pnl_line
        )

        self._position    = None
        self._entry_price = None
        self._stop        = None
        self._target      = None
        self._trade_id    = None
        self._sig_label   = ""

    # ── Session 總結 ──────────────────────────────────────────────

    def _send_session_summary(self, session: str):
        trades = self._session_trades
        if not trades:
            return

        session_zh = "日盤" if session == "day" else "夜盤"
        total_pts  = sum(t["pnl_pts"] for t in trades)
        total_ntd  = int(total_pts * POINT_VALUE)
        wins       = sum(1 for t in trades if t["pnl_pts"] > 0)
        losses     = sum(1 for t in trades if t["pnl_pts"] < 0)
        total_sign = "+" if total_pts >= 0 else ""
        result_emoji = "🟢" if total_pts >= 0 else "🔴"

        lines = [f"📊 <b>{session_zh}總結</b>  {result_emoji}"]
        lines.append(f"{'─'*22}")
        for t in trades:
            icon   = EXIT_EMOJI.get(t["reason"], "⏹")
            sign   = "+" if t["pnl_pts"] >= 0 else ""
            dir_zh = "多" if t["direction"] == "long" else "空"
            lines.append(f"{icon} {t['label']}  {dir_zh}  {sign}{t['pnl_pts']:.0f}pts")
        lines.append(f"{'─'*22}")
        lines.append(f"筆數：{len(trades)}（勝{wins} 敗{losses}）")
        lines.append(f"合計：<b>{total_sign}{total_pts:.0f} pts（{total_sign}NT${total_ntd:,}）</b>")

        dry_tag = "　[模擬]" if self.dry_run else ""
        self._notify("\n".join(lines) + dry_tag)
        logger.info(f"Session summary sent | {session_zh} {total_sign}{total_pts:.0f}pts")

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
