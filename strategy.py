import os
import json
import threading
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta, date, time as dtime
import requests

import heartbeat
import trade_log_emit
import telegram_notify as tg

logger = logging.getLogger(__name__)

HISTORY_URL      = "https://mtx-monitor.asd261-af5.workers.dev/api/history"
FVG_SIGNALS_URL  = "https://mtx-monitor.asd261-af5.workers.dev/api/signals?source=fvg"
# Phase 6a-lite: 'shadow' fetches FVG signals and logs/notifies but does NOT trade.
# Future modes: 'off' (skip entirely), 'paper' (full _units tracking, no broker),
# 'live' (real orders — needs Phase 6c kill-switch + multi-account safety).
FVG_OBSERVE_MODE = os.getenv("FVG_MODE", "shadow")
POLL_INTERVAL = 3     # seconds
POINT_VALUE   = 50    # MXF: NT$50 per point
MAX_UNITS     = 2     # match Worker MAX_UNITS — kept for MTX backwards-compat, will be removed in Phase 6b
PYRAMID_LOCK  = 15    # pts — first unit stop locked to entry ± this on pyramid

# ── Daily MAX LOSS lock (Phase 7 minimum) ──────────────────────────
# When today's cumulative P&L (across all sources, across day+night sessions)
# drops below this threshold, trader refuses new ENTRY/PYRAMID signals.
# Existing open positions continue to close naturally (SL/TP/reversed).
# Resets at trading-day boundary (08:45 TW, day session open).
# Set via .env DAILY_MAX_LOSS_PTS=-300 (must be negative). Empty/unset → disabled.
_dml_env = os.getenv("DAILY_MAX_LOSS_PTS", "").strip()
try:
    DAILY_MAX_LOSS_PTS: Optional[float] = float(_dml_env) if _dml_env else None
    if DAILY_MAX_LOSS_PTS is not None and DAILY_MAX_LOSS_PTS >= 0:
        DAILY_MAX_LOSS_PTS = None  # non-negative is nonsensical; treat as disabled
except ValueError:
    DAILY_MAX_LOSS_PTS = None

TZ_TW = timezone(timedelta(hours=8))

# Persistent trade log + monthly summary (append-only,跨 restart 持久化)
TRADES_LOG_PATH      = Path(__file__).parent / "trades.jsonl"
MONTHLY_SUMMARY_PATH = Path(__file__).parent / "monthly_summary.jsonl"

