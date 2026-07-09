"""Pure unittest for exit_sanity. No broker-SDK / no I/O.
Run:  python3 -m unittest test_exit_sanity -v
WHY: 2026-06-30 the Worker's fill_anchor null-guard bug fed tiny "target" values
(e.g. 15) into strategy's profit-exit path, which trusted them blindly and wrote
|entry|-sized phantom P&L into trades.jsonl (30 corrupt rows). The Worker side is
fixed; this module is the trader-side second line of defense — an implausible
Worker exit price must fall back to entry (≈0 pnl) instead of being believed.
"""
import unittest
from exit_sanity import sane_exit_price


class SaneExitPrice(unittest.TestCase):
    def test_normal_target_passes_through(self):
        price, ok = sane_exit_price(46350, 46200, 1000)
        self.assertEqual(price, 46350)
        self.assertTrue(ok)

    def test_2026_06_30_corruption_falls_back_to_entry(self):
        # The actual incident shape: target polluted to a tiny slip-delta (15)
        # while entry is a real index level (~46xxx). Must NOT be trusted.
        price, ok = sane_exit_price(15, 46200, 1000)
        self.assertEqual(price, 46200)
        self.assertFalse(ok)

    def test_short_side_corruption_also_caught(self):
        price, ok = sane_exit_price(-8, 45057, 1000)
        self.assertEqual(price, 45057)
        self.assertFalse(ok)

    def test_boundary_exactly_max_pts_is_allowed(self):
        # Inclusive bound: a legitimate huge runner should not be clipped.
        price, ok = sane_exit_price(47200, 46200, 1000)
        self.assertEqual(price, 47200)
        self.assertTrue(ok)

    def test_just_over_bound_is_rejected(self):
        price, ok = sane_exit_price(47201, 46200, 1000)
        self.assertEqual(price, 46200)
        self.assertFalse(ok)

    def test_none_candidate_passes_through(self):
        # Null target (e.g. pure-trail) is owned by existing null-handling paths.
        price, ok = sane_exit_price(None, 46200, 1000)
        self.assertIsNone(price)
        self.assertTrue(ok)

    def test_none_entry_passes_through(self):
        # No reference point -> cannot judge; do not invent a fallback.
        price, ok = sane_exit_price(46350, None, 1000)
        self.assertEqual(price, 46350)
        self.assertTrue(ok)

    def test_floats_work(self):
        price, ok = sane_exit_price(45742.0, 45597.5, 1000)
        self.assertEqual(price, 45742.0)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
