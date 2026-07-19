"""P4 wiring tests (2026-07-20 design: flat-checkpoint + orderno claiming).

- strategy._log_flat_transition(): writes ONE `flat` event per not-flat→flat
  transition (the pnl_calc FIFO reset anchor).
- trader._on_match / _on_reply orderno filtering: observe logs would-skip but
  changes nothing; on-mode routes foreign fills/rejects away from strategy.

Run:  python3 -m unittest test_audit_p4_wiring -v
"""
from __future__ import annotations

import types
import unittest

from test_issend_wiring import _make_strategy, _trade   # requests stub
from test_audit_p1_wiring import _harden
import strategy as strategy_mod
import test_trader_query as _tq_stub    # installs the unitrade SDK stub
import trader as trader_mod
from orderno_claim import OrdernoClaimer


class FlatTransitionHook(unittest.TestCase):
    def _mk(self):
        s = _harden(_make_strategy(send_ok=True))
        s._was_flat = None
        self.events = []
        self._orig = strategy_mod.order_log.log_event
        strategy_mod.order_log.log_event = lambda ev, **kw: self.events.append(ev)
        return s

    def tearDown(self):
        strategy_mod.order_log.log_event = self._orig

    def test_transition_to_flat_writes_once(self):
        s = self._mk()
        s._units["mtx"] = [{"dir": "long"}]
        s._log_flat_transition()                  # open → nothing
        self.assertEqual(self.events, [])
        s._units["mtx"] = []
        s._log_flat_transition()                  # flat transition → one event
        s._log_flat_transition()                  # still flat → no dup
        self.assertEqual(self.events, ["flat"])

    def test_boot_flat_writes_checkpoint(self):
        s = self._mk()
        s._log_flat_transition()                  # first call, already flat
        self.assertEqual(self.events, ["flat"])


class _FakeMatch:
    def __init__(self, orderno, bs="B", price=44510.0, qty=1, productid="MXFH6"):
        self.orderno = orderno; self.bs = bs; self.matchprice = price
        self.matchqty = qty; self.productid = productid


class _FakeReply:
    def __init__(self, orderno, bs="B", status="PSC0019:保證金不足", productid="MXFH6"):
        self.orderno = orderno; self.bs = bs
        self.orderstatus = status; self.productid = productid


class _StratRecorder:
    def __init__(self):
        self.fills = []; self.rejects = []
    def on_fill(self, productid, bs, price):
        self.fills.append((productid, bs, price))
    def on_order_rejected(self, productid, bs, status):
        self.rejects.append((productid, bs, status))
    def on_tick(self, price):
        pass


def _mk_trader(mode):
    t = trader_mod.AutoTrader.__new__(trader_mod.AutoTrader)
    t.config = {"product": "MXFH6"}
    t.strategy = _StratRecorder()
    t._orderno_filter_mode = mode
    t._claimer = OrdernoClaimer(link_window_sec=3)
    self_orig = trader_mod.order_log.log_event
    t.__dict__["_test_restore_log"] = self_orig
    trader_mod.order_log.log_event = lambda ev, **kw: None
    return t


class TraderOrdernoFilter(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "t"):
            trader_mod.order_log.log_event = self.t.__dict__["_test_restore_log"]

    def test_observe_mode_foreign_fill_still_reaches_strategy(self):
        self.t = t = _mk_trader("observe")
        t._on_match(_FakeMatch("QI999"))          # never claimed → foreign
        self.assertEqual(len(t.strategy.fills), 1)

    def test_on_mode_foreign_fill_filtered(self):
        self.t = t = _mk_trader("on")
        t._on_match(_FakeMatch("QI999"))
        self.assertEqual(t.strategy.fills, [])

    def test_on_mode_bot_fill_passes(self):
        self.t = t = _mk_trader("on")
        import time as _time
        now = _time.time()
        t._claimer.note_sent(now, "MXFH6", "B")
        t._on_reply(_FakeReply("PY001", status="委託成功"))
        t._on_match(_FakeMatch("PY001"))
        self.assertEqual(len(t.strategy.fills), 1)

    def test_on_mode_foreign_reject_not_routed(self):
        # Sean's manual same-product PSC reject must not reach on_order_rejected.
        self.t = t = _mk_trader("on")
        t._on_reply(_FakeReply("QI888"))
        self.assertEqual(t.strategy.rejects, [])

    def test_on_mode_bot_reject_still_routed(self):
        self.t = t = _mk_trader("on")
        import time as _time
        t._claimer.note_sent(_time.time(), "MXFH6", "B")
        t._on_reply(_FakeReply("PY002"))
        self.assertEqual(len(t.strategy.rejects), 1)

    def test_observe_mode_foreign_reject_still_routed(self):
        self.t = t = _mk_trader("observe")
        t._on_reply(_FakeReply("QI888"))
        self.assertEqual(len(t.strategy.rejects), 1)


if __name__ == "__main__":
    unittest.main()