# Plan D: broker reconciliation
RECON_CHECK_INTERVAL_SEC = 60   # how often to query broker (be polite to API)
RECON_ALERT_AGE_SEC      = 180  # mismatch must persist > 3 min before alerting

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

        # Phase 6a-lite: FVG shadow observation (fetch + log, no trade)
        self._fvg_last_status: Dict[int, str] = {}  # signal_id → last-seen status
        self._fvg_primed:       bool          = False  # True after first poll absorbs initial state silently

        # Phase 7: daily MAX LOSS lock state. Counter accumulates across day+night
        # of the same trading day, resets at 08:45 TW (day session open).
        self._trading_day_pnl_pts: float           = 0.0
        self._trading_day_date:    Optional[Any]   = None  # `date` object, None until first poll
        self._trading_day_locked:  bool            = False
        self._trading_day_alert_sent: bool         = False  # one-shot Telegram on lock trigger

        # Phase 6c-minimum: parallel FVG position tracker (FVG max 1 → single Optional).
        # Set when FVG_MODE in ('paper','live') receives an 'open' signal; cleared on close.
        # NOT in _units (no MTX path refactor needed). Shape:
        #   {id, dir, entry, stop, target, label, opened_at}
        self._fvg_position: Optional[Dict[str, Any]] = None

        # Persistent trades log + monthly P&L counters. Resets on month change (TW trading-day boundary).
        # Restored from trades.jsonl at startup so restart doesn't lose mid-month state.
        self._current_month:        Optional[str]            = None  # "YYYY-MM"
        self._month_pnl_pts:        float                    = 0.0
        self._month_trades_count:   int                      = 0
        self._month_wins:           int                      = 0
        self._month_losses:         int                      = 0
        self._month_by_source:      Dict[str, Dict[str, Any]] = {}

        # Plan D: broker reconciliation safety net. Periodically (~1 min) compare
        # broker's actual net position vs trader's expected (sum of _units MTX +
        # _fvg_position FVG). Mismatch persisting > RECON_ALERT_AGE_SEC → Telegram
        # alert. Catches drift like the 2026-05-16 FVG engine non-determinism bug
        # where broker has a position but trader's _fvg_position is None.
        self._recon_last_check:      float                  = 0.0    # epoch s of last check
        self._recon_mismatch_since:  Optional[float]        = None   # when mismatch first observed
        self._recon_alert_sent:      bool                   = False  # one-shot dedup
        self._recon_last_broker_net: Optional[int]          = None   # last observed broker signed net (for heartbeat)
        self._recon_last_expected:   Optional[int]          = None   # last computed expected signed net

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

        # Restore current-month P&L counters from trades.jsonl (so restart preserves state)
        self._restore_month_from_log()

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
                self._check_trading_day_reset()   # Phase 7: reset daily P&L counter at 08:45 TW
                history = self._fetch_history()
                self._sync_worker_state(history)  # ① sync exits first — clears closed positions
                self._check_new_signal(history)   # ② then pick up new signals with fresh state
            except Exception as e:
                logger.error(f"Poll error: {e}")
            # Phase 6a-lite: FVG shadow observation (separate try/except so MTX path is never
            # affected by FVG side issues — shadow is observational only, must never break trader).
            try:
                self._observe_fvg_signals(self._fetch_fvg_signals())
            except Exception as e:
                logger.debug(f"FVG observe error (silent): {e}")
            # Plan D: broker reconciliation safety net (throttled to ~1 min via internal check)
            try:
                self._check_broker_reconciliation()
            except Exception as e:
                logger.debug(f"recon error (silent): {e}")
            # Fire-and-forget heartbeat — outside try/except so it fires even when poll itself
            # errors (watchdog needs to see "process alive but polls failing" as a signal).
            heartbeat.send({
                "ts":                  int(time.time() * 1000),
                "pid":                 os.getpid(),
                "session":             self._current_session,
                "units":               len(self._units),
                "last_seen_id":        self._last_seen_id,
                "trading_day_pnl_pts": self._trading_day_pnl_pts,
                "trading_day_locked":  self._trading_day_locked,
                "fvg_mode":            FVG_OBSERVE_MODE,
                "fvg_position_id":     self._fvg_position.get("id") if self._fvg_position else None,
                "month":               self._current_month,
                "month_pnl_pts":       self._month_pnl_pts,
                "month_trades_count":  self._month_trades_count,
                "recon_broker_net":    self._recon_last_broker_net,
                "recon_expected_net":  self._recon_last_expected,
                "recon_alert_sent":    self._recon_alert_sent,
            })
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

    @staticmethod
    def _compute_trading_day(now: datetime) -> date:
        """Trading day boundary at 08:45 TW. Before 08:45 = yesterday's trading day."""
        return (now - timedelta(days=1)).date() if now.time() < dtime(8, 45) else now.date()

    def _record_trade(self, *, source: str, label: str, dir_: str, entry, exit_price,
                       stop, target, pnl_pts: float, reason: str, sig_id, opened_at_ms):
        """Append one trade record to trades.jsonl AND update monthly counters.

        Called from _close_unit (MTX) and _fvg_handle_trade (FVG close branch).
        Wraps file I/O + counter math in try/except so failures don't break trading.
        """
        try:
            now = datetime.now(TZ_TW)
            trading_day = self._compute_trading_day(now)
            duration_sec = None
            if isinstance(opened_at_ms, (int, float)) and opened_at_ms > 0:
                duration_sec = int(time.time() - opened_at_ms / 1000)
            record = {
                "ts":           int(time.time()),
                "trading_day":  trading_day.isoformat(),
                "session":      self._current_session,
                "source":       source,
                "id":           sig_id,
                "label":        label,
                "dir":          dir_,
                "entry":        entry,
                "exit":         exit_price,
                "stop":         stop,
                "target":       target,
                "pnl_pts":      pnl_pts,
                "pnl_ntd":      int(pnl_pts * POINT_VALUE) if pnl_pts else 0,
                "reason":       reason,
                "duration_sec": duration_sec,
            }
            with open(TRADES_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # Cloud backup (Worker /api/trade_log,Worker 端 dedup by (id, reason))
            trade_log_emit.send(record)
        except Exception as e:
            logger.error(f"trades.jsonl write failed: {e}")
        # Update monthly counters (in-memory)
        self._month_pnl_pts      += pnl_pts
        self._month_trades_count += 1
        if pnl_pts > 0:
            self._month_wins   += 1
        elif pnl_pts < 0:
            self._month_losses += 1
        bucket = self._month_by_source.setdefault(source, {"pnl_pts": 0.0, "count": 0, "wins": 0, "losses": 0})
        bucket["pnl_pts"] += pnl_pts
        bucket["count"]   += 1
        if pnl_pts > 0:
            bucket["wins"]   += 1
        elif pnl_pts < 0:
            bucket["losses"] += 1

    def _archive_and_reset_month(self, old_month: str, new_month: str):
        """Called on month boundary (trading-day basis). Writes summary to
        monthly_summary.jsonl + Telegram notification + resets in-memory counters."""
        try:
            wr = self._month_wins / max(self._month_trades_count, 1) * 100
            summary = {
                "month":         old_month,
                "total_pnl_pts": self._month_pnl_pts,
                "total_pnl_ntd": int(self._month_pnl_pts * POINT_VALUE),
                "trades":        self._month_trades_count,
                "wins":          self._month_wins,
                "losses":        self._month_losses,
                "win_rate_pct":  round(wr, 2),
                "by_source":     self._month_by_source,
                "archived_at":   int(time.time() * 1000),
            }
            with open(MONTHLY_SUMMARY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            # Telegram summary
            by_src_parts = []
            for src, d in self._month_by_source.items():
                sign = "+" if d.get("pnl_pts", 0) >= 0 else ""
                by_src_parts.append(f"{src.upper()} {sign}{d.get('pnl_pts', 0):.0f}pts ({d.get('count', 0)})")
            by_src_line = "  ".join(by_src_parts) if by_src_parts else "(無)"
            sign = "+" if self._month_pnl_pts >= 0 else ""
            self._safe_notify(
                f"🌙 <b>月結 — {old_month}</b>\n"
                f"總筆數:{self._month_trades_count}\n"
                f"勝/敗:{self._month_wins}/{self._month_losses} ({wr:.1f}%)\n"
                f"合計:{sign}{self._month_pnl_pts:.0f} pts ({sign}NT${int(self._month_pnl_pts * POINT_VALUE):,})\n"
                f"依來源:{by_src_line}\n"
                f"📅 {new_month} 起重新計算"
            )
            logger.info(f"Month archived: {old_month} pnl={self._month_pnl_pts:+.0f}pts trades={self._month_trades_count}")
        except Exception as e:
            logger.error(f"month archive failed: {e}")
        # Reset counters (always, even if archive write failed — don't double-count)
        self._month_pnl_pts        = 0.0
        self._month_trades_count   = 0
        self._month_wins           = 0
        self._month_losses         = 0
        self._month_by_source      = {}

    def _restore_month_from_log(self):
        """On startup, scan trades.jsonl, filter to current month (trading-day basis),
        sum into _month_* counters so restart doesn't lose mid-month state."""
        now = datetime.now(TZ_TW)
        td  = self._compute_trading_day(now)
        current_month_str = td.strftime("%Y-%m")
        self._current_month = current_month_str
        if not TRADES_LOG_PATH.exists():
            logger.info(f"No trades.jsonl yet — month {current_month_str} starts at 0")
            return
        try:
            count = 0
            with open(TRADES_LOG_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    rec_td = rec.get("trading_day", "")
                    if not rec_td.startswith(current_month_str):
                        continue
                    pnl = rec.get("pnl_pts", 0) or 0
                    src = rec.get("source", "?")
                    self._month_pnl_pts      += pnl
                    self._month_trades_count += 1
                    if pnl > 0:   self._month_wins   += 1
                    elif pnl < 0: self._month_losses += 1
                    bucket = self._month_by_source.setdefault(src, {"pnl_pts": 0.0, "count": 0, "wins": 0, "losses": 0})
                    bucket["pnl_pts"] += pnl
                    bucket["count"]   += 1
                    if pnl > 0:   bucket["wins"]   += 1
                    elif pnl < 0: bucket["losses"] += 1
                    count += 1
            logger.info(f"Monthly restored: month={current_month_str} pnl={self._month_pnl_pts:+.0f}pts trades={self._month_trades_count} (read {count} lines)")
        except Exception as e:
            logger.warning(f"Monthly restore failed (continuing with 0s): {e}")

    def _expected_net_position(self) -> int:
        """Trader's expected signed net lots = sum of MTX _units + FVG _fvg_position."""
        net = 0
        for u in self._units:
            net += 1 if u["dir"] == "long" else -1
        if self._fvg_position is not None:
            net += 1 if self._fvg_position["dir"] == "long" else -1
        return net

    def _check_broker_reconciliation(self):
        """Plan D safety net: compare broker's actual net position vs trader's
        expected net. Persistent mismatch (>3 min) → Telegram alert.

        Catches drift scenarios like the 2026-05-16 FVG bug where broker has
        a position but trader's _fvg_position is None (or vice versa).
        Throttled to once per RECON_CHECK_INTERVAL_SEC (~1 min) to be API-polite.
        """
        now = time.time()
        if now - self._recon_last_check < RECON_CHECK_INTERVAL_SEC:
            return
        self._recon_last_check = now

        try:
            broker_pos = self.trader._query_broker_position()
        except Exception as e:
            logger.debug(f"recon: broker query failed (silent): {e}")
            return

        # Broker returns {productid, bs, qty} or None.
        if broker_pos is None:
            broker_net = 0
        else:
            qty = int(broker_pos.get("qty", 0) or 0)
            bs  = broker_pos.get("bs", "")
            broker_net = qty if bs == "B" else (-qty if bs == "S" else 0)

        expected_net = self._expected_net_position()
        self._recon_last_broker_net = broker_net
        self._recon_last_expected   = expected_net

        if broker_net == expected_net:
            # Aligned — clear any prior mismatch state, notify recovery if alerted
            if self._recon_alert_sent:
                logger.info(f"Broker recon recovered: net={broker_net}")
                self._safe_notify(
                    f"✅ <b>Broker 對帳已恢復</b>\n"
                    f"目前 net = {broker_net} 口\n"
                    f"(trader 視角 {expected_net} 口,broker {broker_net} 口,一致)"
                )
            self._recon_mismatch_since = None
            self._recon_alert_sent     = False
            return

        # Mismatch detected
        if self._recon_mismatch_since is None:
            self._recon_mismatch_since = now
            logger.warning(f"Broker recon mismatch: expected={expected_net} broker={broker_net} (starting tolerance window)")
            return

        age = now - self._recon_mismatch_since
        logger.debug(f"recon mismatch ongoing: expected={expected_net} broker={broker_net} age={age:.0f}s")
        if age > RECON_ALERT_AGE_SEC and not self._recon_alert_sent:
            self._recon_alert_sent = True
            # Build context for alert
            mtx_count = len(self._units)
            mtx_long  = sum(1 for u in self._units if u["dir"] == "long")
            mtx_short = mtx_count - mtx_long
            fvg_str   = f"{self._fvg_position['dir']} (id={self._fvg_position['id']})" if self._fvg_position else "(無)"
            logger.error(f"DAILY_RECON_ALERT: broker_net={broker_net} expected_net={expected_net} age={age:.0f}s")
            self._safe_notify(
                f"⚠️ <b>Broker 對帳異常 {int(age/60)} 分鐘</b>\n"
                f"Trader 預期: <b>{expected_net} 口</b>\n"
                f"  MTX _units: {mtx_count} ({mtx_long} long / {mtx_short} short)\n"
                f"  FVG _fvg_position: {fvg_str}\n"
                f"Broker 實際: <b>{broker_net} 口</b>\n"
                f"差距: <b>{broker_net - expected_net:+d} 口</b>\n"
                f"建議:檢查是否 FVG bot engine 漏 close 訊號(見 [[feedback-fvg-engine-multi-open-bug]]),"
                f"或執行 /flat-position skill 手動對齊"
            )

    def _check_trading_day_reset(self):
        """Reset daily P&L counter when trading day flips (08:45 TW).
        Also detects MONTH change → archive monthly summary + Telegram + reset.
        """
        now   = datetime.now(TZ_TW)
        today = self._compute_trading_day(now)
        # First call: initialize state, no reset action
        if self._trading_day_date is None:
            self._trading_day_date = today
            if self._current_month is None:
                self._current_month = today.strftime("%Y-%m")
            return
        if today == self._trading_day_date:
            return
        # --- Trading day changed: reset daily lock state ---
        prev_pnl     = self._trading_day_pnl_pts
        was_locked   = self._trading_day_locked
        self._trading_day_date        = today
        self._trading_day_pnl_pts     = 0.0
        self._trading_day_locked      = False
        self._trading_day_alert_sent  = False
        logger.info(f"Trading day reset → {today} (prev_pnl={prev_pnl:+.0f}pts was_locked={was_locked})")
        if DAILY_MAX_LOSS_PTS is not None or was_locked:
            self._safe_notify(
                f"🌅 <b>Trading Day Reset</b>\n"
                f"日期:{today}\n"
                f"前日損益:{prev_pnl:+.0f} pts\n"
                f"前日鎖手狀態:{'是 (已解除)' if was_locked else '否'}\n"
                f"今日 MAX LOSS:{'未設定' if DAILY_MAX_LOSS_PTS is None else f'{DAILY_MAX_LOSS_PTS:+.0f} pts'}"
            )
        # --- Month changed? archive prev month, reset counters ---
        new_month = today.strftime("%Y-%m")
        if self._current_month is not None and new_month != self._current_month:
            self._archive_and_reset_month(self._current_month, new_month)
        self._current_month = new_month

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

        # Phase 7: daily MAX LOSS gate. Only blocks real broker calls; startup state
        # restore (place_order=False) is always allowed so locked positions resume tracking.
        if place_order and self._trading_day_locked:
            label = "加碼" if is_pyramid else "進場"
            logger.warning(f"Daily MAX LOSS lock active — refusing {label} signal id={trade.get('id')}")
            # No Telegram on each rejection (would spam); the original lock-trigger msg is enough
            return

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
            "opened_at": int(time.time() * 1000),  # epoch ms, for trade duration calc
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
        # Persistent trade log + monthly counters (跨 restart 持久化)
        self._record_trade(
            source="mtx", label=unit["sig_label"], dir_=unit["dir"],
            entry=unit["entry"], exit_price=exit_price, stop=unit["stop"],
            target=unit["target"], pnl_pts=pnl_pts, reason=reason,
            sig_id=unit["id"], opened_at_ms=unit.get("opened_at"),
        )
        # Phase 7: accumulate into trading-day P&L counter (persists across session changes)
        self._trading_day_pnl_pts += pnl_pts
        if (DAILY_MAX_LOSS_PTS is not None
                and not self._trading_day_locked
                and self._trading_day_pnl_pts <= DAILY_MAX_LOSS_PTS):
            self._trading_day_locked = True
            logger.warning(f"DAILY_MAX_LOSS triggered: pnl={self._trading_day_pnl_pts:+.0f} ≤ {DAILY_MAX_LOSS_PTS:+.0f}")
            if not self._trading_day_alert_sent:
                self._trading_day_alert_sent = True
                self._safe_notify(
                    f"🛑 <b>每日 MAX LOSS 觸發</b>\n"
                    f"今日損益:{self._trading_day_pnl_pts:+.0f} pts (NT${int(self._trading_day_pnl_pts * POINT_VALUE):,})\n"
                    f"門檻:{DAILY_MAX_LOSS_PTS:+.0f} pts\n"
                    f"動作:trader 已綁手,拒收新進場/加碼訊號\n"
                    f"既有持倉依自然 SL/TP/反向繼續\n"
                    f"重置時間:明早 08:45 TW"
                )
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

    # ── Phase 6a-lite: FVG shadow observation ─────────────────────
    def _fetch_fvg_signals(self) -> list:
        if FVG_OBSERVE_MODE == "off":
            return []
        try:
            resp = requests.get(FVG_SIGNALS_URL, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            logger.debug(f"FVG fetch failed (silent): {e}")
            return []

    def _observe_fvg_signals(self, signals: list):
        """FVG signal handling per FVG_MODE:
          - 'off'    : skip entirely
          - 'shadow' : log + Telegram on transitions, NO broker calls
          - 'paper'  : also track _fvg_position locally, NO broker calls
          - 'live'   : also place real broker orders

        First poll primes the status map silently to avoid spamming historical entries.
        Subsequent polls notify only on (id, status) transitions.
        """
        if FVG_OBSERVE_MODE not in ("shadow", "paper", "live"):
            return
        mode_tag = FVG_OBSERVE_MODE.upper()
        for sig in signals:
            sig_id = sig.get("id")
            status = sig.get("status", "")
            if not isinstance(sig_id, int) or not status:
                continue
            if self._fvg_last_status.get(sig_id) == status:
                continue
            self._fvg_last_status[sig_id] = status
            if not self._fvg_primed:
                continue  # silent priming on first poll
            dir_ch  = sig.get("dir", "?")
            entry   = sig.get("entry", "?")
            stop    = sig.get("stop", "?")
            target  = sig.get("target", "?")
            pnl     = sig.get("pnl")
            pnl_str = f"  pnl={pnl:+.1f}" if isinstance(pnl, (int, float)) else ""
            logger.info(f"FVG {mode_tag} {status} {dir_ch}@{entry} SL={stop} TP={target}{pnl_str} id={sig_id}")
            self._safe_notify(
                f"👁 <b>[FVG {mode_tag}]</b>  {status}\n"
                f"方向:{dir_ch}  進場 {entry}  停損 {stop}  停利 {target}{pnl_str}"
            )
            # Phase 6c: paper/live modes also track local position + (live) place broker orders
            if FVG_OBSERVE_MODE in ("paper", "live"):
                self._fvg_handle_trade(sig)
        if not self._fvg_primed:
            self._fvg_primed = True
            logger.info(f"FVG {mode_tag} primed: {len(self._fvg_last_status)} initial signal(s) absorbed silently")

    def _fvg_handle_trade(self, sig: dict):
        """Phase 6c-minimum: FVG position lifecycle for paper/live modes.

        Maintains self._fvg_position (Optional, single position because FVG engine is
        single-position state machine). For 'live' mode, also places real broker orders.

        NOT integrated into self._units (MTX path is untouched). On trader restart,
        _fvg_position is lost — known limitation, see project_signal_bus.md notes.
        """
        is_live  = FVG_OBSERVE_MODE == "live"
        sig_id   = sig.get("id")
        status   = sig.get("status", "")
        mode_tag = "LIVE" if is_live else "PAPER"

        if status == "open":
            # Refuse if already have an open FVG position (defensive — FVG engine
            # shouldn't emit a second open while one is alive, but trust no one)
            if self._fvg_position is not None:
                logger.warning(f"FVG {mode_tag} entry {sig_id} ignored — existing position id={self._fvg_position.get('id')}")
                return
            # Refuse if daily MAX LOSS lock is engaged (only blocks LIVE broker calls;
            # PAPER mode still tracks for analytics)
            if is_live and self._trading_day_locked:
                logger.warning(f"FVG LIVE entry {sig_id} rejected — daily MAX LOSS lock active")
                self._safe_notify(f"🛑 [FVG LIVE] 進場訊號被綁手擋下 id={sig_id}")
                return
            # Track + (live) place broker order
            self._fvg_position = {
                "id":        sig_id,
                "dir":       sig.get("dir"),
                "entry":     sig.get("entry"),
                "stop":      sig.get("stop"),
                "target":    sig.get("target"),
                "label":     sig.get("label", "FVG"),
                "opened_at": int(time.time() * 1000),
            }
            if is_live:
                product = self.trader.config["product"]
                if self._fvg_position["dir"] == "long":
                    self._execute_order("BUY", product, 1)
                elif self._fvg_position["dir"] == "short":
                    self._execute_order("SELL", product, 1)
            logger.info(f"FVG {mode_tag} POSITION OPEN id={sig_id} dir={self._fvg_position['dir']} @{self._fvg_position['entry']}")

        elif status in ("profit", "loss", "session_end"):
            # Match by id (must be the position we opened)
            if self._fvg_position is None or self._fvg_position.get("id") != sig_id:
                logger.debug(f"FVG {mode_tag} {status} {sig_id} skipped — no matching local position")
                return
            # Place broker close if live
            if is_live:
                product = self.trader.config["product"]
                if self._fvg_position["dir"] == "long":
                    self._execute_order("SELL", product, 1, opencloseflag="1")
                else:
                    self._execute_order("BUY", product, 1, opencloseflag="1")
            # Accumulate pnl into trading-day counter (whether paper or live —
            # we want the daily lock to consider paper outcomes too, since paper
            # is "what would have happened" and informs whether to keep going)
            pnl_pts = sig.get("pnl") if isinstance(sig.get("pnl"), (int, float)) else 0
            # Persistent trade log + monthly counters
            exit_price = sig.get("meta", {}).get("exit_price")
            self._record_trade(
                source="fvg", label=self._fvg_position.get("label", "FVG"), dir_=self._fvg_position["dir"],
                entry=self._fvg_position["entry"], exit_price=exit_price,
                stop=self._fvg_position["stop"], target=self._fvg_position["target"],
                pnl_pts=pnl_pts, reason=status,
                sig_id=sig_id, opened_at_ms=self._fvg_position.get("opened_at"),
            )
            self._trading_day_pnl_pts += pnl_pts
            if (DAILY_MAX_LOSS_PTS is not None
                    and not self._trading_day_locked
                    and self._trading_day_pnl_pts <= DAILY_MAX_LOSS_PTS):
                self._trading_day_locked = True
                if not self._trading_day_alert_sent:
                    self._trading_day_alert_sent = True
                    self._safe_notify(
                        f"🛑 <b>每日 MAX LOSS 觸發 (via FVG)</b>\n"
                        f"今日損益:{self._trading_day_pnl_pts:+.0f} pts (NT${int(self._trading_day_pnl_pts * POINT_VALUE):,})\n"
                        f"門檻:{DAILY_MAX_LOSS_PTS:+.0f} pts\n"
                        f"動作:trader 已綁手"
                    )
                logger.warning(f"DAILY_MAX_LOSS triggered via FVG: pnl={self._trading_day_pnl_pts:+.0f}")
            logger.info(f"FVG {mode_tag} POSITION CLOSE id={sig_id} reason={status} pnl={pnl_pts:+.1f}pts")
            self._fvg_position = None
