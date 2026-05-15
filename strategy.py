import threading
import time
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta, time as dtime
import requests

import telegram_notify as tg

logger = logging.getLogger(__name__)

HISTORY_URL   = "https://mtx-monitor.asd261-af5.workers.dev/api/history"
POLL_INTERVAL = 15    # seconds
POINT_VALUE   = 50    # MXF: NT$50 per point
MAX_UNITS     = 2     # match Worker MAX_UNITS
PYRAMID_LOCK  = 15    # pts — first unit stop locked to entry ± this on pyramid

TZ_TW = timezone(timedelta(hours=8))

ENTRY_EMOJI = {"long": "🟢", "short": "🔴"}
EXIT_EMOJI  = {"profit": "✅", "loss": "❌", "reversed": "🔄", "replaced": "🔁", "trail": "🔒"}


def _get_session(dt: datetime) -> str:
    t = dt.time()
    if dtime(8, 45) <= t < dtime(13, 45):
        return "day"
    if t >= dtime(15, 0) or t < dtime(5, 0):
        return "night"
    return "break"


class MTXStrategy:
    """
    Polls MTX-1 Monitor /api/history and mirrors Worker's position state.
    Supports up to MAX_UNITS=2 simultaneous positions (matching Worker pyramiding).

    Key design decisions:
    - _sync_worker_state runs BEFORE _check_new_signal each poll cycle,
      so local state is cleared before evaluating new signals.
    - _check_new_signal scans ALL new trade IDs (not just history[0]),
      oldest-first, to avoid missing intermediate trades.
    - Same-direction skip does NOT update _last_seen_id, allowing retry
      after the existing position is closed by sync.
    """

    def __init__(self, trader, dry_run: bool = True):
        self.trader    = trader
        self.dry_run   = dry_run
        self._tg_token = trader.config.get("telegram_token", "")
        self._tg_chat  = trader.config.get("telegram_chat_id", "")

        self._lock = threading.Lock()

        # Multi-unit position tracking
        # Each entry: {id, dir, entry, stop, target, sig_label}
        self._units: List[Dict[str, Any]] = []

        self._last_seen_id: Optional[int] = None
        self._running = False

        # Session tracking
        self._current_session: Optional[str] = None
        self._session_trades:  List[dict]    = []
        self._prev_session_pnl_pts: float    = 0.0
        self._prev_session_label:   str      = ""

        if dry_run:
            logger.info("[DRY RUN] Strategy in simulation mode — no real orders")

    # ── 啟動 / 停止 ──────────────────────────────────────────────

    def start(self):
        try:
            history = self._fetch_history()
            if history:
                # Only restore trades opened in the current trading session
                # (avoids ghost "trail" entries from previous sessions in Worker KV)
                now_tw = datetime.now(TZ_TW)
                t = now_tw.time()
                if t >= dtime(15, 0) or t < dtime(5, 0):   # night session: started at 15:00 today (or yesterday)
                    base = now_tw if t >= dtime(15, 0) else (now_tw - timedelta(days=1))
                    session_start = base.replace(hour=15, minute=0, second=0, microsecond=0)
                elif t >= dtime(8, 45):                     # day session
                    session_start = now_tw.replace(hour=8, minute=45, second=0, microsecond=0)
                else:
                    session_start = now_tw - timedelta(hours=1)  # break or unknown: conservative 1h

                cutoff_ms = int(session_start.timestamp() * 1000)
                open_trades = sorted(
                    [t for t in history if isinstance(t, dict)
                     and t.get("status") in ("open", "trail")
                     and t.get("id", 0) > cutoff_ms],
                    key=lambda t: t["id"]
                )
                if open_trades:
                    for trade in open_trades[:MAX_UNITS]:
                        logger.info(f"Startup: restoring state id={trade['id']} dir={trade['dir']} (no order placed)")
                        self._open_unit(trade, notify=False, place_order=False)
                    self._last_seen_id = open_trades[-1]["id"]
                    logger.info(f"Startup: {len(self._units)} unit(s) state restored")
                else:
                    self._last_seen_id = history[0]["id"]
                    logger.info(f"Startup: no current-session open trades, last id={self._last_seen_id}")
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

    # ── 斷線 / 重連 ───────────────────────────────────────────────

    def on_disconnect(self):
        with self._lock:
            units = list(self._units)
        logger.warning(f"Disconnected | local units={len(units)}")
        if units:
            pos_desc = ", ".join(
                f"{'多' if u['dir'] == 'long' else '空'}@{u['entry']}" for u in units
            )
            dry_tag = " [模擬]" if self.dry_run else ""
            threading.Thread(target=self._safe_notify, args=(
                f"⚠️ <b>斷線警告{dry_tag}</b>\n"
                f"本地倉位：{pos_desc}\n正在等待重連...",
            ), daemon=True).start()

    def on_reconnect(self, broker_pos: Optional[dict] = None):
        with self._lock:
            units = list(self._units)

        dry_tag   = " [模擬]" if self.dry_run else ""
        local_dir = units[0]["dir"] if units else None
        logger.warning(f"Reconnected | local_units={len(units)} | broker={broker_pos}")

        if broker_pos:
            broker_dir = "long" if broker_pos.get("bs") == "B" else "short"
            mismatch   = (local_dir != broker_dir)
        else:
            mismatch = False

        if mismatch:
            b_zh = "多" if broker_dir == "long" else "空"
            l_zh = "多" if local_dir == "long" else ("空" if local_dir == "short" else "無")
            threading.Thread(target=self._safe_notify, args=(
                f"🚨 <b>重連倉位不一致{dry_tag}</b>\n"
                f"本地：{l_zh}（{len(units)}口）\n券商：{b_zh}\n請立即手動確認！",
            ), daemon=True).start()
        elif units:
            pos_desc = ", ".join(
                f"{'多' if u['dir'] == 'long' else '空'}@{u['entry']}" for u in units
            )
            threading.Thread(target=self._safe_notify, args=(
                f"✅ <b>重連成功{dry_tag}</b>\n倉位確認：{pos_desc}（券商一致）",
            ), daemon=True).start()
        else:
            logger.info("Reconnect: no open position, no action needed")

    # ── 行情 tick 回調 ────────────────────────────────────────────

    def on_tick(self, price: float):
        with self._lock:
            if not self._units:
                return
            for unit in list(self._units):
                self._check_exit_unit(unit, price)

    # ── Poll 迴圈 ─────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                self._check_session_change()
                history = self._fetch_history()
                self._sync_worker_state(history)  # ① sync exits first — clears closed positions
                self._check_new_signal(history)   # ② then pick up new signals with fresh state
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(POLL_INTERVAL)

    def _check_session_change(self):
        now     = datetime.now(TZ_TW)
        session = _get_session(now)
        if session == self._current_session:
            return
        if self._current_session in ("day", "night") and session == "break":
            self._send_session_summary(self._current_session)
            self._session_trades = []
        self._current_session = session
        if session in ("day", "night"):
            logger.info(f"{'日盤' if session == 'day' else '夜盤'}開始")
            threading.Thread(target=self._send_open_notify, args=(session,), daemon=True).start()

    # ── 新訊號偵測 ────────────────────────────────────────────────

    def _check_new_signal(self, history: list):
        if not history:
            return

        cutoff = self._last_seen_id or 0
        new_trades = sorted(
            [t for t in history if isinstance(t, dict) and t.get("id", 0) > cutoff],
            key=lambda t: t["id"]   # oldest-first
        )
        if not new_trades:
            return

        for trade in new_trades:
            trade_id  = trade["id"]
            status    = trade.get("status", "")
            direction = trade.get("dir")

            if status != "open":
                # Already closed before we could act — mark seen, keep scanning
                self._last_seen_id = trade_id
                continue

            with self._lock:
                cur_dir = self._units[0]["dir"] if self._units else None
                n_units = len(self._units)

            if cur_dir == direction:
                if n_units >= MAX_UNITS:
                    # At max units, same direction: Worker ignores too
                    logger.info(f"Max units ({MAX_UNITS}), same-direction ignore | id={trade_id}")
                    self._last_seen_id = trade_id
                    continue
                else:
                    # Pyramid: Worker already verified canPyramid conditions
                    logger.info(f"Pyramid | unit {n_units + 1}/{MAX_UNITS} | id={trade_id}")
                    self._last_seen_id = trade_id
                    self._add_unit(trade)
                    return
            else:
                # No position, or reversal
                self._last_seen_id = trade_id
                self._enter(trade)
                return

    # ── Worker 狀態同步（每口獨立）────────────────────────────────

    def _sync_worker_state(self, history: list):
        with self._lock:
            if not self._units:
                return
            units_snapshot = list(self._units)

        for unit in units_snapshot:
            # Scan full history (Worker keeps up to 50 entries) — earlier slice [:20]
            # silently abandoned units that fell off the front, leaving stale local state
            # that later got reversed at far-away prices, producing huge phantom P&L.
            our_trade = next((t for t in history if t.get("id") == unit["id"]), None)
            if our_trade is None:
                continue

            status   = our_trade.get("status", "open")
            new_stop = our_trade.get("stop")

            with self._lock:
                if unit not in self._units:
                    continue  # closed during this iteration

                if status == "loss":
                    pnl        = our_trade.get("pnl") or 0
                    exit_price = (our_trade["entry"] + pnl) if unit["dir"] == "long" \
                                 else (our_trade["entry"] - pnl)
                    logger.info(f"Worker exit (loss) | id={unit['id']}")
                    self._close_unit(unit, "loss", exit_price=exit_price)

                elif status == "profit":
                    exit_price = our_trade.get("target")
                    logger.info(f"Worker exit (profit) | id={unit['id']}")
                    self._close_unit(unit, "profit", exit_price=exit_price)

                elif status == "trail":
                    # Worker (worker/index.js:782/785) writes status='trail' when trailing stop hits.
                    # Use the trail-stop level as exit_price; Worker's t.pnl already reflects locked profit.
                    pnl        = our_trade.get("pnl") or 0
                    exit_price = our_trade.get("stop")
                    logger.info(f"Worker exit (trail) | id={unit['id']}")
                    self._close_unit(unit, "trail", exit_price=exit_price)

                elif status == "reversed":
                    # Worker stores P&L in t.pnl (= triggering-trade entry ± our entry).
                    pnl        = our_trade.get("pnl") or 0
                    exit_price = (our_trade["entry"] + pnl) if unit["dir"] == "long" \
                                 else (our_trade["entry"] - pnl)
                    # Worker uses status="reversed" for both true direction reversals AND
                    # same-direction replacements (e.g. ⑧ never pyramids — it always replaces).
                    # Distinguish by checking the triggering trade's direction.
                    triggering = next(
                        (t for t in history
                         if isinstance(t, dict)
                         and t.get("id", 0) > unit["id"]
                         and t.get("status") == "open"),
                        None,
                    )
                    same_dir = bool(triggering and triggering.get("dir") == unit["dir"])
                    reason   = "replaced" if same_dir else "reversed"
                    logger.info(f"Worker exit ({reason}) | id={unit['id']}")
                    self._close_unit(unit, reason, exit_price=exit_price)

                elif status in ("open", "trail") and new_stop and new_stop != unit["stop"]:
                    logger.info(f"Trailing stop synced | {unit['stop']} → {new_stop} | id={unit['id']}")
                    unit["stop"] = new_stop

    # ── 進場（反向或首口）────────────────────────────────────────

    def _enter(self, trade: dict):
        direction = trade.get("dir")
        entry     = trade.get("entry")

        with self._lock:
            for unit in list(self._units):
                if unit["dir"] != direction:
                    self._close_unit(unit, "reversed", exit_price=entry)
            if self._units:
                return  # unexpected same-direction units still open
            self._open_unit(trade)

    # ── 加碼（第 2 口）────────────────────────────────────────────

    def _add_unit(self, trade: dict):
        with self._lock:
            if not self._units or len(self._units) >= MAX_UNITS:
                return
            # Lock first unit stop to entry ± PYRAMID_LOCK
            first = self._units[0]
            if first["dir"] == "long":
                first["stop"] = max(first["stop"] or 0, first["entry"] + PYRAMID_LOCK)
            else:
                first["stop"] = min(first["stop"] or 999999, first["entry"] - PYRAMID_LOCK)
            logger.info(f"First unit stop locked → {first['stop']}")
            self._open_unit(trade, is_pyramid=True)

    # ── 開倉執行 ──────────────────────────────────────────────────

    def _open_unit(self, trade: dict, is_pyramid: bool = False, notify: bool = True,
                   place_order: bool = True):
        # Call within lock (or at startup before threads start)
        direction = trade.get("dir")
        product   = self.trader.config["product"]

        if place_order:
            if direction == "long":
                self._execute_order("BUY", product, 1)
            elif direction == "short":
                self._execute_order("SELL", product, 1)
            else:
                logger.warning(f"Unknown direction: {direction}")
                return

        unit = {
            "id":        trade["id"],
            "dir":       direction,
            "entry":     trade.get("entry"),
            "stop":      trade.get("stop"),
            "target":    trade.get("target"),
            "sig_label": trade.get("sigLabel", ""),
        }
        self._units.append(unit)
        logger.info(f"Unit {len(self._units)} opened | {direction} entry={unit['entry']} stop={unit['stop']}")

        if not notify:
            return

        emoji   = ENTRY_EMOJI.get(direction, "📌")
        dry_tag = " [模擬]" if self.dry_run else ""
        suffix  = "（加碼）" if is_pyramid else ""
        text = (
            f"{emoji} <b>進場{dry_tag}{suffix}</b>\n"
            f"信號：{unit['sig_label']}\n"
            f"方向：{'多' if direction == 'long' else '空'}\n"
            f"進場：{unit['entry']}　停損：{unit['stop']}　停利：{unit['target']}"
        )
        threading.Thread(target=self._safe_notify, args=(text,), daemon=True).start()

    # ── tick-level 出場檢查（每口獨立）────────────────────────────

    def _check_exit_unit(self, unit: dict, price: float):
        # Call within lock
        if unit not in self._units:
            return
        if unit["dir"] == "long":
            if unit["stop"] and price <= unit["stop"]:
                logger.info(f"Stop hit | id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, "loss", exit_price=price)
            elif unit["target"] and price >= unit["target"]:
                logger.info(f"Target hit | id={unit['id']} price={price} target={unit['target']}")
                self._close_unit(unit, "profit", exit_price=price)
        elif unit["dir"] == "short":
            if unit["stop"] and price >= unit["stop"]:
                logger.info(f"Stop hit | id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, "loss", exit_price=price)
            elif unit["target"] and price <= unit["target"]:
                logger.info(f"Target hit | id={unit['id']} price={price} target={unit['target']}")
                self._close_unit(unit, "profit", exit_price=price)

    # ── 平倉執行（單口）──────────────────────────────────────────

    def _close_unit(self, unit: dict, reason: str, exit_price=None):
        # Call within lock
        if unit not in self._units:
            return

        product = self.trader.config["product"]
        if unit["dir"] == "long":
            self._execute_order("SELL", product, 1, opencloseflag="1")
        else:
            self._execute_order("BUY", product, 1, opencloseflag="1")

        pnl_pts = 0
        if exit_price and unit["entry"]:
            pnl_pts = (exit_price - unit["entry"]) if unit["dir"] == "long" \
                      else (unit["entry"] - exit_price)
        pnl_ntd = int(pnl_pts * POINT_VALUE)

        logger.info(f"Unit closed | reason={reason} dir={unit['dir']} "
                    f"entry={unit['entry']} exit={exit_price} pnl={pnl_pts:+.0f}pts")

        self._session_trades.append({
            "label":     unit["sig_label"],
            "direction": unit["dir"],
            "entry":     unit["entry"],
            "exit":      exit_price,
            "pnl_pts":   pnl_pts,
            "reason":    reason,
        })
        self._units.remove(unit)

        emoji     = EXIT_EMOJI.get(reason, "⏹")
        reason_zh = {"profit": "停利出場", "loss": "停損出場",
                     "reversed": "反向平倉", "replaced": "汰換平倉",
                     "trail": "移動停利"}.get(reason, reason)
        dry_tag   = " [模擬]" if self.dry_run else ""
        pnl_sign  = "+" if pnl_pts >= 0 else ""
        pnl_line  = (f"損益：<b>{pnl_sign}{pnl_pts:.0f} pts（{pnl_sign}NT${pnl_ntd:,}）</b>"
                     if exit_price else "")
        text = (
            f"{emoji} <b>出場{dry_tag}</b>\n"
            f"原因：{reason_zh}\n"
            f"方向：{'多' if unit['dir'] == 'long' else '空'}\n"
            f"進場：{unit['entry']}　出場：{exit_price}\n"
            + pnl_line
        )
        threading.Thread(target=self._safe_notify, args=(text,), daemon=True).start()

    # ── Session 總結 ──────────────────────────────────────────────

    def _send_session_summary(self, session: str):
        trades = self._session_trades
        if not trades:
            return

        session_zh   = "日盤" if session == "day" else "夜盤"
        total_pts    = sum(t["pnl_pts"] for t in trades)
        total_ntd    = int(total_pts * POINT_VALUE)
        wins         = sum(1 for t in trades if t["pnl_pts"] > 0)
        losses       = sum(1 for t in trades if t["pnl_pts"] < 0)
        total_sign   = "+" if total_pts >= 0 else ""
        result_emoji = "🟢" if total_pts >= 0 else "🔴"

        lines = [f"📊 <b>{session_zh}總結</b>  {result_emoji}", "─" * 22]
        for t in trades:
            icon   = EXIT_EMOJI.get(t["reason"], "⏹")
            sign   = "+" if t["pnl_pts"] >= 0 else ""
            dir_zh = "多" if t["direction"] == "long" else "空"
            lines.append(f"{icon} {t['label']}  {dir_zh}  {sign}{t['pnl_pts']:.0f}pts")
        lines.append("─" * 22)
        lines.append(f"筆數：{len(trades)}（勝{wins} 敗{losses}）")
        lines.append(f"合計：<b>{total_sign}{total_pts:.0f} pts（{total_sign}NT${total_ntd:,}）</b>")

        dry_tag = "　[模擬]" if self.dry_run else ""
        self._notify("\n".join(lines) + dry_tag)
        logger.info(f"Session summary sent | {session_zh} {total_sign}{total_pts:.0f}pts")

        self._prev_session_pnl_pts = total_pts
        self._prev_session_label   = session_zh

    # ── 開盤通知 ──────────────────────────────────────────────────

    def _send_open_notify(self, session: str):
        session_zh = "日盤" if session == "day" else "夜盤"
        close_time = "13:45" if session == "day" else "05:00（+1）"
        dry_tag    = " [模擬]" if self.dry_run else ""

        with self._lock:
            units = list(self._units)

        if units:
            pos_lines = [
                f"持倉：{'多' if u['dir'] == 'long' else '空'}  進場 {u['entry']}  停損 {u['stop']}  停利 {u['target']}"
                for u in units
            ]
            pos_text = "\n".join(pos_lines)
        else:
            pos_text = "持倉：無"

        lines = [f"🔔 <b>{session_zh}開盤{dry_tag}</b>", "系統：✅ 正常運作", pos_text]
        if self._prev_session_label:
            prev_sign = "+" if self._prev_session_pnl_pts >= 0 else ""
            prev_ntd  = int(self._prev_session_pnl_pts * POINT_VALUE)
            lines.append(
                f"{self._prev_session_label}損益：{prev_sign}{self._prev_session_pnl_pts:.0f} pts"
                f"（{prev_sign}NT${prev_ntd:,}）"
            )
        lines.append(f"收盤：{close_time}")

        try:
            self._notify("\n".join(lines))
        except Exception as e:
            logger.warning(f"Open notify failed: {e}")
        logger.info(f"Open notify sent | {session_zh}")

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

    def _safe_notify(self, text: str):
        try:
            tg.send(self._tg_token, self._tg_chat, text)
        except Exception as e:
            logger.warning(f"Notify failed: {e}")

    # ── HTTP ─────────────────────────────────────────────────────

    def _fetch_history(self) -> list:
        resp = requests.get(HISTORY_URL, timeout=10)
        resp.raise_for_status()
        return resp.json() or []
