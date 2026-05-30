"""Tests for open_freeze.in_open_freeze_window. Pure stdlib unittest.
Run:  python3 -m unittest test_open_freeze -v

Rule: no entry AND no exit may fire in the first `freeze_secs` seconds after a
session open (day 08:45:00, night 15:00:00 TW). This is the pure predicate; the
order-path gating lives in strategy.py. freeze_secs<=0 (or invalid) disables.
"""
import unittest
from datetime import datetime

from open_freeze import in_open_freeze_window


def dt(h, m, s=0):
    return datetime(2026, 5, 30, h, m, s)  # date irrelevant; predicate is time-of-day


class OpenFreezeDisabledTest(unittest.TestCase):
    # freeze_secs<=0 / invalid → never freeze (fail-open, mirrors atr_gate style)
    def test_disabled_zero(self):
        self.assertFalse(in_open_freeze_window(dt(8, 45, 0), 0))

    def test_disabled_negative(self):
        self.assertFalse(in_open_freeze_window(dt(8, 45, 0), -1))

    def test_disabled_non_int_string(self):
        self.assertFalse(in_open_freeze_window(dt(8, 45, 0), "300"))

    def test_disabled_none(self):
        self.assertFalse(in_open_freeze_window(dt(8, 45, 0), None))

    def test_disabled_bool_true(self):
        # bool is an int subclass in Python — exclude explicitly so True != 1s window
        self.assertFalse(in_open_freeze_window(dt(8, 45, 0), True))


class OpenFreezeDayOpenTest(unittest.TestCase):
    # Day session opens 08:45:00 → window = 08:45:00–08:49:59 at freeze_secs=300
    def test_exact_open_in(self):
        self.assertTrue(in_open_freeze_window(dt(8, 45, 0), 300))

    def test_mid_window_in(self):
        self.assertTrue(in_open_freeze_window(dt(8, 47, 30), 300))

    def test_last_second_in(self):
        self.assertTrue(in_open_freeze_window(dt(8, 49, 59), 300))

    def test_window_end_boundary_out(self):
        # strict < freeze_secs: 08:50:00 is the first tradable second
        self.assertFalse(in_open_freeze_window(dt(8, 50, 0), 300))

    def test_one_second_before_open_out(self):
        self.assertFalse(in_open_freeze_window(dt(8, 44, 59), 300))


class OpenFreezeNightOpenTest(unittest.TestCase):
    # Night session opens 15:00:00 → window = 15:00:00–15:04:59 at freeze_secs=300
    def test_exact_open_in(self):
        self.assertTrue(in_open_freeze_window(dt(15, 0, 0), 300))

    def test_mid_window_in(self):
        self.assertTrue(in_open_freeze_window(dt(15, 2, 0), 300))

    def test_last_second_in(self):
        self.assertTrue(in_open_freeze_window(dt(15, 4, 59), 300))

    def test_window_end_boundary_out(self):
        self.assertFalse(in_open_freeze_window(dt(15, 5, 0), 300))

    def test_one_second_before_open_out(self):
        self.assertFalse(in_open_freeze_window(dt(14, 59, 59), 300))


class OpenFreezeOtherTimesTest(unittest.TestCase):
    # Everything outside the two open windows must be tradable.
    def test_midday_out(self):
        self.assertFalse(in_open_freeze_window(dt(10, 0, 0), 300))

    def test_deep_night_out(self):
        self.assertFalse(in_open_freeze_window(dt(2, 0, 0), 300))

    def test_session_break_1400_out(self):
        self.assertFalse(in_open_freeze_window(dt(14, 0, 0), 300))

    def test_day_close_1345_out(self):
        self.assertFalse(in_open_freeze_window(dt(13, 45, 0), 300))

    def test_night_close_0500_out(self):
        self.assertFalse(in_open_freeze_window(dt(5, 0, 0), 300))


class OpenFreezeCustomWindowTest(unittest.TestCase):
    # Window length tracks freeze_secs exactly (env-tunable).
    def test_60s_inside(self):
        self.assertTrue(in_open_freeze_window(dt(8, 45, 30), 60))

    def test_60s_outside(self):
        self.assertFalse(in_open_freeze_window(dt(8, 46, 0), 60))

    def test_600s_extends_window(self):
        # 10-min window: 08:54:59 still frozen
        self.assertTrue(in_open_freeze_window(dt(8, 54, 59), 600))
        self.assertFalse(in_open_freeze_window(dt(8, 55, 0), 600))


if __name__ == "__main__":
    unittest.main()
