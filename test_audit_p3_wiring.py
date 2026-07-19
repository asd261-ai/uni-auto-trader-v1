"""P3 audit-batch wiring tests (2026-07-19 dual-repo audit, long tail).

Covers:
- _position_is_flat acquires the lock (fd-watchdog soft-kill must never read
  half-written position state; unacquirable lock → NOT flat → no kill).
- on_fill FIFO attribution pins (the sole writer of entry_fill/exit_fill had
  zero tests).
- _open_unit pre-order gate wiring (daily-loss lock, cross-source on/observe).
- _record_missed_exit reads the fields the Worker actually writes
  (closePrice/pnl/closedAt — not exit/exitPrice).
- _record_trade honors caller-provided trading_day/session (boot flush must
  not re-stamp yesterday's trade with today's day).
- Daily MAX LOSS lock escalates after N consecutive failed real-P&L reads
  (was: debug-only fail-open — breaker dead while believed armed).

Run:  python3 -m unittest test_audit_p3_wiring -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta

from test_issend_wiring import _make_strategy, _trade   # installs requests stub
from test_audit_p1_wiring import _harden
import strategy as strategy_mod


def _mk(send_ok=True):
    s = _harden(_make_strategy(send_ok=send_ok))
    s._fill_anchor = False
    s._current_session = "night"
    s._notes = {"health": []}
    s._safe_health_notify = lambda text: s._notes["health"].append(text)
    return s


class PositionIsFlatLock(unittest.TestCase):
    def test_flat_when_lock_free_and_empty(self):
        s = _mk()
        self.assertTrue(s._position_is_flat())

    def test_not_flat_with_pending_fill(self):
        s = _mk()
        s._pending_fills.append({"kind": "entry", "bs": "B"})
        self.assertFalse(s._position_is_flat())

    def test_unacquirable_lock_reports_not_flat(self):
        # Entry order in flight: _open_unit holds the lock across the blocking
        # SDK send. The watchdog must NOT read half-written state and conclude
        # "flat" — timeout → False → soft-kill stays its hand.
        s = _mk()
        s._lock.acquire()
        try:
            self.assertFalse(s._position_is_flat())
        finally:
            s._lock.release()


class OnFillFifo(unittest.TestCase):
    def test_entry_fill_stamps_unit(self):
        s = _mk()
        u = {"source": "mtx", "id": 5, "dir": "long", "entry": 44500, "entry_fill": None}
        s._pending_fills.append({"kind": "entry", "bs": "B", "unit": u,
                                 "source": "mtx", "id": 5, "ts_ms": 1})
        s.on_fill("MXFH6", "B", 44510)
        self.assertEqual(u["entry_fill"], 44510)
        self.assertEqual(s._pending_fills, [])

    def test_wrong_bs_front_leaves_queue_intact(self):
        # Manual/foreign fill on the other side must not consume the bot's pend.
        s = _mk()
        u = {"source": "mtx", "id": 5, "dir": "long", "entry": 44500, "entry_fill": None}
        s._pending_fills.append({"kind": "entry", "bs": "B", "unit": u,
                                 "source": "mtx", "id": 5, "ts_ms": 1})
        s.on_fill("MXFH6", "S", 44490)
        self.assertIsNone(u["entry_fill"])
        self.assertEqual(len(s._pending_fills), 1)

    def test_foreign_product_ignored(self):
        s = _mk()
        u = {"source": "mtx", "id": 5, "dir": "long", "entry": 44500, "entry_fill": None}
        s._pending_fills.append({"kind": "entry", "bs": "B", "unit": u,
                                 "source": "mtx", "id": 5, "ts_ms": 1})
        s.on_fill("TXFH6", "B", 44510)
        self.assertIsNone(u["entry_fill"])
        self.assertEqual(len(s._pending_fills), 1)

    def test_duplicate_entry_fill_ignored(self):
        s = _mk()
        u = {"source": "mtx", "id": 5, "dir": "long", "entry": 44500, "entry_fill": 44505}
        s._pending_fills.append({"kind": "entry", "bs": "B", "unit": u,
                                 "source": "mtx", "id": 5, "ts_ms": 1})
        s.on_fill("MXFH6", "B", 44520)
        self.assertEqual(u["entry_fill"], 44505)   # first fill wins

    def test_exit_fill_books_deferred_record(self):
        s = _mk()
        pe = {"record": dict(source="mtx", label="x", dir_="long", entry=44500,
                             exit_price=44400, stop=44400, target=None,
                             pnl_pts=-100, reason="loss", sig_id=9,
                             opened_at_ms=0, entry_fill=44505),
              "deadline_ms": int(time.time() * 1000) + 60_000}
        s._pending_exit_records.append(pe)
        s._pending_fills.append({"kind": "exit", "bs": "S", "pe": pe, "ts_ms": 1})
        s.on_fill("MXFH6", "S", 44398)
        self.assertEqual(s._pending_exit_records, [])
        self.assertEqual(s._pending_fills, [])
        rec = s.__dict__.get("_recorded", [])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0].get("exit_fill"), 44398)


class OpenUnitGates(unittest.TestCase):
    def test_daily_loss_lock_refuses_real_entry(self):
        s = _mk()
        s._trading_day_locked = True
        s._open_unit(_trade(), "mtx", notify=False)
        self.assertEqual(s._units["mtx"], [])
        self.assertEqual(s._send_calls, [])

    def test_daily_loss_lock_allows_restore(self):
        s = _mk()
        s._trading_day_locked = True
        s._open_unit(_trade(), "mtx", notify=False, place_order=False)
        self.assertEqual(len(s._units["mtx"]), 1)

    def test_cross_source_on_blocks_opposite(self):
        s = _mk()
        s._units["fvg"] = [{"dir": "short"}]
        orig = strategy_mod.CROSS_SOURCE_OPP_MODE
        strategy_mod.CROSS_SOURCE_OPP_MODE = "on"
        try:
            s._open_unit(_trade(dir_="long"), "mtx", notify=False)
        finally:
            strategy_mod.CROSS_SOURCE_OPP_MODE = orig
        self.assertEqual(s._units["mtx"], [])
        self.assertEqual(s._send_calls, [])

    def test_cross_source_observe_still_places(self):
        s = _mk()
        s._units["fvg"] = [{"dir": "short"}]
        orig = strategy_mod.CROSS_SOURCE_OPP_MODE
        strategy_mod.CROSS_SOURCE_OPP_MODE = "observe"
        try:
            s._open_unit(_trade(dir_="long"), "mtx", notify=False)
        finally:
            strategy_mod.CROSS_SOURCE_OPP_MODE = orig
        self.assertEqual(len(s._units["mtx"]), 1)
        self.assertEqual(len(s._send_calls), 1)


class MissedExitFields(unittest.TestCase):
    def test_reads_worker_closeprice(self):
        # Worker terminal records write closePrice/pnl/closedAt — never
        # exit/exitPrice. The old reader booked exit=None pnl=0 for EVERY
        # offline exit.
        s = _mk()
        unit = {"id": 7, "dir": "long", "entry": 44500, "stop": 44350,
                "target": 44800, "sig_label": "test", "opened_at": 1}
        worker = {"id": 7, "status": "loss", "closePrice": 44350,
                  "pnl": -150, "closedAt": 1_784_400_000_000}
        s._record_missed_exit(unit, worker)
        rec = s.__dict__.get("_recorded", [])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["exit_price"], 44350)
        self.assertEqual(rec[0]["pnl_pts"], -150.0)

    def test_falls_back_to_worker_pnl_when_no_price(self):
        s = _mk()
        unit = {"id": 7, "dir": "short", "entry": None, "sig_label": "t", "opened_at": 1}
        worker = {"id": 7, "status": "trail", "pnl": 42}
        s._record_missed_exit(unit, worker)
        rec = s.__dict__.get("_recorded", [])
        self.assertEqual(rec[0]["pnl_pts"], 42.0)


class RecordTradeStamp(unittest.TestCase):
    def test_caller_trading_day_wins_over_now(self):
        s = _mk()
        del s._record_trade   # use the real method, not the harness recorder
        s._month_pnl_pts = 0; s._month_trades_count = 0
        s._month_wins = 0; s._month_losses = 0; s._month_by_source = {}
        tmp = os.path.join(tempfile.mkdtemp(), "trades.jsonl")
        orig = strategy_mod.TRADES_LOG_PATH
        strategy_mod.TRADES_LOG_PATH = tmp
        try:
            s._record_trade(source="mtx", label="x", dir_="long", entry=44500,
                            exit_price=44400, stop=None, target=None,
                            pnl_pts=-100, reason="loss", sig_id=1, opened_at_ms=0,
                            trading_day="2026-07-17", session="night")
        finally:
            strategy_mod.TRADES_LOG_PATH = orig
        with open(tmp, encoding="utf-8") as f:
            row = json.loads(f.readline())
        self.assertEqual(row["trading_day"], "2026-07-17")
        self.assertEqual(row["session"], "night")


class LossLockReadFailEscalation(unittest.TestCase):
    def _prep(self, s):
        s._trading_day_locked = False
        s._pnl_read_fail_streak = 0
        self.orig_max = strategy_mod.DAILY_MAX_LOSS_PTS
        self.orig_n   = strategy_mod.PNL_READ_FAIL_ALERT_N
        strategy_mod.DAILY_MAX_LOSS_PTS = -400.0
        strategy_mod.PNL_READ_FAIL_ALERT_N = 3
        self.orig_hb = strategy_mod.pnl_calc.heartbeat_fields

    def _restore(self):
        strategy_mod.DAILY_MAX_LOSS_PTS = self.orig_max
        strategy_mod.PNL_READ_FAIL_ALERT_N = self.orig_n
        strategy_mod.pnl_calc.heartbeat_fields = self.orig_hb

    def test_escalates_once_at_threshold(self):
        s = _mk()
        self._prep(s)
        strategy_mod.pnl_calc.heartbeat_fields = lambda base=None: (_ for _ in ()).throw(RuntimeError("corrupt"))
        try:
            for _ in range(5):
                s._check_daily_loss_lock()
        finally:
            self._restore()
        alerts = [t for t in s._notes["health"] if "熔斷" in t or "P&L" in t]
        self.assertEqual(len(alerts), 1)   # one-shot at streak==N, not every poll

    def test_success_resets_streak(self):
        s = _mk()
        self._prep(s)
        strategy_mod.pnl_calc.heartbeat_fields = lambda base=None: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            s._check_daily_loss_lock()
            s._check_daily_loss_lock()
            strategy_mod.pnl_calc.heartbeat_fields = lambda base=None: {"real_trading_day_pnl_pts": 10.0}
            s._check_daily_loss_lock()
            self.assertEqual(s._pnl_read_fail_streak, 0)
        finally:
            self._restore()


if __name__ == "__main__":
    unittest.main()
