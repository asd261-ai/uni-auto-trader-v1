"""Tests for margin_headroom. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_margin_headroom -v
"""
import unittest

from margin_headroom import headroom_low


class HeadroomLow(unittest.TestCase):
    MIN = 100000.0

    def test_below_floor_is_low(self):
        self.assertTrue(headroom_low(99999.0, self.MIN))
        self.assertTrue(headroom_low(0.0, self.MIN))
        self.assertTrue(headroom_low(50000, self.MIN))

    def test_at_or_above_floor_is_not_low(self):
        # Exactly at floor is NOT low (bot can still place its worst-case order).
        self.assertFalse(headroom_low(100000.0, self.MIN))
        self.assertFalse(headroom_low(100001.0, self.MIN))
        self.assertFalse(headroom_low(5_000_000, self.MIN))

    def test_disabled_when_floor_zero_or_negative(self):
        # Feature off → never low, even with zero available margin.
        self.assertFalse(headroom_low(0.0, 0))
        self.assertFalse(headroom_low(0.0, -1))
        self.assertFalse(headroom_low(10.0, 0.0))

    def test_disabled_when_floor_none(self):
        self.assertFalse(headroom_low(0.0, None))

    def test_fail_safe_on_none_ordexcess(self):
        # Bad / no broker read → never alert.
        self.assertFalse(headroom_low(None, self.MIN))

    def test_fail_safe_on_non_numeric(self):
        # Schema drift (string/garbage) → never alert.
        self.assertFalse(headroom_low("oops", self.MIN))
        self.assertFalse(headroom_low(object(), self.MIN))

    def test_floor_non_numeric_disables(self):
        self.assertFalse(headroom_low(0.0, "bad"))

    def test_numeric_strings_parse(self):
        # twdordexcess often arrives as a numeric string from the SDK.
        self.assertTrue(headroom_low("99999", self.MIN))
        self.assertFalse(headroom_low("150000", self.MIN))


if __name__ == "__main__":
    unittest.main()
