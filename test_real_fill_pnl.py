"""Pure unittest for real_fill_pnl. No broker-SDK / no I/O.
Run:  python3 -m unittest test_real_fill_pnl -v
WHY: real-fill P&L must NEVER fall back to signal values — a missing fill must
read as null, not a fabricated number, or real-money attribution silently lies.
"""
import unittest
import real_fill_pnl as rfp


class ComputePnlPtsReal(unittest.TestCase):
    def test_long_uses_exit_minus_entry(self):
        self.assertEqual(rfp.compute_pnl_pts_real("long", 46100, 46160), 60)

    def test_short_uses_entry_minus_exit(self):
        self.assertEqual(rfp.compute_pnl_pts_real("short", 46470, 46450), 20)

    def test_missing_entry_fill_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("long", None, 46160))

    def test_missing_exit_fill_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("short", 46470, None))

    def test_both_missing_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("long", None, None))

    def test_result_is_rounded_int(self):
        self.assertEqual(rfp.compute_pnl_pts_real("long", 46100.4, 46160.4), 60)
