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
import pnl_calc  # additive: real-fill P&L from orders.jsonl (read-only, no execution impact)
from mtx_restore import reconcile_restore, load_mtx_state, save_mtx_state
from margin_headroom import headroom_low
from session_timing import session_summary_action
from exit_reason import stop_hit_reason
from atr_gate import should_skip_code4_atr
from open_freeze import in_open_freeze_window
import fill_emit  # fill-anchoring (Plan B): report real entry fill → Worker /api/fill
from feed_schema import SCHEMA_FAIL, clean_feed
from tick_watchdog import TickStaleWatchdog
import order_reject
import real_fill_pnl  # task B: real-fill P&L fields for trades.jsonl

logger = logging.getLogger(__name__)

# Phase 6b: multi-source signal bus.
# Each source has its own URL, max-units, and (for FVG) operating mode.
# MTX path is unchanged behaviorally — it now just lives under _units['mtx'].
HISTORY_URL     = "https://mtx-monitor.asd261-af5.workers.dev/api/history"
FVG_SIGNALS_URL = "https://mtx-monitor.asd261-af5.workers.dev/api/signals?source=fvg"

SIGNAL_SOURCES = [
    {"source": "mtx", "url": HISTORY_URL},
    {"source": "fvg", "url": FVG_SIGNALS_URL},
]

# Per-source max units. MTX pyramids up to 2, FVG engine is single-position.
MAX_UNITS_PER_SOURCE: Dict[str, int] = {"mtx": 2, "fvg": 1}

# FVG operating mode (env): off | shadow | paper | live
#   off    : skip fetch entirely
#   shadow : fetch + log + Telegram; never touch _units, never call broker
#   paper  : fetch + go through _check_new_signal/_sync_worker_state, but skip broker
#   live   : full pipeline including broker orders
FVG_OBSERVE_MODE = os.getenv("FVG_MODE", "shadow")

# Option C: 30m bias filter for FVG 5m entries. Trader queries the most recent
# 30m signal's meta.bias_dir and only fires 5m signals whose direction aligns.
# - bias_dir=bull → 5m long passes, 5m short absorbed
# - bias_dir=bear → 5m short passes, 5m long absorbed
# - bias_dir=neutral or off → no filter (pass through)
# - bias absent (stale or never fired) → behavior depends on FVG_30M_REQUIRE_BIAS
#   * FVG_30M_REQUIRE_BIAS=0 (default, lenient): pass through
#   * FVG_30M_REQUIRE_BIAS=1 (strict): block
FVG_30M_BIAS_URL       = "https://mtx-monitor.asd261-af5.workers.dev/api/signals?source=fvg_30m&limit=1"
FVG_30M_BIAS_TTL_MIN   = int(os.getenv("FVG_30M_BIAS_TTL_MIN", "1440"))   # 24h
FVG_30M_BIAS_CACHE_SEC = int(os.getenv("FVG_30M_BIAS_CACHE_SEC", "60"))
FVG_30M_REQUIRE_BIAS   = os.getenv("FVG_30M_REQUIRE_BIAS", "0") == "1"

# Regime gate for MTX long entries (manual switch — defaults OFF).
# When enabled, blocks new MTX long entries in confirmed downtrend regime.
# Rule: daily 20-SMA slope (over last 10 days) < 0 AND today's close < (SMA - threshold pts)
#
# OOS validation 2026-05-19 showed this rule does NOT generalize automatically —
# it overfits to crash months (March 2026). Keep as MANUAL kill-switch for use
# during clearly developing crash regimes. Sean flips on visually, off when over.
#
# State: reads daily_closes.json from trader dir. Populate via update_daily_close.py
# sidecar (run after each day-session close, or cron at 13:50 TW).
REGIME_GATE_ENABLED        = os.getenv("REGIME_GATE_DOWNTREND_BLOCK", "0") == "1"
REGIME_GATE_SMA_DAYS       = int(os.getenv("REGIME_GATE_SMA_DAYS", "20"))
REGIME_GATE_SLOPE_DAYS     = int(os.getenv("REGIME_GATE_SLOPE_DAYS", "10"))
REGIME_GATE_THRESHOLD_PTS  = int(os.getenv("REGIME_GATE_THRESHOLD_PTS", "100"))
DAILY_CLOSES_PATH          = Path(__file__).parent / "daily_closes.json"
REGIME_CACHE_SEC           = int(os.getenv("REGIME_CACHE_SEC", "300"))  # re-read every 5 min

# Half-size short side via skip-alternate (manual switch; default OFF).
# At 1-lot granularity true 0.5× sizing is impossible, so for MTX SHORT signals
# whose code is in HALF_SIZE_CODES we take only every 2nd qualifying signal
# (≈50% participation) — approximating half exposure on those codes without
# changing long / other-code size. Data (2026-05-22, 4.5mo) shows ③④ shorts
# have no positive edge across 5/6 months; this trims that bleed while keeping
# optionality (vs a full cut). Set via .env e.g. HALF_SIZE_CODES=3,4 ; empty → off.
HALF_SIZE_CODES = {int(c) for c in os.getenv("HALF_SIZE_CODES", "").split(",") if c.strip().isdigit()}

# Code-4 ATR-gated skip (manual switch; default OFF).
# When set, ④ 轉弱賣出 signals with ATR > threshold are silent-absorbed
# (skipped, no order). Backtest 5/22-5/27 (n=15 ④, n=6 ATR>58) showed
# +348 pts (NTD +17,400) lift. Other codes (⑧/③) have opposite ATR direction
# so this rule is intentionally ④-only. Fail-open on missing ATR.
# Set via .env MTX_SKIP_CODE_4_ATR_GT=58 ; unset/0 → disabled.
# Spec: docs/superpowers/specs/2026-05-27-mtx-skip-code4-high-atr.md
try:
    SKIP_CODE_4_ATR_GT = int(os.getenv("MTX_SKIP_CODE_4_ATR_GT", "0") or "0")
except (ValueError, TypeError):
    SKIP_CODE_4_ATR_GT = 0

# Session-open trading freeze (Sean 2026-05-30). FULL freeze: no entry AND no exit
# in the first OPEN_FREEZE_SECS after a session open (day 08:45, night 15:00 TW) —
# sit out the opening spike. Gates entry/reversal, tick exits, and Worker-driven
# exits. Trade-off (accepted): software stop-loss is OFF during the window, so a
# gap-and-go can exit worse than the stop. Default 300 (5 min, ON); 0 disables.
# Phase-0 (2026-05-30) showed 0/322 real trades currently land in-window, so this
# is a forward guardrail — a no-op until a signal ever fires/exits at the open.
try:
    OPEN_FREEZE_SECS = int(os.getenv("OPEN_FREEZE_SECS", "300") or "0")
except (ValueError, TypeError):
    OPEN_FREEZE_SECS = 0

POLL_INTERVAL = 3     # seconds
SESSION_SUMMARY_DELAY_SEC = 300   # delay session-close summary so bell/session_end trades settle
POINT_VALUE   = 50    # MXF: NT$50 per point
PYRAMID_LOCK  = 15    # pts — first MTX unit stop locked to entry ± this on pyramid

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

# Per-source persistent unit state (trader-side, restart-safe). Both MTX and FVG
# persist their open units locally — authoritative on restart for what the bot
# ACTUALLY holds; the Worker KV is reconciled against it (levels + missed exits),
# never trusted blindly (that caused phantom MTX units — see mtx_restore.py).
FVG_STATE_PATH       = Path(__file__).parent / "fvg_state.json"
MTX_STATE_PATH       = Path(__file__).parent / "mtx_state.json"
PENDING_EXIT_PATH    = Path(__file__).parent / "pending_exit_records.json"  # task B: deferred trade records awaiting real exit fill
EXIT_FILL_TIMEOUT_MS = 60_000   # task B: flush deferred record (exit_fill=null) if no real fill in 60s

# Plan D: broker reconciliation
RECON_CHECK_INTERVAL_SEC = 60   # how often to query broker (be polite to API)
RECON_ALERT_AGE_SEC      = 180  # mismatch must persist > 3 min before alerting

# Shared-account margin headroom: alert when available order-excess margin
# (DMargin.twdordexcess) drops below the floor the bot needs to keep placing
# MXF orders (FUF1239 rejection threshold). 0 disables (no redeploy needed).
MARGIN_HEADROOM_MIN_TWD = float(os.getenv("MARGIN_HEADROOM_MIN_TWD", "100000"))

# Tick-stale watchdog: detect when the dquote tick feed silently stops (alive but blind).
TICK_STALE_DAY_SEC      = int(os.getenv("TICK_STALE_DAY_SEC", "90"))     # liquid day session
TICK_STALE_NIGHT_SEC    = int(os.getenv("TICK_STALE_NIGHT_SEC", "300"))  # thin night session
TICK_CHECK_INTERVAL_SEC = int(os.getenv("TICK_CHECK_INTERVAL_SEC", "30"))
TICK_STALE_KILL_DAY_SEC   = int(os.getenv("TICK_STALE_KILL_DAY_SEC", "180"))
TICK_STALE_KILL_NIGHT_SEC = int(os.getenv("TICK_STALE_KILL_NIGHT_SEC", "600"))
TICK_STALE_KILL_GRACE_SEC = int(os.getenv("TICK_STALE_KILL_GRACE_SEC", "180"))
TICK_STALE_KILL          = os.getenv("TICK_STALE_KILL", "off").lower() == "on"  # Phase B arms os._exit

ENTRY_EMOJI = {"long": "🟢", "short": "🔴"}
EXIT_EMOJI  = {"profit": "✅", "loss": "❌", "reversed": "🔄", "replaced": "🔁", "trail": "🔒",
               "session_end": "🌙"}
SOURCE_TAG  = {"mtx": "[MTX] ", "fvg": "[FVG] "}  # Both sources tagged for Telegram clarity


