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
    - Syncs Worker-driven exits (OB-3, stop hit) and trailing stop updates
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
            history = self._fetch_history()
            if history:
                self._last_seen_id = history[0]["id"]
                logger.info(f"Startup: latest trade id={history[0]['id']} status={history[0].get('status')} — skipping")
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
                history = self._fetch_history()
                self._check_new_signal(history)
                self._sync_worker_state(history)
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

    def _check_new_signal(self, history: list):
        if not history:
            return

        trade    = history[0]
        trade_id = trade["id"]
        status   = trade.get("status", "")

        if trade_id == self._last_seen_id:
            return
        if status != "open":
            self._last_seen_id = trade_id
            return

        direction = trade.get("dir")
        with self._lock:
            if self._position == direction:
                logger.info(f"Same-direction skip | already {self._position} | id={trade_id} label={trade.get('sigLabel')}")
                self._last_seen_id = trade_id
                return

        logger.info(f"New open trade | id={trade_id} dir={direction} "
                    f"entry={trade.get('entry')} stop={trade.get('stop')} target={trade.get('target')} "
                    f"label={trade.get('sigLabel')}")
        self._last_seen_id = trade_id
        self._enter(trade)

    # ── Worker 狀態同步：OB-3 出場 + 移動停損更新 ─────────────────

    def _sync_worker_state(self, history: list):
        """
        Sync two things from Worker history:
        1. Worker-driven exits: OB-3, stop/target hit recorded by Worker
           → if our open trade is no longer 'open'/'trail', close locally
        2. Trailing stop updates: Worker moved stop up/down to lock profit
           → update self._stop so tick-level _check_exit uses latest value
        """
        with self._lock:
            if self._position is None or self._trade_id is None:
                return
            trade_id = self._trade_id

        # Find our trade in history (search up to 20 recent entries)
        our_trade = next((t for t in history[:20] if t["id"] == trade_id), None)
        if our_trade is None:
            return

        status   = our_trade.get("status", "open")
        new_stop = our_trade.get("stop")

        with self._lock:
            if self._position is None:
                return  # closed by tick between the two lock acquisitions

            if status == "loss":
                # Worker closed via OB-3, ATR stop, or other loss rule
                # Approximate exit price: entry + pnl (long) or entry - pnl (short)
                pnl = our_trade.get("pnl") or 0
                exit_price = (our_trade["entry"] + pnl) if self._position == "long" \
                             else (our_trade["entry"] - pnl)
                logger.info(f"Worker exit (loss) | id={trade_id} exit≈{exit_price}")
                self._close("loss", exit_price=exit_price)

            elif status == "profit":
                exit_price = our_trade.get("target")
                logger.info(f"Worker exit (profit) | id={trade_id} exit={exit_price}")
                self._close("profit", exit_price=exit_price)

            elif status in ("open", "trail") and new_stop and new_stop != self._stop:
                # Trailing stop moved in Worker — keep in sync
                logger.info(f"Trailing stop synced | {self._stop} → {new_stop} | id={trade_id}")
                self._stop = new_stop

            # 'reversed' is handled naturally by _check_new_signal when the new trade appears

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

    # ── 出場條件檢查（tick-level，毫秒級）────────────────────────

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

        # 立即清倉位狀態，確保後續 _enter() same-direction 判斷正確
        self._position    = None
        self._entry_price = None
        self._stop        = None
        self._target      = None
        self._trade_id    = None
        self._sig_label   = ""

        # Telegram 出場通知（用 try/except 確保 Telegram 失敗不影響倉位追蹤）
        emoji      = EXIT_EMOJI.get(reason, "⏹")
        reason_zh  = {"profit": "停利出場", "loss": "停損出場", "reversed": "反向平倉"}.get(reason, reason)
        dry_tag    = " [模擬]" if self.dry_run else ""
        pnl_sign   = "+" if pnl_pts >= 0 else ""
        pnl_line   = f"損益：<b>{pnl_sign}{pnl_pts:.0f} pts（{pnl_sign}NT${pnl_ntd:,}）</b>" if exit_price else ""
        try:
            self._notify(
                f"{emoji} <b>出場{dry_tag}</b>\n"
                f"原因：{reason_zh}\n"
                f"方向：{'多' if prev_position == 'long' else '空'}\n"
                f"進場：{prev_entry}　出場：{exit_price}\n"
                + pnl_line
            )
        except Exception as e:
            logger.warning(f"Exit notify failed: {e}")

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

    def _fetch_history(self) -> list:
        resp = requests.get(HISTORY_URL, timeout=10)
        resp.raise_for_status()
        return resp.json() or []
