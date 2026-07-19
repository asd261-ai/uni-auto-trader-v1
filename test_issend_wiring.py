"""Wiring tests: _open_unit/_close_unit must NOT book optimistically when the
order send fails client-side (issend=False: SDK failure or order-guard block).

2026-07-19 audit: a failed send produces NO broker reply, so the reject-rollback
path can never fire — the optimistically-recorded unit becomes a phantom that
books phantom P&L (entry) or silently drops a live position from tracking (exit).

Runs on system python3: heavy deps (requests) are stubbed before importing
strategy; Strategy is instantiated via __new__ with only the attrs these two
code paths touch (no SDK, no threads).

Run:  python3 -m unittest test_issend_wiring -v
"""
from __future__ import annotations

import sys
import types
import unittest

# Stub modules strategy.py imports but these tests never exercise.
if "requests" not in sys.modules:
    _rq = types.ModuleType("requests")
    class _RequestException(Exception):
        pass
    _rq.RequestException = _RequestException
    def _no_net(*a, **k):
        raise _RequestException("stubbed requests — no network in unit tests")
    _rq.get = _no_net
    _rq.post = _no_net
    sys.modules["requests"] = _rq

import strategy as strategy_mod
from strategy import MTXStrategy as Strategy


class _FakeTrader:
    def __init__(self):
        self.config = {"product": "MXFH6"}


def _make_strategy(send_ok: bool):
    """Bare Strategy with only the state _open_unit/_close_unit touch."""
    s = Strategy.__new__(Strategy)
    s.trader = _FakeTrader()
    s.dry_run = False
    s._units = {"mtx": [], "fvg": []}
    s._pending_fills = []
    s._pending_exit_records = []
    s._session_trades = []
    s._trading_day_pnl_pts = 0
    s._trading_day_locked = False
    s._current_session = "night"
    s._last_price = None
    s._should_place_order = lambda source: True
    s._save_mtx_state = lambda: None
    s._save_fvg_state = lambda: None
    s._save_pending_exit_records = lambda: None
    s._record_trade = lambda **kw: s.__dict__.setdefault("_recorded", []).append(kw)
    s._safe_notify = lambda text: None
    s._safe_health_notify = lambda text: None
    s._send_calls = []
    def _exec(side, product, qty, opencloseflag=""):
        s._send_calls.append((side, product, qty, opencloseflag))
        return send_ok
    s._execute_order = _exec
    return s


def _trade(id_=101, dir_="long"):
    return {"id": id_, "dir": dir_, "entry": 44500, "stop": 44350,
            "target": 44800, "label": "test-sig"}


class OpenUnitIssend(unittest.TestCase):
    def test_send_ok_books_unit_and_pend(self):
        s = _make_strategy(send_ok=True)
        s._open_unit(_trade(), "mtx", notify=False)
        self.assertEqual(len(s._units["mtx"]), 1)
        self.assertEqual(len(s._pending_fills), 1)
        self.assertEqual(len(s._send_calls), 1)

    def test_send_failed_books_nothing(self):
        # issend=False → no unit, no pending fill: there will never be a reply
        # to trigger rollback, so booking here would create a phantom.
        s = _make_strategy(send_ok=False)
        s._open_unit(_trade(), "mtx", notify=False)
        self.assertEqual(s._units["mtx"], [])
        self.assertEqual(s._pending_fills, [])
        self.assertEqual(len(s._send_calls), 1)   # the send was attempted

    def test_restore_path_unaffected(self):
        # place_order=False (startup restore) never sends and must still book.
        s = _make_strategy(send_ok=False)
        s._open_unit(_trade(), "mtx", notify=False, place_order=False)
        self.assertEqual(len(s._units["mtx"]), 1)
        self.assertEqual(s._send_calls, [])


class CloseUnitIssend(unittest.TestCase):
    def _open_held_unit(self, s):
        s._open_unit(_trade(), "mtx", notify=False, place_order=False)
        return s._units["mtx"][0]

    def test_send_ok_closes_and_defers_record(self):
        s = _make_strategy(send_ok=True)
        u = self._open_held_unit(s)
        s._close_unit(u, "loss", exit_price=44350)
        self.assertEqual(s._units["mtx"], [])
        self.assertEqual(len(s._pending_exit_records), 1)

    def test_send_failed_keeps_unit_for_retry(self):
        # issend=False → the position is still live at the broker. The unit must
        # stay tracked (next tick retries the close); nothing may be booked.
        s = _make_strategy(send_ok=False)
        u = self._open_held_unit(s)
        s._close_unit(u, "loss", exit_price=44350)
        self.assertEqual(s._units["mtx"], [u])          # still tracked
        self.assertEqual(s._pending_exit_records, [])   # nothing booked
        self.assertEqual(s._session_trades, [])
        self.assertEqual(s.__dict__.get("_recorded", []), [])
        self.assertEqual(len(s._send_calls), 1)


if __name__ == "__main__":
    unittest.main()
