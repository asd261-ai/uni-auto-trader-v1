"""6/29 scenario regression at the pure-helper level (no SDK/threading).
Run:  python3 -m unittest test_cross_source_integration -v
"""
import unittest

import entry_guard as eg
import order_reject as orj


class June29Collision(unittest.TestCase):
    def test_fvg_long_blocked_when_mtx_short_held(self):
        # 12:30 MTX short open; 12:31 FVG long would collide.
        units = {"mtx": [{"dir": "short"}], "fvg": []}
        self.assertTrue(eg.cross_source_opposite(units, "fvg", "long"),
                        "FVG long must be flagged while MTX short is held")

    def test_rejected_exit_pend_cleared_before_next_fill(self):
        # FUF0092 close-reject for the short (B to close). Its exit pend must be removed so the
        # next entry fill is not mis-consumed.
        exit_pend = {"kind": "exit", "bs": "B", "pe": "PE-9G552"}
        pending = [exit_pend]
        got = orj.rollback_rejected_exit(pending, "MXFG6", "B", "MXFG6")
        self.assertIs(got, exit_pend)
        self.assertEqual(pending, [], "stale exit pend must be gone → no FIFO poison")
