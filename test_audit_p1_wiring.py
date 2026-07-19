"""P1 audit-batch wiring tests (2026-07-19 dual-repo audit).

Covers:
- MTX boot floor: with an uninitialized cursor (startup Worker fetch failed),
  pre-boot `open` signals are absorbed instead of re-entering a held position.
- Startup local fallback: when the Worker is unreachable at boot, held units
  are restored from mtx_state.json so their exits stay managed.
- Exit-fill timeout flush also removes the exit pend from _pending_fills
  (FIFO poison).
- Rejected-entry rollback persists state to disk (no ghost resurrection on
  restart).

Reuses the bare-instance harness from test_issend_wiring (stubs `requests`
before importing strategy).

Run:  python3 -m unittest test_audit_p1_wiring -v
"""
from __future__ import annotations

import threading
import time
import unittest

from test_issend_wiring import _make_strategy, _trade   # installs the requests stub
import strategy as strategy_mod


def _harden(s):
    """Extra attrs the P1 paths touch beyond the issend harness."""
    s._lock = threading.Lock()
    s._last_seen_id = {"mtx": None, "fvg": None}
    s._mtx_boot_ts_ms = int(time.time() * 1000)
    s._in_open_freeze = lambda: False
    s._margin_alert_sent = False
    s._margin_alert_ts = 0.0
    s._saved = {"mtx": 0, "fvg": 0}
    s._save_mtx_state = lambda: s._saved.__setitem__("mtx", s._saved["mtx"] + 1)
    s._save_fvg_state = lambda: s._saved.__setitem__("fvg", s._saved["fvg"] + 1)
    return s


class MtxBootFloor(unittest.TestCase):
    def _sig(self, id_, status="open"):
        return {"id": id_, "status": status, "dir": "long", "entry": 44500,
                "stop": 44350, "target": 44800, "sigLabel": "boot-test"}

    def test_preboot_open_signal_absorbed_when_cursor_none(self):
        s = _harden(_make_strategy(send_ok=True))
        opened = []
        s._open_unit = lambda *a, **k: opened.append(a)
        pre_boot_id = s._mtx_boot_ts_ms - 60_000     # opened before this boot
        s._check_new_signal([self._sig(pre_boot_id)], "mtx")
        self.assertEqual(opened, [])                 # no duplicate real entry
        self.assertEqual(s._last_seen_id["mtx"], pre_boot_id)  # cursor recovers

    def test_postboot_signal_not_absorbed(self):
        s = _harden(_make_strategy(send_ok=True))
        opened = []
        s._open_unit = lambda *a, **k: opened.append(a)
        new_id = s._mtx_boot_ts_ms + 60_000          # genuinely new, post-boot
        s._check_new_signal([self._sig(new_id)], "mtx")
        self.assertEqual(len(opened), 1)

    def test_floor_inactive_once_cursor_initialized(self):
        # Normal boots set the cursor; the floor must not eat signals then.
        s = _harden(_make_strategy(send_ok=True))
        s._last_seen_id["mtx"] = 100
        opened = []
        s._open_unit = lambda *a, **k: opened.append(a)
        s._check_new_signal([self._sig(s._mtx_boot_ts_ms - 60_000)], "mtx")
        self.assertEqual(len(opened), 1)


class StartupLocalFallback(unittest.TestCase):
    def test_fallback_restores_local_units_without_orders(self):
        import json, os, tempfile
        s = _harden(_make_strategy(send_ok=True))
        tmp = os.path.join(tempfile.mkdtemp(), "mtx_state.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"product": "MXFH6", "mtx_units": [
                {"id": 123, "dir": "long", "entry": 44500, "stop": 44350,
                 "target": 44800, "sig_label": "carried"}]}, f)
        orig = strategy_mod.MTX_STATE_PATH
        strategy_mod.MTX_STATE_PATH = type(orig)(tmp) if not isinstance(orig, str) else tmp
        try:
            s._restore_mtx_local_fallback()
        finally:
            strategy_mod.MTX_STATE_PATH = orig
        self.assertEqual(len(s._units["mtx"]), 1)    # held unit tracked again
        self.assertEqual(s._units["mtx"][0]["id"], 123)
        self.assertEqual(s._send_calls, [])          # never places orders


class FlushClearsPendingFills(unittest.TestCase):
    def test_due_flush_removes_exit_pend_from_fifo(self):
        s = _harden(_make_strategy(send_ok=True))
        pe = {"record": dict(source="mtx", label="x", dir_="long", entry=44500,
                             exit_price=44350, stop=44350, target=44800,
                             pnl_pts=-150, reason="loss", sig_id=9,
                             opened_at_ms=0, entry_fill=None),
              "deadline_ms": int(time.time() * 1000) - 1}   # already overdue
        exit_pend = {"kind": "exit", "bs": "S", "pe": pe,
                     "ts_ms": int(time.time() * 1000)}
        s._pending_fills.append(exit_pend)
        s._pending_exit_records.append(pe)
        s._flush_due_exit_records()
        self.assertEqual(s._pending_exit_records, [])
        self.assertEqual(s._pending_fills, [],
                         "flushed pe must not leave its exit pend poisoning the FIFO")


class RollbackPersistsState(unittest.TestCase):
    def test_entry_rollback_saves_mtx_state(self):
        s = _harden(_make_strategy(send_ok=True))
        u = {"source": "mtx", "id": 7, "dir": "short", "entry": 44600,
             "stop": 44750, "entry_fill": None}
        s._units["mtx"].append(u)
        s._pending_fills.append({"kind": "entry", "bs": "S", "unit": u,
                                 "ts_ms": int(time.time() * 1000)})
        s.on_order_rejected("MXFH6", "S", "PSC0019:保證金不足")
        self.assertEqual(s._units["mtx"], [])        # rolled back
        self.assertGreaterEqual(s._saved["mtx"], 1,
                                "rollback must persist mtx_state.json or a "
                                "restart resurrects the phantom unit")


if __name__ == "__main__":
    unittest.main()
