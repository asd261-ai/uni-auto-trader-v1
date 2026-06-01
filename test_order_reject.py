"""Tests for order_reject. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_order_reject -v
"""
import unittest

import order_reject as orj


def _unit(source="mtx", dir_="short", id_=111, entry_fill=None):
    return {"source": source, "id": id_, "dir": dir_, "entry": 46137,
            "stop": 46290, "entry_fill": entry_fill}


def _entry(unit, bs):
    return {"kind": "entry", "bs": bs, "unit": unit}


def _exit(bs):
    return {"kind": "exit", "bs": bs}


class IsRejectStatus(unittest.TestCase):
    def test_real_reject_codes_are_rejects(self):
        for s in ("FUF1239:同ID客戶未沖銷部位及委託保證金超過使用額度",
                  "FUF0092:無足夠留倉口數平倉                                     0",
                  "FUF0026:商品代號錯誤",
                  "TTO0001:交易時間已結束",
                  "HHO0038:市價單不允許當日有效委託"):
            self.assertTrue(orj.is_reject_status(s), s)

    def test_success_statuses_are_not_rejects(self):
        for s in ("委託成功", "完全成交", "刪單成功", "改價成功", "", None):
            self.assertFalse(orj.is_reject_status(s), repr(s))


class RollbackRejectedEntry(unittest.TestCase):
    def test_single_unfilled_entry_rolled_back(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]
        self.assertIs(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"), u)
        self.assertEqual(units["mtx"], [])
        self.assertEqual(pending, [])

    def test_foreign_product_is_noop(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFG6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 1)

    def test_bs_mismatch_is_noop(self):
        u = _unit(dir_="long")
        units = {"mtx": [u]}
        pending = [_entry(u, "B")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])

    def test_filled_entry_is_never_removed(self):
        u = _unit(entry_fill=46132)        # already filled
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 1)

    def test_two_unfilled_same_side_entries_ambiguous_noop(self):
        u1, u2 = _unit(id_=1), _unit(id_=2)
        units = {"mtx": [u1, u2]}
        pending = [_entry(u1, "S"), _entry(u2, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u1, u2])
        self.assertEqual(len(pending), 2)

    def test_pending_exit_same_side_bails(self):
        # An exit (close) reject like FUF0092 shares bs with the close order; if a same-side
        # exit is pending, the reject may be for it, not the entry → bail.
        u = _unit()
        units = {"mtx": [u]}
        pending = [_exit("S"), _entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 2)

    def test_reversal_opposite_side_entry_rolled_back(self):
        # Reversal: pending exit (close long, B) + new entry (short, S). Reject S → the
        # exit is opposite side, so the entry is unambiguous → roll it back.
        u = _unit(dir_="short")
        units = {"mtx": [u]}
        pending = [_exit("B"), _entry(u, "S")]
        self.assertIs(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"), u)
        self.assertEqual(units["mtx"], [])
        self.assertEqual(pending, [_exit("B")])

    def test_exit_reject_no_entry_candidate_is_noop(self):
        units = {"mtx": []}
        pending = [_exit("B")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "B", "MXFF6"))
        self.assertEqual(len(pending), 1)

    def test_empty_pending_is_noop(self):
        self.assertIsNone(orj.rollback_rejected_entry([], {"mtx": []}, "MXFF6", "S", "MXFF6"))
