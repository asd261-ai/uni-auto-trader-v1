"""Tests for exit_reason. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_exit_reason -v
"""
import unittest

from exit_reason import stop_hit_reason


class StopHitReasonTest(unittest.TestCase):
    def test_long_trailed_into_profit(self):
        self.assertEqual(stop_hit_reason("long", 44050, 44000), "trail")

    def test_long_original_protective_stop(self):
        self.assertEqual(stop_hit_reason("long", 43950, 44000), "loss")

    def test_long_breakeven_is_loss(self):
        self.assertEqual(stop_hit_reason("long", 44000, 44000), "loss")

    def test_short_trailed_into_profit_0906_case(self):
        self.assertEqual(stop_hit_reason("short", 44151, 44219), "trail")

    def test_short_original_protective_stop(self):
        self.assertEqual(stop_hit_reason("short", 44349, 44219), "loss")

    def test_short_breakeven_is_loss(self):
        self.assertEqual(stop_hit_reason("short", 44219, 44219), "loss")


if __name__ == "__main__":
    unittest.main()