def _get_session(dt: datetime) -> str:
    t = dt.time()
    if dtime(8, 45) <= t < dtime(13, 45):
        return "day"
    if t >= dtime(15, 0) or t < dtime(5, 0):
        return "night"
    return "break"


class MTXStrategy:
    """
    Phase 6b unified strategy executor. Polls one or more signal sources
    (currently MTX `/api/history` + FVG `/api/signals?source=fvg`) and mirrors
    each source's state via per-source `_units` lists.

    Key design decisions:
    - `_units` is keyed by source. MTX has its own list, FVG has its own list.
      `MAX_UNITS_PER_SOURCE` caps each source independently.
    - For each source per poll: `_sync_worker_state` runs BEFORE `_check_new_signal`,
      so local state clears closed positions before evaluating new signals.
    - `_check_new_signal` scans ALL new trade IDs (not just history[0]),
      oldest-first, to avoid missing intermediate trades.
    - Same-direction skip does NOT update `_last_seen_id`, allowing retry
      after the existing position is closed by sync.
    - FVG_OBSERVE_MODE gates FVG side:
        off    → skip
        shadow → observe-only via _observe_fvg_signals (no _units, no broker)
        paper  → full unified path but skip broker
        live   → full unified path including broker
    """

    def __init__(self, trader, dry_run: bool = True):
        self.trader    = trader
        self.dry_run   = dry_run
        self._tg_token = trader.config.get("telegram_token", "")
        self._tg_chat  = trader.config.get("telegram_chat_id", "")
        # Server/disconnect/reconciliation/MAX-LOSS alerts route to the Health bot.
        # Fall back to the trading bot so a missing-secret deploy doesn't silently
        # drop a server alert.
        self._tg_health_token = trader.config.get("health_telegram_token", "") or self._tg_token
        self._tg_health_chat  = trader.config.get("health_telegram_chat_id", "") or self._tg_chat

        self._lock = threading.Lock()

        # Phase 6b: per-source unit lists. Each unit dict carries its own `source` tag
        # so downstream helpers (_close_unit, _check_exit_unit) can branch on it.
        self._units: Dict[str, List[Dict[str, Any]]] = {"mtx": [], "fvg": []}

        # Phase 6b: per-source last-seen signal id. Independent cursor per source so
        # MTX and FVG histories don't interfere with each other.
        self._last_seen_id: Dict[str, Optional[int]] = {"mtx": None, "fvg": None}

        self._running = False

        # Session tracking
        self._current_session: Optional[str] = None
        self._pending_summary_session: Optional[str] = None  # deferred session-close summary
        self._pending_summary_due:     float         = 0.0   # epoch seconds it becomes due
        self._session_trades:  List[dict]    = []
        self._prev_session_pnl_pts: float    = 0.0
        self._prev_session_label:   str      = ""

        # FVG shadow observation (FVG_MODE='shadow' only): transition detection
        self._fvg_last_status: Dict[int, str] = {}
        self._fvg_primed:       bool          = False  # True after first poll absorbs initial state silently

        # FVG consumer-side boot floor: signals whose id (entry-bar timestamp in ms)
        # is <= boot_ts are silent-absorbed in _check_new_signal to prevent phantom
        # replay of stale KV `status=open` signals after restart. Mirrors the
        # producer-side fix in fvg-trader/fvg/live.py (commit aef3946). Disable
        # with env FVG_BOOT_FLOOR=0.
        self._fvg_boot_ts_ms:        int  = int(time.time() * 1000)
        self._proc_start_ts: float = time.time()  # wall-clock boot, for tick-wd kill grace
        self._fvg_boot_floor_enabled: bool = os.getenv("FVG_BOOT_FLOOR", "1") != "0"

        # Option C: 30m bias filter cache. _get_fvg_30m_bias() queries
        # /api/signals?source=fvg_30m&limit=1 at most once per FVG_30M_BIAS_CACHE_SEC.
        self._fvg_30m_bias_cache:     Optional[str]   = None
        self._fvg_30m_bias_cached_at: float           = 0.0

        # Regime gate cache. _check_regime() re-reads daily_closes.json at most
        # once per REGIME_CACHE_SEC, computes ("uptrend"/"downtrend"/"chop"/"undefined").
        self._regime_cache:           Optional[str]   = None
        self._regime_cached_at:       float           = 0.0

        # Half-size skip-alternate counter: per-code count of qualifying MTX short
        # signals seen, used to take every 2nd one (≈50%). In-memory (resets on
        # restart — acceptable, still ≈50% over time). See HALF_SIZE_CODES.
        self._half_size_seen:         Dict[int, int]  = {}

        # Phase 7: daily MAX LOSS lock state. Counter accumulates across day+night
        # of the same trading day, resets at 08:45 TW (day session open).
        self._trading_day_pnl_pts: float           = 0.0
        self._trading_day_date:    Optional[Any]   = None  # `date` object, None until first poll
        self._trading_day_locked:  bool            = False
        self._trading_day_alert_sent: bool         = False  # one-shot Telegram on lock trigger

        # Persistent trades log + monthly P&L counters. Resets on month change (TW trading-day boundary).
        # Restored from trades.jsonl at startup so restart doesn't lose mid-month state.
        self._current_month:        Optional[str]            = None  # "YYYY-MM"
        self._month_pnl_pts:        float                    = 0.0
        self._month_trades_count:   int                      = 0
        self._month_wins:           int                      = 0
        self._month_losses:         int                      = 0
        self._month_by_source:      Dict[str, Dict[str, Any]] = {}

        # Plan D: broker reconciliation safety net. Periodically (~1 min) compare
        # broker's actual net position vs trader's expected (sum of all _units source lists).
        # Mismatch persisting > RECON_ALERT_AGE_SEC → Telegram alert.
        self._recon_last_check:      float                  = 0.0    # epoch s of last check
        self._recon_mismatch_since:  Optional[float]        = None   # when mismatch first observed
        self._recon_alert_sent:      bool                   = False  # one-shot dedup
        self._recon_schema_alert_sent: bool                 = False  # one-shot dedup for SDK schema drift
        self._recon_last_broker_net: Optional[int]          = None   # last observed broker signed net (for heartbeat)
        self._recon_last_expected:   Optional[int]          = None   # last computed expected signed net
        # Shared-account margin headroom monitor (alert-only, never touches orders)
        self._margin_last_check:     float                  = 0.0    # epoch s of last get_margin
        self._margin_alert_sent:     bool                   = False  # one-shot dedup latch

        # Tick-stale watchdog: detect when the dquote tick feed silently stops delivering
        # (trader alive but blind to price → exits won't fire). Alert-only; see tick_watchdog.py.
        self._tick_wd = TickStaleWatchdog(
            day_threshold=TICK_STALE_DAY_SEC,
            night_threshold=TICK_STALE_NIGHT_SEC,
            kill_day_threshold=TICK_STALE_KILL_DAY_SEC,
            kill_night_threshold=TICK_STALE_KILL_NIGHT_SEC,
            kill_grace=TICK_STALE_KILL_GRACE_SEC,
            check_interval=TICK_CHECK_INTERVAL_SEC,
        )

        # Fill-anchoring (Plan B): FIFO of pending broker fills (entry+exit, in send
        # order) so on_fill() attributes each Match to the right order even on
        # reversals/replaces. Entry fills capture the unit's real entry price and
        # (when FILL_ANCHOR) POST it to the Worker. FILL_ANCHOR default OFF — capture
        # + log only; flip on after verifying capture is correct.
        self._pending_fills: List[dict] = []
        self._pending_exit_records: List[dict] = []   # task B: prepared trade records awaiting real exit fill
        self._fill_anchor: bool         = os.getenv("FILL_ANCHOR", "0") == "1"

        if dry_run:
            logger.info("[DRY RUN] Strategy in simulation mode — no real orders")

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _normalize(raw: dict, source: str) -> dict:
        """Coerce a raw history/signal item into a common shape.

        MTX history (worker /api/history) uses `sigLabel`; FVG signals use `label`.
        Every item gets a `source` tag and a `label` field after normalization.
        """
        out = dict(raw)
        out["source"] = source
        if source == "mtx" and "label" not in out:
            out["label"] = raw.get("sigLabel", "")
        return out

    def _should_place_order(self, source: str) -> bool:
        """Whether to actually hit broker for a given source.
        MTX always does (unless dry_run); FVG only in 'live' mode."""
        if source == "mtx":
            return True
        if source == "fvg":
            return FVG_OBSERVE_MODE == "live"
        return False

    def _flatten_units(self) -> List[Dict[str, Any]]:
        """Snapshot all units across all sources, for cross-source visibility
        (heartbeat, reconciliation, disconnect notifications)."""
        out: List[Dict[str, Any]] = []
        for src_units in self._units.values():
            out.extend(src_units)
        return out

    # ── 啟動 / 停止 ──────────────────────────────────────────────

    def start(self):
        # MTX startup restore — pulls open positions from Worker KV history
        try:
            history = self._fetch_history(HISTORY_URL)
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
                # Reconcile local mtx_state.json vs Worker history to decide what to
                # restore. Local file = what the bot ACTUALLY opened; Worker = what
                # signals fired. Divergence (phantom = Worker-open but no local record)
                # is the bug being fixed: those are lock-refused / HALF_SIZE-skipped
                # units the bot never filled, so we must NOT restore them as positions.
                local_units = load_mtx_state(str(MTX_STATE_PATH))
                skip_restore = os.getenv("MTX_SKIP_RESTORE", "0") == "1"
                if skip_restore:
                    self._last_seen_id["mtx"] = history[0]["id"]
                    logger.info(f"Startup: MTX SKIP_RESTORE — flat boot, last id={self._last_seen_id['mtx']}")
                else:
                    rec = reconcile_restore(local_units, history, cutoff_ms)
                    mtx_cap = MAX_UNITS_PER_SOURCE["mtx"]
                    for u in rec["to_restore"][:mtx_cap]:
                        u = self._normalize(u, "mtx")
                        # Local units store the signal label under "sig_label", and _normalize
                        # blanks "label" (no Worker "sigLabel" present). Recover it so _open_unit
                        # (reads "label") keeps the label on restore.
                        u["label"] = u.get("label") or u.get("sig_label", "")
                        logger.info(f"Startup: restoring MTX id={u['id']} dir={u['dir']} "
                                    f"(local-confirmed, no order placed)")
                        self._open_unit(u, source="mtx", notify=False, place_order=False)
                    for unit, worker in rec["to_record_exit"]:
                        self._record_missed_exit(unit, worker)
                    for pid in rec["skipped_phantoms"]:
                        logger.warning(f"Startup: SKIP phantom MTX id={pid} "
                                       f"(Worker-open but bot never filled — not restored)")
                    if rec["dropped_stale"]:
                        logger.info(f"Startup: dropped {len(rec['dropped_stale'])} stale local MTX unit(s)")
                    self._save_mtx_state()   # persist the reconciled set (drops recorded-exit + stale)
                    self._last_seen_id["mtx"] = history[0]["id"]
                    logger.info(f"Startup: MTX restored {len(self._units['mtx'])}, "
                                f"recorded {len(rec['to_record_exit'])} missed-exit, "
                                f"skipped {len(rec['skipped_phantoms'])} phantom")
        except Exception as e:
            logger.warning(f"Startup MTX fetch failed: {e}")

        now = datetime.now(TZ_TW)
        self._current_session = _get_session(now)
        logger.info(f"Current session: {self._current_session}")

        # Restore current-month P&L counters from trades.jsonl (so restart preserves state)
        self._restore_month_from_log()
        # Restore FVG units from disk (so trader restart doesn't lose track of an open FVG trade)
        self._load_fvg_state()

        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info(f"MTXStrategy started | dry_run={self.dry_run} | poll={POLL_INTERVAL}s | "
                    f"sources={[s['source'] for s in SIGNAL_SOURCES]} | fvg_mode={FVG_OBSERVE_MODE} | "
                    f"fvg_boot_ts={self._fvg_boot_ts_ms} "
                    f"(FVG opens with id<=this are silent-absorbed; floor={'on' if self._fvg_boot_floor_enabled else 'OFF'}) | "
                    f"30m_bias_filter=on (require={FVG_30M_REQUIRE_BIAS}, ttl={FVG_30M_BIAS_TTL_MIN}min, cache={FVG_30M_BIAS_CACHE_SEC}s) | "
                    f"regime_gate={'ON' if REGIME_GATE_ENABLED else 'off'} "
                    f"(SMA={REGIME_GATE_SMA_DAYS}d slope={REGIME_GATE_SLOPE_DAYS}d thr={REGIME_GATE_THRESHOLD_PTS}pts; "
                    f"daily_closes={'present' if DAILY_CLOSES_PATH.exists() else 'MISSING'}) | "
                    f"fill_anchor={'ON' if self._fill_anchor else 'off'}")

    def stop(self):
        self._running = False

    # ── 斷線 / 重連 ───────────────────────────────────────────────

    def on_disconnect(self):
        with self._lock:
            units = self._flatten_units()
        logger.warning(f"Disconnected | local units={len(units)}")
        # Weekend: market closed → broker disconnect expected, suppress Telegram notify
        if datetime.now(TZ_TW).weekday() >= 5:
            logger.info("Disconnect notify suppressed (weekend)")
            return
        if units:
            pos_desc = ", ".join(
                f"{'多' if u['dir'] == 'long' else '空'}@{u['entry']}[{u.get('source','?').upper()}]" for u in units
            )
            dry_tag = " [模擬]" if self.dry_run else ""
            threading.Thread(target=self._safe_health_notify, args=(
                f"⚠️ <b>斷線警告{dry_tag}</b>\n"
                f"本地倉位:{pos_desc}\n正在等待重連...",
            ), daemon=True).start()

    def on_reconnect(self, broker_pos: Optional[dict] = None):
        with self._lock:
            units = self._flatten_units()

        dry_tag   = " [模擬]" if self.dry_run else ""
        # Use net direction across all sources for mismatch check (signed net = #long − #short)
        net = sum(1 if u["dir"] == "long" else -1 for u in units)
        local_dir = "long" if net > 0 else ("short" if net < 0 else None)
        logger.warning(f"Reconnected | local_units={len(units)} net={net} | broker={broker_pos}")

        # Weekend: market closed → suppress Telegram notify (same rationale as on_disconnect)
        if datetime.now(TZ_TW).weekday() >= 5:
            logger.info("Reconnect notify suppressed (weekend)")
            return

        if broker_pos is SCHEMA_FAIL:
            logger.error("Reconnect: broker position schema drift — cannot verify alignment")
            threading.Thread(target=self._safe_health_notify, args=(
                f"⚠️ <b>重連無法核對倉位{dry_tag}</b>\n"
                f"券商持倉回應欄位漂移，無法判讀。本地 net={net}。\n請手動確認券商實際倉位!",
            ), daemon=True).start()
            return

        if broker_pos:
            broker_dir = "long" if broker_pos.get("bs") == "B" else "short"
            mismatch   = (local_dir != broker_dir)
        else:
            mismatch = False

        if mismatch:
            b_zh = "多" if broker_dir == "long" else "空"
            l_zh = "多" if local_dir == "long" else ("空" if local_dir == "short" else "無")
            threading.Thread(target=self._safe_health_notify, args=(
                f"🚨 <b>重連倉位不一致{dry_tag}</b>\n"
                f"本地:{l_zh}(net={net})\n券商:{b_zh}\n請立即手動確認!",
            ), daemon=True).start()
        elif units:
            pos_desc = ", ".join(
                f"{'多' if u['dir'] == 'long' else '空'}@{u['entry']}[{u.get('source','?').upper()}]" for u in units
            )
            threading.Thread(target=self._safe_health_notify, args=(
                f"✅ <b>重連成功{dry_tag}</b>\n倉位確認:{pos_desc}(券商一致)",
            ), daemon=True).start()
        else:
            logger.info("Reconnect: no open position, no action needed")

    # ── Session-open freeze 判定 ──────────────────────────────────

    def _in_open_freeze(self) -> bool:
        """True during the session-open trading freeze (first OPEN_FREEZE_SECS of
        the day/night open). When True, ALL order paths are suppressed — entry,
        reversal, tick exit, and Worker-driven exit. See open_freeze.py."""
        return in_open_freeze_window(datetime.now(TZ_TW), OPEN_FREEZE_SECS)

    # ── 行情 tick 回調 ────────────────────────────────────────────

    def on_tick(self, price: float):
        self._tick_wd.record_tick(time.time())   # stamp BEFORE the flat early-return below
        # Session-open freeze: skip exit checks too (carried position waits out the
        # opening spike). Watchdog stamp above is kept so the freeze doesn't look
        # like a dead feed. Stop-loss is intentionally dormant during the window.
        if self._in_open_freeze():
            return
        with self._lock:
            all_units = self._flatten_units()
            if not all_units:
                return
            for unit in all_units:
                self._check_exit_unit(unit, price)

    # ── 成交回報(fill-anchoring, Plan B)──────────────────────────
    def on_fill(self, productid: str, bs: str, price: float):
        """Called from trader._on_match (broker thread). Attribute this fill to the
        front pending order (FIFO, send order). Entry fills capture the unit's real
        entry price; when FILL_ANCHOR is on, report MTX entry fills to the Worker so
        it re-anchors stop/target to the real entry. Exit fills are consumed (FIFO
        alignment) but not acted on (Layer ① derives real exit P&L from orders.jsonl)."""
        if productid != self.trader.config.get("product"):
            return  # not our product (e.g. a manual trade in another contract)
        with self._lock:
            if not self._pending_fills or self._pending_fills[0]["bs"] != bs:
                return  # front doesn't match → likely a manual/foreign fill; leave queue intact
            pend = self._pending_fills.pop(0)
            if pend["kind"] != "entry":
                # Task B: exit fill — stamp the real exit price onto the deferred record
                # and write trades.jsonl now. (Layer ① still owns realised P&L.)
                pe = pend.get("pe")
                if pe is not None and pe in self._pending_exit_records:
                    self._pending_exit_records.remove(pe)
                    rec = real_fill_pnl.finalize_exit(pe["record"], price)
                    self._record_trade(**rec)
                    self._save_pending_exit_records()
                return
            unit = pend["unit"]
            if unit.get("entry_fill") is not None:
                return
            unit["entry_fill"] = price
            sig_entry = unit.get("entry")
            slip = round(price - sig_entry) if sig_entry else 0
            logger.info(f"Entry fill | {pend['source']} id={pend['id']} {unit['dir']} "
                        f"signal_entry={sig_entry} fill={price} slip={slip:+d} "
                        f"anchor={'on' if self._fill_anchor else 'off'}")
            if self._fill_anchor and pend["source"] == "mtx":
                fill_emit.send({"source": "mtx", "id": pend["id"], "fill_price": price})

    def _flush_due_exit_records(self):
        """Task B safety net: flush deferred trade records whose real exit fill never
        arrived within EXIT_FILL_TIMEOUT_MS → write with exit_fill=null + warn. Worst
        case is the same information as before this feature (no row is ever lost)."""
        with self._lock:
            now_ms = int(time.time() * 1000)
            due = real_fill_pnl.due_records(self._pending_exit_records, now_ms)
            if not due:
                return
            for pe in due:
                self._pending_exit_records.remove(pe)
                rec = real_fill_pnl.finalize_exit(pe["record"], None)
                self._record_trade(**rec)
                logger.warning(
                    f"[real-fill] exit fill timeout (>{EXIT_FILL_TIMEOUT_MS // 1000}s) "
                    f"src={rec.get('source')} id={rec.get('id')} reason={rec.get('reason')} "
                    f"→ wrote exit_fill=null")
            self._save_pending_exit_records()

    def on_order_rejected(self, productid: str, bs: str, orderstatus: str):
        """Called from trader._on_reply (broker thread) when a reply is a rejection.
        Roll back the optimistic unit so no phantom unit / phantom P&L lingers."""
        with self._lock:
            unit = order_reject.rollback_rejected_entry(
                self._pending_fills, self._units, productid, bs,
                self.trader.config.get("product"),
            )
        if unit:
            logger.warning(
                f"[order-rejected] source={unit['source']} dir={unit['dir']} "
                f"id={unit['id']} status={orderstatus} → unit rolled back (no fill, no P&L)"
            )

    # ── Poll 迴圈 ─────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            # Weekend filter: market closed Sat/Sun → broker unreachable, any trading
            # logic is wasted at best, ghost-record-polluting at worst. Skip core trading
            # paths but keep heartbeat + recon running (so watchdog stays happy).
            is_weekend = datetime.now(TZ_TW).weekday() >= 5
            try:
                self._check_session_change()
                self._check_trading_day_reset()   # Phase 7: reset daily P&L counter at 08:45 TW
                self._check_daily_loss_lock()     # Phase 7: daily MAX LOSS lock from REAL P&L (restart-safe)
                self._flush_due_exit_records()    # task B: flush deferred records past 60s timeout
                if not is_weekend:
                    for src_info in SIGNAL_SOURCES:
                        source = src_info["source"]
                        # FVG can be turned off globally via env
                        if source == "fvg" and FVG_OBSERVE_MODE == "off":
                            continue
                        try:
                            raw = self._fetch_history(src_info["url"])
                        except Exception as e:
                            # MTX failure is loud (trader core), FVG failure is quiet
                            if source == "mtx":
                                logger.error(f"MTX history fetch error: {e}")
                            else:
                                logger.debug(f"FVG signals fetch error (silent): {e}")
                            continue
                        history = [self._normalize(t, source) for t in raw if isinstance(t, dict)]
                        # Shadow mode: observation-only path for FVG (no _units update, no broker)
                        if source == "fvg" and FVG_OBSERVE_MODE == "shadow":
                            self._observe_fvg_signals(history)
                            continue
                        try:
                            self._sync_worker_state(history, source)  # ① sync exits first — clears closed positions
                            self._check_new_signal(history, source)   # ② then pick up new signals with fresh state
                        except Exception as e:
                            # FVG errors must never break MTX; MTX errors loud
                            if source == "mtx":
                                logger.error(f"MTX signal handling error: {e}")
                            else:
                                logger.debug(f"FVG signal handling error (silent): {e}")
            except Exception as e:
                logger.error(f"Poll error: {e}")
            # Plan D: broker reconciliation safety net (throttled to ~1 min via internal check)
            try:
                self._check_broker_reconciliation()
            except Exception as e:
                logger.debug(f"recon error (silent): {e}")
            # Shared-account margin-headroom monitor (alert-only, ~1 min throttle internally)
            try:
                self._check_margin_headroom()
            except Exception as e:
                logger.debug(f"margin headroom error (silent): {e}")
            # Tick-stale watchdog: detect if the dquote feed goes silent during an active
            # session (self-throttled + session/weekend-gated inside check()).
            # PHASE 1 (observe-only): route alerts to the log, NOT Telegram — validates
            # thresholds/gating live on viploginm with zero noise and zero trading impact.
            # PHASE 2: change the notify callback to self._safe_health_notify for real alerts.
            try:
                self._tick_wd.check(
                    time.time(), self._current_session,
                    datetime.now(TZ_TW).weekday() >= 5,
                    lambda m: logger.warning(f"[tick-wd OBSERVE] {m}"),  # PHASE 2: -> self._safe_health_notify
                    uptime=time.time() - self._proc_start_ts,
                    on_kill=self._tick_wd_kill,
                )
            except Exception as e:
                logger.debug(f"tick watchdog error (silent): {e}")
            # Fire-and-forget heartbeat — outside try/except so it fires even when poll itself
            # errors (watchdog needs to see "process alive but polls failing" as a signal).
            mtx_units = self._units.get("mtx", [])
            fvg_units = self._units.get("fvg", [])
            heartbeat.send({
                "ts":                  int(time.time() * 1000),
                "pid":                 os.getpid(),
                "session":             self._current_session,
                "units":               len(mtx_units) + len(fvg_units),  # back-compat total
                "units_mtx":           len(mtx_units),
                "units_fvg":           len(fvg_units),
                "last_seen_id":        self._last_seen_id.get("mtx"),    # back-compat = MTX cursor
                "last_seen_id_mtx":    self._last_seen_id.get("mtx"),
                "last_seen_id_fvg":    self._last_seen_id.get("fvg"),
                "trading_day_pnl_pts": self._trading_day_pnl_pts,
                "trading_day_locked":  self._trading_day_locked,
                "fvg_mode":            FVG_OBSERVE_MODE,
                "fvg_position_id":     fvg_units[0].get("id") if fvg_units else None,
                "month":               self._current_month,
                "month_pnl_pts":       self._month_pnl_pts,
                "month_trades_count":  self._month_trades_count,
                "recon_broker_net":    self._recon_last_broker_net,
                "recon_expected_net":  self._recon_last_expected,
                "recon_alert_sent":    self._recon_alert_sent,
                "last_tick_age_sec":   self._tick_wd.last_tick_age(time.time()),
                # Additive real-fill P&L (broker Match prices, FIFO from orders.jsonl).
                # Coexists with the signal-based trading_day_pnl_pts/month_pnl_pts above.
                # Scoped to the bot's own contract so manual trades in other months
                # (shared account) don't pollute the reported real P&L.
                **pnl_calc.heartbeat_fields(base=self.trader.config["product"]),
            })
            time.sleep(POLL_INTERVAL)

    def _check_session_change(self):
        now     = datetime.now(TZ_TW)
        session = _get_session(now)
        # Defer the session-close summary ~5 min so bell/session_end closes land in
        # _session_trades first. session_summary_action runs every poll (no early-return).
        act = session_summary_action(
            self._current_session, session,
            self._pending_summary_session, self._pending_summary_due,
            time.time(), SESSION_SUMMARY_DELAY_SEC,
        )
        if act["fire"] is not None:
            self._send_session_summary(act["fire"])
            self._session_trades = []
        self._pending_summary_session = act["pending_session"]
        self._pending_summary_due     = act["due_at"]
        if session != self._current_session:
            self._current_session = session
            if session in ("day", "night"):
                logger.info(f"{'日盤' if session == 'day' else '夜盤'}開始")
                threading.Thread(target=self._send_open_notify, args=(session,), daemon=True).start()

    @staticmethod
    def _compute_trading_day(now: datetime) -> date:
        """Trading day boundary at 08:45 TW. Before 08:45 = yesterday's trading day."""
        return (now - timedelta(days=1)).date() if now.time() < dtime(8, 45) else now.date()

    def _record_trade(self, *, source: str, label: str, dir_: str, entry, exit_price,
                       stop, target, pnl_pts: float, reason: str, sig_id, opened_at_ms,
                       entry_fill=None, exit_fill=None, pnl_pts_real=None):
        """Append one trade record to trades.jsonl AND update monthly counters.

        Called from _close_unit for every closed unit regardless of source.
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
                "entry_fill":   entry_fill,
                "exit_fill":    exit_fill,
                "pnl_pts_real": pnl_pts_real,
                "reason":       reason,
                "duration_sec": duration_sec,
            }
            with open(TRADES_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # Cloud backup (Worker /api/trade_log, Worker 端 dedup by (id, reason))
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

    def _load_fvg_state(self) -> None:
        """Restore _units['fvg'] from disk (Plan A trader-side companion).

        Trader restart used to drop the FVG position → broker FVG positions
        becoming untracked. With this, restart restores trader's view
        and Plan D recon still validates against broker reality.

        Backwards compat: old schema `{"fvg_position": <single dict or null>}`
        is migrated transparently to `{"fvg_units": [<list>]}` on next save.
        """
        if not FVG_STATE_PATH.exists():
            return
        try:
            data = json.loads(FVG_STATE_PATH.read_text())
            # New schema
            units = data.get("fvg_units")
            if units is None:
                # Legacy schema migration
                legacy_pos = data.get("fvg_position")
                units = [legacy_pos] if legacy_pos else []
            if units:
                # Ensure each restored unit has a 'source' tag for downstream branches
                for u in units:
                    if isinstance(u, dict):
                        u.setdefault("source", "fvg")
                        self._units["fvg"].append(u)
                        logger.info(
                            f"FVG state restored: id={u.get('id')} "
                            f"dir={u.get('dir')} entry={u.get('entry')}"
                        )
        except Exception as e:
            logger.warning(f"FVG state load failed (continuing fresh): {e}")
            self._units["fvg"] = []

    def _save_fvg_state(self) -> None:
        """Atomic write of _units['fvg'] to disk. Called after every FVG unit
        open/close in _open_unit / _close_unit so disk reflects live state crash-safely."""
        try:
            data = {"fvg_units": list(self._units.get("fvg", []))}
            tmp = FVG_STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(FVG_STATE_PATH)
        except Exception as e:
            logger.error(f"FVG state save failed: {e}")

    def _save_mtx_state(self) -> None:
        """Atomic write of _units['mtx'] to disk — the bot's authoritative record of
        which MTX units it actually opened. Read at startup by the restore reconciler."""
        try:
            save_mtx_state(str(MTX_STATE_PATH), self._units.get("mtx", []))
        except Exception as e:
            logger.error(f"MTX state save failed: {e}")

    def _record_missed_exit(self, unit: dict, worker: dict) -> None:
        """A locally-held MTX unit the Worker shows already closed (exited while the bot
        was down). Record the trade once; do NOT restore it as open (no double-count)."""
        exit_price = worker.get("exit", worker.get("exitPrice"))
        reason = worker.get("status", "session_end")
        entry = unit.get("entry")
        # Compute pnl_pts from entry/exit if both numeric; fallback to 0.0
        try:
            if entry is not None and exit_price is not None:
                raw = float(exit_price) - float(entry)
                pnl_pts = raw if unit.get("dir") == "long" else -raw
            else:
                pnl_pts = 0.0
        except (TypeError, ValueError):
            pnl_pts = 0.0
        try:
            self._record_trade(
                source="mtx",
                label=unit.get("sig_label", unit.get("label", "")),
                dir_=unit.get("dir", ""),
                entry=entry,
                exit_price=exit_price,
                stop=unit.get("stop"),
                target=unit.get("target"),
                pnl_pts=pnl_pts,
                reason=reason,
                sig_id=unit.get("id"),
                opened_at_ms=unit.get("opened_at"),
            )
            logger.info(f"Startup: recorded missed MTX exit id={unit.get('id')} reason={reason}")
        except Exception as e:
            logger.warning(f"Startup: missed-exit record failed id={unit.get('id')}: {e}")

    def _expected_net_position(self) -> int:
        """Trader's expected signed net lots = sum across all source unit lists."""
        net = 0
        for units in self._units.values():
            for u in units:
                net += 1 if u["dir"] == "long" else -1
        return net

    def _check_broker_reconciliation(self):
        """Plan D safety net: compare broker's actual net position vs trader's
        expected net. Persistent mismatch (>3 min) → Telegram alert.

        Catches drift scenarios like the 2026-05-16 FVG bug where broker has
        a position but trader's FVG units list is empty (or vice versa).
        Throttled to once per RECON_CHECK_INTERVAL_SEC (~1 min) to be API-polite.

        Skipped when broker API likely unreliable:
        - Session is "break" (intraday break 13:45-15:00 or overnight 05:00-08:45)
        - Weekend (Saturday/Sunday TW) — market fully closed, broker API may
          return empty positions even if real positions are held.

        Existing mismatch state (_recon_mismatch_since) is preserved across the
        skip so when session resumes the tolerance window picks up where it
        left off — but stale state can self-reset on first match after resume.
        """
        # Don't bother during break / unknown sessions or weekends
        if self._current_session not in ("day", "night"):
            return
        if datetime.now(TZ_TW).weekday() >= 5:  # 5=Saturday, 6=Sunday
            return

        now = time.time()
        if now - self._recon_last_check < RECON_CHECK_INTERVAL_SEC:
            return
        self._recon_last_check = now

        try:
            broker_pos = self.trader._query_broker_position()
        except Exception as e:
            logger.debug(f"recon: broker query failed (silent): {e}")
            return

        # SDK schema drift: the response shape can't be trusted. Pause recon this
        # cycle — do NOT fall through to net=0, which reads as "flat" and would
        # false-alert on every genuinely-held position (the 2026-05 recon bug).
        if broker_pos is SCHEMA_FAIL:
            logger.error("recon: broker position schema drift — recon paused this cycle")
            if not self._recon_schema_alert_sent:
                self._recon_schema_alert_sent = True
                self._safe_health_notify(
                    "⚠️ <b>Broker 對帳暫停</b>\n"
                    "券商持倉回應欄位漂移（schema drift），無法可靠判讀 net。\n"
                    "對帳已暫停（不誤報），請檢查 SDK / DPosition 欄位是否變動。"
                )
            return
        # Good read — clear the schema-drift alert latch.
        self._recon_schema_alert_sent = False

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
                self._safe_health_notify(
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
            # Build per-source context for alert
            mtx_units = self._units.get("mtx", [])
            fvg_units = self._units.get("fvg", [])
            mtx_count = len(mtx_units)
            mtx_long  = sum(1 for u in mtx_units if u["dir"] == "long")
            mtx_short = mtx_count - mtx_long
            if fvg_units:
                fvg_str = ", ".join(f"{u['dir']} (id={u['id']})" for u in fvg_units)
            else:
                fvg_str = "(無)"
            logger.error(f"DAILY_RECON_ALERT: broker_net={broker_net} expected_net={expected_net} age={age:.0f}s")
            self._safe_health_notify(
                f"⚠️ <b>Broker 對帳異常 {int(age/60)} 分鐘</b>\n"
                f"Trader 預期: <b>{expected_net} 口</b>\n"
                f"  MTX _units: {mtx_count} ({mtx_long} long / {mtx_short} short)\n"
                f"  FVG _units: {fvg_str}\n"
                f"Broker 實際: <b>{broker_net} 口</b>\n"
                f"差距: <b>{broker_net - expected_net:+d} 口</b>\n"
                f"建議:檢查是否 FVG bot engine 漏 close 訊號(見 [[feedback-fvg-engine-multi-open-bug]]),"
                f"或執行 /flat-position skill 手動對齊"
            )

    def _check_margin_headroom(self):
        """Shared-account margin-headroom safety net (alert-only).

        Account 0239174 is shared with Sean's manual trades. When his manual
        positions drain the account's available order-excess margin, the bot's
        MXF orders get rejected by the broker (FUF1239). This periodically reads
        the broker's available order-excess margin (DMargin.twdordexcess) and
        warns the Health bot when it falls below MARGIN_HEADROOM_MIN_TWD, so Sean
        can top up / trim before the bot silently misses entries.

        READ-ONLY — never touches orders or _units. Same gating/cadence as recon
        (skip break/weekend, ~1 min throttle). One-shot dedup + recovery notify.
        See [[project-shared-account-margin-contention]].
        """
        if MARGIN_HEADROOM_MIN_TWD <= 0:   # feature disabled
            return
        if self._current_session not in ("day", "night"):
            return
        if datetime.now(TZ_TW).weekday() >= 5:  # 5=Sat, 6=Sun
            return
        now = time.time()
        if now - self._margin_last_check < RECON_CHECK_INTERVAL_SEC:
            return
        self._margin_last_check = now

        excess = self.trader._query_broker_margin_excess("TWD")
        if excess is None:
            # No reliable read — fail-safe, do NOT alert and do NOT clear latch.
            logger.debug("margin headroom: no reliable read this cycle")
            return

        if headroom_low(excess, MARGIN_HEADROOM_MIN_TWD):
            if not self._margin_alert_sent:
                self._margin_alert_sent = True
                logger.warning(
                    f"MARGIN_HEADROOM_LOW: ordexcess={excess:.0f} "
                    f"floor={MARGIN_HEADROOM_MIN_TWD:.0f}"
                )
                self._safe_health_notify(
                    f"⚠️ <b>可用保證金 headroom 不足</b>\n"
                    f"可委託超額保證金剩 <b>NT${excess:,.0f}</b>(門檻 NT${MARGIN_HEADROOM_MIN_TWD:,.0f})\n"
                    f"bot 新單恐被 FUF1239 拒(共用帳號保證金競爭)。\n"
                    f"請入金或減少手動部位以恢復 headroom。"
                )
        else:
            if self._margin_alert_sent:
                self._margin_alert_sent = False
                logger.info(f"margin headroom recovered: ordexcess={excess:.0f}")
                self._safe_health_notify(
                    f"✅ <b>保證金 headroom 已恢復</b>\n"
                    f"可委託超額保證金 <b>NT${excess:,.0f}</b>(門檻 NT${MARGIN_HEADROOM_MIN_TWD:,.0f})"
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
            self._safe_health_notify(
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

    def _check_daily_loss_lock(self):
        """Set the daily MAX LOSS lock from the REAL-account daily P&L.

        Source is pnl_calc (FIFO over the persistent orders.jsonl broker fills,
        windowed to the 08:45 TW trading day) — so this is RESTART-SAFE: after a
        restart while down, the next poll re-derives real P&L from fills and
        re-locks immediately; it clears at 08:45 when _check_trading_day_reset
        flips the flag and pnl_calc's day window slides to the new day.
        Fail-open on a transient None read (leave lock state unchanged).
        Scope is the bot's resolved contract only (config["product"], e.g. MXFF6):
        the shared broker account also logs Sean's MANUAL trades in other months
        (e.g. MXFG6) into orders.jsonl — those must NOT trip the bot's lock.
        (FVG runs paper, never hits orders.jsonl.)
        """
        if DAILY_MAX_LOSS_PTS is None or self._trading_day_locked:
            return
        try:
            real = pnl_calc.heartbeat_fields(
                base=self.trader.config["product"]
            ).get("real_trading_day_pnl_pts")
        except Exception as e:
            logger.debug(f"daily-loss lock: real P&L read failed (skip): {e}")
            return
        if real is None or real > DAILY_MAX_LOSS_PTS:
            return
        self._trading_day_locked = True
        logger.warning(f"DAILY_MAX_LOSS triggered: real_day_pnl={real:+.0f} ≤ {DAILY_MAX_LOSS_PTS:+.0f}")
        if not self._trading_day_alert_sent:
            self._trading_day_alert_sent = True
            self._safe_health_notify(
                f"🛑 <b>每日 MAX LOSS 觸發</b>\n"
                f"今日真倉損益:{real:+.0f} pts (NT${int(real * POINT_VALUE):,})\n"
                f"門檻:{DAILY_MAX_LOSS_PTS:+.0f} pts\n"
                f"動作:trader 已綁手,拒收新進場/加碼訊號\n"
                f"既有持倉依自然 SL/TP/反向繼續\n"
                f"重置時間:明早 08:45 TW(日盤前)"
            )

    # ── 30m bias filter (Option C) ────────────────────────────────

    def _get_fvg_30m_bias(self) -> Optional[str]:
        """Return the bias_dir ('bull'/'bear'/'neutral'/'off') from the most
        recent 30m FVG signal, or None if no signal within FVG_30M_BIAS_TTL_MIN.

        Cached for FVG_30M_BIAS_CACHE_SEC to avoid per-poll fetches. On transient
        fetch error, returns last-cached value rather than None so a network blip
        doesn't accidentally flip the filter behavior mid-session.
        """
        now = time.time()
        if now - self._fvg_30m_bias_cached_at < FVG_30M_BIAS_CACHE_SEC:
            return self._fvg_30m_bias_cache
        try:
            data = self._fetch_history(FVG_30M_BIAS_URL)
            if not data:
                bias = None
            else:
                sig = data[0]
                age_ms = (now * 1000) - sig.get("id", 0)
                if age_ms > FVG_30M_BIAS_TTL_MIN * 60 * 1000:
                    bias = None
                else:
                    bias = (sig.get("meta") or {}).get("bias_dir")
            self._fvg_30m_bias_cache = bias
            self._fvg_30m_bias_cached_at = now
            return bias
        except Exception as e:
            logger.warning(f"30m bias fetch failed: {e} (keeping last cached={self._fvg_30m_bias_cache})")
            self._fvg_30m_bias_cached_at = now  # rate-limit error log
            return self._fvg_30m_bias_cache

    # ── Regime gate (manual switch) ──────────────────────────────

    def _check_regime(self) -> str:
        """Returns 'uptrend' | 'downtrend' | 'chop' | 'undefined'.

        Reads daily_closes.json (list of {date, close}). Computes 20-day SMA,
        slope over last 10 days, distance from SMA. Cached REGIME_CACHE_SEC.
        Defensive: returns 'undefined' on any error (missing file, short history,
        bad data). Caller must check enabled flag separately.
        """
        now = time.time()
        if now - self._regime_cached_at < REGIME_CACHE_SEC:
            return self._regime_cache or "undefined"
        try:
            if not DAILY_CLOSES_PATH.exists():
                self._regime_cache = "undefined"
                self._regime_cached_at = now
                return "undefined"
            with open(DAILY_CLOSES_PATH) as f:
                data = json.load(f)
            # Expected: [{"date": "YYYY-MM-DD", "close": 40500}, ...]
            # Sort by date ascending, take last N where N = SMA + slope_window
            data.sort(key=lambda x: x["date"])
            needed = REGIME_GATE_SMA_DAYS + REGIME_GATE_SLOPE_DAYS
            if len(data) < needed:
                logger.debug(f"regime gate: only {len(data)} daily closes, need {needed}")
                self._regime_cache = "undefined"
                self._regime_cached_at = now
                return "undefined"
            closes = [float(d["close"]) for d in data]
            # SMA over last N=SMA_DAYS
            sma_now = sum(closes[-REGIME_GATE_SMA_DAYS:]) / REGIME_GATE_SMA_DAYS
            sma_old = sum(closes[-REGIME_GATE_SMA_DAYS - REGIME_GATE_SLOPE_DAYS:
                                 -REGIME_GATE_SLOPE_DAYS]) / REGIME_GATE_SMA_DAYS
            slope = sma_now - sma_old
            dist = closes[-1] - sma_now
            if slope < 0 and dist < -REGIME_GATE_THRESHOLD_PTS:
                regime = "downtrend"
            elif slope > 0 and dist > REGIME_GATE_THRESHOLD_PTS:
                regime = "uptrend"
            else:
                regime = "chop"
            self._regime_cache = regime
            self._regime_cached_at = now
            return regime
        except Exception as e:
            logger.warning(f"regime gate compute failed: {e} (defaulting to undefined)")
            self._regime_cache = "undefined"
            self._regime_cached_at = now
            return "undefined"

    # ── 新訊號偵測 ────────────────────────────────────────────────

    def _check_new_signal(self, history: list, source: str):
        """Scan a source's history for new 'open' signals. Source-aware:
        - MTX: same-dir pyramid up to MAX_UNITS_PER_SOURCE['mtx']; opposite → reverse
        - FVG: max 1 unit, defensive refusal if any unit already open
        """
        if not history:
            return

        cutoff   = self._last_seen_id.get(source) or 0
        max_cap  = MAX_UNITS_PER_SOURCE.get(source, 1)
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
                self._last_seen_id[source] = trade_id
                continue

            # Session-open freeze: no entry/reversal in the first OPEN_FREEZE_SECS
            # of a session open (Sean 2026-05-30). Silent-absorb (consume) the
            # signal — skip, not defer, so we don't chase a stale entry once the
            # window clears. Covers MTX + FVG, new entries + reversals + pyramids.
            if self._in_open_freeze():
                logger.info(
                    f"Session-open freeze | {source} entry {trade_id} silent-absorbed "
                    f"(first {OPEN_FREEZE_SECS}s of session open)"
                )
                self._last_seen_id[source] = trade_id
                continue

            # FVG consumer-side boot floor: silent-absorb stale KV `status=open`
            # signals replayed at boot. Without this, restart re-fires any FVG
            # signal the producer never closed (e.g. producer-crash leftovers),
            # causing phantom Unit 1 in trader state.
            if source == "fvg" and self._fvg_boot_floor_enabled and trade_id <= self._fvg_boot_ts_ms:
                logger.info(
                    f"FVG pre-boot signal {trade_id} silent-absorbed "
                    f"(boot_ts={self._fvg_boot_ts_ms})"
                )
                self._last_seen_id[source] = trade_id
                continue

            # Option C: 30m bias filter for FVG 5m entries. Direction must align
            # with the most recent 30m sidecar bias_dir.
            if source == "fvg":
                bias = self._get_fvg_30m_bias()
                if bias is None:
                    if FVG_30M_REQUIRE_BIAS:
                        logger.info(f"FVG signal {trade_id} silent-absorbed: no 30m bias (strict mode)")
                        self._last_seen_id[source] = trade_id
                        continue
                    # lenient: bias absent, fall through
                elif bias in ("bull", "bear"):
                    bias_long = "long" if bias == "bull" else "short"
                    if direction != bias_long:
                        logger.info(
                            f"FVG signal {trade_id} silent-absorbed: dir={direction} "
                            f"mismatches 30m bias={bias}"
                        )
                        self._last_seen_id[source] = trade_id
                        continue
                # bias in ("neutral", "off") → no directional filter, fall through

            # Regime gate (manual switch; default OFF). Blocks MTX long entries
            # in confirmed downtrend regime. See REGIME_GATE_* env vars.
            # Note: only blocks NEW entries (no pyramid handling here — pyramid
            # path returns earlier; this gate fires before _enter).
            if (REGIME_GATE_ENABLED and source == "mtx" and direction == "long"):
                regime = self._check_regime()
                if regime == "downtrend":
                    logger.info(
                        f"MTX long signal {trade_id} silent-absorbed by regime gate "
                        f"(downtrend; SMA={REGIME_GATE_SMA_DAYS}d slope={REGIME_GATE_SLOPE_DAYS}d thr={REGIME_GATE_THRESHOLD_PTS})"
                    )
                    self._last_seen_id[source] = trade_id
                    continue
                # 'undefined' fails-open: if no daily_closes.json or insufficient
                # history, we DON'T block (safer when data is unavailable)

            # Code-4 ATR-gated skip (env-gated; default OFF; **night-only**
            # since 2026-05-28). Fires BEFORE HALF_SIZE so ATR-skipped signals
            # don't increment the half-size counter. Pure function in atr_gate.py
            # handles validity (fail-open on missing atr, code-specific to ④,
            # session-conditional, threshold ≤0 = disabled). Spec:
            # docs/superpowers/specs/2026-05-27-mtx-skip-code4-high-atr.md
            # (section 3 updated 5/28 for night-only refinement; counterfactual
            # showed day-session ATR>58 trades net +78 pts edge, only night
            # cluster was net negative).
            if source == "mtx" and direction == "short":
                _sig_code = int(trade.get("sigCode") or 0)
                _sig_atr  = trade.get("atr")
                if should_skip_code4_atr(_sig_code, _sig_atr, SKIP_CODE_4_ATR_GT, self._current_session):
                    logger.info(
                        f"MTX code-4 ATR-gated skip | atr={_sig_atr} > "
                        f"threshold={SKIP_CODE_4_ATR_GT} session={self._current_session} "
                        f"id={trade_id} entry={trade.get('entry')}"
                    )
                    # Real-time Telegram via Health Bot channel (avoid polluting
                    # MTX_Monitor trade-signal stream). Per Sean 5/28 design choice
                    # for Phase 2 observation visibility — catch unexpected fires.
                    # After ≥6-week promotion (~7/8), may downgrade to session
                    # summary only.
                    _atr_skip_msg = (
                        f"🚫 ATR Skip | ④ short atr={_sig_atr} > {SKIP_CODE_4_ATR_GT}"
                        f" [{self._current_session}]"
                        f"\nentry={trade.get('entry')} id={trade_id}"
                    )
                    threading.Thread(
                        target=self._safe_health_notify, args=(_atr_skip_msg,), daemon=True
                    ).start()
                    self._last_seen_id[source] = trade_id
                    continue

            # Half-size skip-alternate (manual switch; default OFF). For MTX SHORT
            # signals whose code is in HALF_SIZE_CODES, silent-absorb every 2nd one
            # (≈50% participation). Fires before _enter; pyramid path returns later
            # so this only gates NEW entries, consistent with the regime gate above.
            if (HALF_SIZE_CODES and source == "mtx" and direction == "short"):
                _hs_code = int(trade.get("sigCode") or 0)
                if _hs_code in HALF_SIZE_CODES:
                    self._half_size_seen[_hs_code] = self._half_size_seen.get(_hs_code, 0) + 1
                    if self._half_size_seen[_hs_code] % 2 == 0:
                        logger.info(
                            f"MTX short signal {trade_id} (code {_hs_code}) silent-absorbed "
                            f"by half-size skip-alternate (≈50%, n={self._half_size_seen[_hs_code]})"
                        )
                        self._last_seen_id[source] = trade_id
                        continue

            with self._lock:
                units_here = self._units.get(source, [])
                cur_dir = units_here[0]["dir"] if units_here else None
                n_units = len(units_here)

            # FVG: any existing unit blocks new opens regardless of direction
            if source == "fvg" and n_units >= max_cap:
                logger.warning(f"FVG entry {trade_id} ignored — existing position id={units_here[0].get('id')}")
                self._last_seen_id[source] = trade_id
                continue

            if cur_dir == direction:
                if n_units >= max_cap:
                    # At max units, same direction: Worker ignores too
                    logger.info(f"Max units ({max_cap}), same-direction ignore | source={source} id={trade_id}")
                    self._last_seen_id[source] = trade_id
                    continue
                else:
                    # Pyramid (MTX only — FVG has max=1 so already returned above)
                    logger.info(f"Pyramid | source={source} unit {n_units + 1}/{max_cap} | id={trade_id}")
                    self._last_seen_id[source] = trade_id
                    self._add_unit(trade, source)
                    return
            else:
                # No position, or reversal
                self._last_seen_id[source] = trade_id
                self._enter(trade, source)
                return

    # ── Worker 狀態同步(每口獨立)────────────────────────────────

    def _sync_worker_state(self, history: list, source: str):
        """Sync local source units against the authoritative source history.
        Closes units whose corresponding history record now has a terminal status.

        Status handling:
          loss        → close with reason="loss"  (MTX + FVG)
          profit      → close with reason="profit" (MTX + FVG)
          trail       → close with reason="trail"  (MTX only — FVG doesn't emit)
          reversed    → close with reason="reversed" or "replaced" (MTX only)
          session_end → close with reason="session_end" (FVG only)
          open + new stop → update unit's stop (trailing-stop sync)
        """
        with self._lock:
            units_here = self._units.get(source, [])
            if not units_here:
                return
            units_snapshot = list(units_here)

        for unit in units_snapshot:
            # Scan full history — Worker keeps up to 50 entries
            our_trade = next((t for t in history if t.get("id") == unit["id"]), None)
            if our_trade is None:
                continue

            status   = our_trade.get("status", "open")
            new_stop = our_trade.get("stop")

            with self._lock:
                if unit not in self._units.get(source, []):
                    continue  # closed during this iteration

                # Session-open freeze: defer Worker-driven CLOSES during the window
                # (carried position waits out the opening spike). Trailing-stop level
                # syncs below place no order, so they're still allowed; the close is
                # re-evaluated on the next poll once the window clears.
                if self._in_open_freeze() and status in (
                        "loss", "profit", "trail", "reversed", "session_end"):
                    logger.info(
                        f"Session-open freeze | {source} Worker exit ({status}) deferred "
                        f"id={unit['id']} (first {OPEN_FREEZE_SECS}s of session open)"
                    )
                    continue

                if status == "loss":
                    pnl        = our_trade.get("pnl") or 0
                    exit_price = (our_trade["entry"] + pnl) if unit["dir"] == "long" \
                                 else (our_trade["entry"] - pnl)
                    logger.info(f"Worker exit (loss) | source={source} id={unit['id']}")
                    self._close_unit(unit, "loss", exit_price=exit_price)

                elif status == "profit":
                    exit_price = our_trade.get("target")
                    # FVG signals carry the actual exit price in meta when available
                    meta_exit = (our_trade.get("meta") or {}).get("exit_price")
                    if meta_exit is not None:
                        exit_price = meta_exit
                    logger.info(f"Worker exit (profit) | source={source} id={unit['id']}")
                    self._close_unit(unit, "profit", exit_price=exit_price)

                elif status == "trail":
                    # Worker (worker/index.js:782/785) writes status='trail' when trailing stop hits.
                    # Use the trail-stop level as exit_price; Worker's t.pnl already reflects locked profit.
                    exit_price = our_trade.get("stop")
                    logger.info(f"Worker exit (trail) | source={source} id={unit['id']}")
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
                    logger.info(f"Worker exit ({reason}) | source={source} id={unit['id']}")
                    self._close_unit(unit, reason, exit_price=exit_price)

                elif status == "session_end":
                    # FVG-specific: engine force-exits open positions at session end
                    meta_exit = (our_trade.get("meta") or {}).get("exit_price")
                    exit_price = meta_exit if meta_exit is not None else our_trade.get("entry")
                    logger.info(f"Worker exit (session_end) | source={source} id={unit['id']}")
                    self._close_unit(unit, "session_end", exit_price=exit_price)

                elif status in ("open", "trail") and new_stop and new_stop != unit["stop"]:
                    logger.info(f"Trailing stop synced | source={source} {unit['stop']} → {new_stop} | id={unit['id']}")
                    unit["stop"] = new_stop

    # ── 進場(反向或首口)────────────────────────────────────────

    def _enter(self, trade: dict, source: str):
        direction = trade.get("dir")
        entry     = trade.get("entry")

        with self._lock:
            for unit in list(self._units.get(source, [])):
                if unit["dir"] != direction:
                    self._close_unit(unit, "reversed", exit_price=entry)
            if self._units.get(source):
                return  # unexpected same-direction units still open
            self._open_unit(trade, source)

    # ── 加碼(第 2 口)────────────────────────────────────────────

    def _add_unit(self, trade: dict, source: str):
        max_cap = MAX_UNITS_PER_SOURCE.get(source, 1)
        with self._lock:
            units_here = self._units.get(source, [])
            if not units_here or len(units_here) >= max_cap:
                return
            # Lock first unit stop to entry ± PYRAMID_LOCK (MTX-style pyramid only)
            if source == "mtx":
                first = units_here[0]
                if first["dir"] == "long":
                    first["stop"] = max(first["stop"] or 0, first["entry"] + PYRAMID_LOCK)
                else:
                    first["stop"] = min(first["stop"] or 999999, first["entry"] - PYRAMID_LOCK)
                logger.info(f"First MTX unit stop locked → {first['stop']}")
            self._open_unit(trade, source, is_pyramid=True)

    # ── 開倉執行 ──────────────────────────────────────────────────

    def _open_unit(self, trade: dict, source: str, is_pyramid: bool = False, notify: bool = True,
                   place_order: Optional[bool] = None):
        # Call within lock (or at startup before threads start).
        # place_order=None → decide from source/mode via _should_place_order.
        # Caller can force place_order=False (e.g. startup restore).
        direction = trade.get("dir")
        product   = self.trader.config["product"]
        if place_order is None:
            place_order = self._should_place_order(source)

        # Phase 7: daily MAX LOSS gate. Only blocks real broker calls; startup state
        # restore (place_order=False) is always allowed so locked positions resume tracking.
        if place_order and self._trading_day_locked:
            label = "加碼" if is_pyramid else "進場"
            logger.warning(f"Daily MAX LOSS lock active — refusing {source} {label} signal id={trade.get('id')}")
            # No Telegram per rejection (would spam); original lock-trigger msg is enough
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
            "source":    source,
            "id":        trade["id"],
            "dir":       direction,
            "entry":     trade.get("entry"),
            "stop":      trade.get("stop"),
            "target":    trade.get("target"),
            "sig_label": trade.get("label") or trade.get("sigLabel", ""),
            "opened_at": int(time.time() * 1000),  # epoch ms, for trade duration calc
            "entry_fill": None,                     # actual broker fill price (set by on_fill)
        }
        self._units.setdefault(source, []).append(unit)
        # Fill-anchoring: register a pending ENTRY fill (in send order) so on_fill
        # attributes the real entry price to this unit, reversal/replace-safe via FIFO.
        if place_order:
            self._pending_fills.append({"kind": "entry", "bs": "B" if direction == "long" else "S",
                                        "unit": unit, "source": source, "id": unit["id"]})
            self._pending_fills = self._pending_fills[-12:]
        logger.info(f"{source.upper()} Unit {len(self._units[source])} opened | "
                    f"{direction} entry={unit['entry']} stop={unit['stop']}")

        # Persist unit state to disk so restart restores what the bot ACTUALLY holds.
        if source == "fvg":
            self._save_fvg_state()
        elif source == "mtx" and place_order:   # real open only; restore path saves at end
            self._save_mtx_state()

        if not notify:
            return

        emoji   = ENTRY_EMOJI.get(direction, "📌")
        tag     = SOURCE_TAG.get(source, "")
        dry_tag = " [模擬]" if self.dry_run else ""
        if not place_order and source == "fvg" and FVG_OBSERVE_MODE == "paper":
            dry_tag = " [PAPER]"
        suffix  = "(加碼)" if is_pyramid else ""
        text = (
            f"{emoji} <b>{tag}進場{dry_tag}{suffix}</b>\n"
            f"信號:{unit['sig_label']}\n"
            f"方向:{'多' if direction == 'long' else '空'}\n"
            f"進場:{unit['entry']}　停損:{unit['stop']}　停利:{unit['target']}"
        )
        threading.Thread(target=self._safe_notify, args=(text,), daemon=True).start()

    # ── tick-level 出場檢查(每口獨立)────────────────────────────

    def _check_exit_unit(self, unit: dict, price: float):
        # Call within lock
        source = unit.get("source", "mtx")
        if unit not in self._units.get(source, []):
            return
        if unit["dir"] == "long":
            if unit["stop"] and price <= unit["stop"]:
                logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, stop_hit_reason("long", unit["stop"], unit["entry"]), exit_price=price)
            elif unit["target"] and price >= unit["target"]:
                logger.info(f"Target hit | source={source} id={unit['id']} price={price} target={unit['target']}")
                self._close_unit(unit, "profit", exit_price=price)
        elif unit["dir"] == "short":
            if unit["stop"] and price >= unit["stop"]:
                logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, stop_hit_reason("short", unit["stop"], unit["entry"]), exit_price=price)
            elif unit["target"] and price <= unit["target"]:
                logger.info(f"Target hit | source={source} id={unit['id']} price={price} target={unit['target']}")
                self._close_unit(unit, "profit", exit_price=price)

    # ── 平倉執行(單口)──────────────────────────────────────────

    def _close_unit(self, unit: dict, reason: str, exit_price=None):
        # Call within lock
        source = unit.get("source", "mtx")
        if unit not in self._units.get(source, []):
            return

        place_order = self._should_place_order(source)
        product = self.trader.config["product"]
        if place_order:
            if unit["dir"] == "long":
                self._execute_order("SELL", product, 1, opencloseflag="1")
            else:
                self._execute_order("BUY", product, 1, opencloseflag="1")
            # Fill-anchoring: register a pending EXIT fill so on_fill's FIFO stays
            # aligned with send order (so an exit fill isn't mis-read as an entry fill
            # on same-direction reversals). Exit fills are consumed but not acted on
            # (Layer ① already derives real exit P&L from orders.jsonl).
            exit_pending = {"kind": "exit", "bs": "S" if unit["dir"] == "long" else "B"}
            self._pending_fills.append(exit_pending)
            self._pending_fills = self._pending_fills[-12:]

        pnl_pts = 0
        if exit_price and unit["entry"]:
            pnl_pts = (exit_price - unit["entry"]) if unit["dir"] == "long" \
                      else (unit["entry"] - exit_price)
        pnl_ntd = int(pnl_pts * POINT_VALUE)

        logger.info(f"{source.upper()} Unit closed | reason={reason} dir={unit['dir']} "
                    f"entry={unit['entry']} exit={exit_price} pnl={pnl_pts:+.0f}pts")

        self._session_trades.append({
            "source":    source,
            "label":     unit["sig_label"],
            "direction": unit["dir"],
            "entry":     unit["entry"],
            "exit":      exit_price,
            "pnl_pts":   pnl_pts,
            "reason":    reason,
        })
        # Persistent trade log + monthly counters (跨 restart 持久化).
        # Task B: real orders defer the write until on_fill stamps the real exit_fill;
        # paper (no broker order) has no fill coming → write immediately with nulls.
        record_kwargs = dict(
            source=source, label=unit["sig_label"], dir_=unit["dir"],
            entry=unit["entry"], exit_price=exit_price, stop=unit["stop"],
            target=unit["target"], pnl_pts=pnl_pts, reason=reason,
            sig_id=unit["id"], opened_at_ms=unit.get("opened_at"),
            entry_fill=unit.get("entry_fill"),
        )
        if place_order:
            pe = {"record": record_kwargs,
                  "deadline_ms": int(time.time() * 1000) + EXIT_FILL_TIMEOUT_MS}
            exit_pending["pe"] = pe
            self._pending_exit_records.append(pe)
            self._save_pending_exit_records()
        else:
            self._record_trade(**record_kwargs)
        # Phase 7: accumulate signal-side trading-day P&L — DISPLAY ONLY (heartbeat
        # field trading_day_pnl_pts). Lock authority moved to _check_daily_loss_lock,
        # which reads the restart-safe REAL-account P&L from pnl_calc. (Old behaviour
        # used this in-memory counter, which mixed in paper FVG and reset on restart.)
        self._trading_day_pnl_pts += pnl_pts
        self._units[source].remove(unit)

        # Persist state after change so disk reflects live positions crash-safely.
        if source == "fvg":
            self._save_fvg_state()
        elif source == "mtx":
            self._save_mtx_state()

        emoji     = EXIT_EMOJI.get(reason, "⏹")
        tag       = SOURCE_TAG.get(source, "")
        reason_zh = {"profit": "停利出場", "loss": "停損出場",
                     "reversed": "反向平倉", "replaced": "汰換平倉",
                     "trail": "移動停利", "session_end": "盤末平倉"}.get(reason, reason)
        dry_tag   = " [模擬]" if self.dry_run else ""
        if not place_order and source == "fvg" and FVG_OBSERVE_MODE == "paper":
            dry_tag = " [PAPER]"
        pnl_sign  = "+" if pnl_pts >= 0 else ""
        pnl_line  = (f"損益:<b>{pnl_sign}{pnl_pts:.0f} pts({pnl_sign}NT${pnl_ntd:,})</b>"
                     if exit_price else "")
        text = (
            f"{emoji} <b>{tag}出場{dry_tag}</b>\n"
            f"原因:{reason_zh}\n"
            f"方向:{'多' if unit['dir'] == 'long' else '空'}\n"
            f"進場:{unit['entry']}　出場:{exit_price}\n"
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
            src    = t.get("source", "mtx")
            tag    = "[FVG] " if src == "fvg" else ""
            lines.append(f"{icon} {tag}{t['label']}  {dir_zh}  {sign}{t['pnl_pts']:.0f}pts")
        lines.append("─" * 22)
        # Per-source breakdown when multiple sources contributed
        by_src: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            src = t.get("source", "mtx")
            d = by_src.setdefault(src, {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
            d["count"] += 1
            d["pnl"]   += t["pnl_pts"]
            if t["pnl_pts"] > 0:   d["wins"]   += 1
            elif t["pnl_pts"] < 0: d["losses"] += 1
        if len(by_src) > 1:
            for src, d in by_src.items():
                s = "+" if d["pnl"] >= 0 else ""
                lines.append(f"{src.upper()}:{d['count']} 筆(勝{d['wins']} 敗{d['losses']}) {s}{d['pnl']:.0f}pts")
        lines.append(f"筆數:{len(trades)}(勝{wins} 敗{losses})")
        lines.append(f"合計:<b>{total_sign}{total_pts:.0f} pts({total_sign}NT${total_ntd:,})</b>")

        dry_tag = "　[模擬]" if self.dry_run else ""
        self._notify("\n".join(lines) + dry_tag)
        logger.info(f"Session summary sent | {session_zh} {total_sign}{total_pts:.0f}pts")

        self._prev_session_pnl_pts = total_pts
        self._prev_session_label   = session_zh

    # ── 開盤通知 ──────────────────────────────────────────────────

    def _send_open_notify(self, session: str):
        session_zh = "日盤" if session == "day" else "夜盤"
        close_time = "13:45" if session == "day" else "05:00(+1)"
        dry_tag    = " [模擬]" if self.dry_run else ""

        with self._lock:
            units = self._flatten_units()

        if units:
            pos_lines = [
                f"持倉:{'多' if u['dir'] == 'long' else '空'}[{u.get('source','?').upper()}]"
                f"  進場 {u['entry']}  停損 {u['stop']}  停利 {u['target']}"
                for u in units
            ]
            pos_text = "\n".join(pos_lines)
        else:
            pos_text = "持倉:無"

        lines = [f"🔔 <b>{session_zh}開盤{dry_tag}</b>", "系統:✅ 正常運作", pos_text]
        if self._prev_session_label:
            prev_sign = "+" if self._prev_session_pnl_pts >= 0 else ""
            prev_ntd  = int(self._prev_session_pnl_pts * POINT_VALUE)
            lines.append(
                f"{self._prev_session_label}損益:{prev_sign}{self._prev_session_pnl_pts:.0f} pts"
                f"({prev_sign}NT${prev_ntd:,})"
            )
        lines.append(f"收盤:{close_time}")

        try:
            self._notify("\n".join(lines))
        except Exception as e:
            logger.warning(f"Open notify failed: {e}")
        logger.info(f"Open notify sent | {session_zh}")

    # ── 下單(dry_run 攔截)───────────────────────────────────────

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

    def _safe_health_notify(self, text: str):
        try:
            tg.send(self._tg_health_token, self._tg_health_chat, text)
        except Exception as e:
            logger.warning(f"Health notify failed: {e}")

    def _tick_wd_kill(self, msg: str) -> None:
        # Phase A (TICK_STALE_KILL off): observe only — log the would-fire, do NOT exit.
        # Phase B (on): alert then os._exit(1) so systemd restarts and the OS reclaims fds.
        if not TICK_STALE_KILL:
            logger.error(f"[tick-wd KILL would-fire] {msg}")
            return
        logger.error(f"[tick-wd KILL] {msg}")
        try:
            self._safe_health_notify(f"🔪 Trader self-restart: {msg}")
        except Exception:
            pass
        import os as _os
        _os._exit(1)

    # ── HTTP ─────────────────────────────────────────────────────

    def _fetch_history(self, url: str = HISTORY_URL) -> list:
        """Fetch raw history/signals list from a source URL. Used for both MTX
        history and FVG signals — same shape (list of trade-like dicts).

        Passes the payload through feed_schema.clean_feed: drops malformed entries
        (non-dict / non-int id) and detects a wholly-malformed (non-list) payload,
        so junk from the Worker can't reach the cursor/sync logic."""
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        items, ok = clean_feed(resp.json() or [])
        if not ok:
            logger.error(f"FEED_MALFORMED: non-list payload from {url} — dropped")
        return items

    # ── FVG_MODE='shadow' observe-only path ───────────────────────
    def _observe_fvg_signals(self, signals: list):
        """Shadow mode: log + Telegram on (id, status) transitions, never touch
        _units or broker. Paper/live modes go through the unified _check_new_signal
        / _sync_worker_state path instead.

        First poll primes the status map silently to avoid spamming historical entries.
        Subsequent polls notify only on transitions.
        """
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
            logger.info(f"FVG SHADOW {status} {dir_ch}@{entry} SL={stop} TP={target}{pnl_str} id={sig_id}")
            self._safe_notify(
                f"👁 <b>[FVG SHADOW]</b>  {status}\n"
                f"方向:{dir_ch}  進場 {entry}  停損 {stop}  停利 {target}{pnl_str}"
            )
        if not self._fvg_primed:
            self._fvg_primed = True
            logger.info(f"FVG SHADOW primed: {len(self._fvg_last_status)} initial signal(s) absorbed silently")
