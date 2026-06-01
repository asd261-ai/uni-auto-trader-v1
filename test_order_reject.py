"""Tests for order_reject. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_order_reject -v
"""
import unittest

import order_reject as orj


def _unit(source="mtx", dir_="short", id_=111):
    return {"source": source, "id": id_, "dir": dir_, "entry": 46137, "stop": 46290}


class IsRejectStatus(unittest.TestCase):
    def test_fuf_codes_are_rejects(self):
        self.assertTrue(orj.is_reject_status("FUF1239:同ID客戶未沖銷部位及委託保證金超過使用額度"))
        self.assertTrue(orj.is_reject_status("FUF0092:無足夠留倉口數平倉"))

    def test_success_and_fill_are_not_rejects(self):
        self.assertFalse(orj.is_reject_status("委託成功"))
        self.assertFalse(orj.is_reject_status("完全成交"))
        self.assertFalse(orj.is_reject_status(""))
        self.assertFalse(orj.is_reject_status(None))


class RollbackRejectedEntry(unittest.TestCase):
    def test_rejected_entry_removes_unit_and_pops_pending(self):
        unit = _unit()
        units = {"mtx": [unit]}
        pending = [{"kind": "entry", "bs": "S", "unit": unit}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6")
        self.assertIs(removed, unit)
        self.assertEqual(units["mtx"], [])
        self.assertEqual(pending, [])

    def test_foreign_product_is_noop(self):
        unit = _unit()
        units = {"mtx": [unit]}
        pending = [{"kind": "entry", "bs": "S", "unit": unit}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFG6", "S", "MXFF6")
        self.assertIsNone(removed)
        self.assertEqual(units["mtx"], [unit])
        self.assertEqual(len(pending), 1)

    def test_bs_mismatch_leaves_queue_intact(self):
        unit = _unit(dir_="long")
        units = {"mtx": [unit]}
        pending = [{"kind": "entry", "bs": "B", "unit": unit}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6")
        self.assertIsNone(removed)
        self.assertEqual(units["mtx"], [unit])

    def test_exit_rejection_does_not_remove_unit(self):
        unit = _unit()
        units = {"mtx": [unit]}
        pending = [{"kind": "exit", "bs": "B"}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "B", "MXFF6")
        self.assertIsNone(removed)
        self.assertEqual(units["mtx"], [unit])
        self.assertEqual(len(pending), 1)

    def test_empty_pending_is_noop(self):
        self.assertIsNone(orj.rollback_rejected_entry([], {"mtx": []}, "MXFF6", "S", "MXFF6"))

    def test_fifo_pops_front_entry_only(self):
        u1, u2 = _unit(id_=1), _unit(id_=2)
        units = {"mtx": [u1, u2]}
        pending = [{"kind": "entry", "bs": "S", "unit": u1},
                   {"kind": "entry", "bs": "S", "unit": u2}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6")
        self.assertIs(removed, u1)
        self.assertEqual(units["mtx"], [u2])
        self.assertEqual(len(pending), 1)
        self.assertIs(pending[0]["unit"], u2)
