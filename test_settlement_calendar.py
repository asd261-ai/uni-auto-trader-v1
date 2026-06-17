"""Tests for settlement_calendar. Pure stdlib unittest (runs on system python3).
Run:  python3 -m unittest test_settlement_calendar -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from settlement_calendar import third_wednesday, is_settlement_window


class ThirdWednesdayTests(unittest.TestCase):
    def test_month_starting_monday(self):
        # June 2026 starts on a Monday -> 3rd Wed = June 17 (the 2026-06-17 settlement)
        self.assertEqual(third_wednesday(2026, 6), date(2026, 6, 17))

    def test_month_starting_wednesday(self):
        # July 2026 starts on a Wednesday -> 3rd Wed = July 15 (next settlement)
        self.assertEqual(third_wednesday(2026, 7), date(2026, 7, 15))

    def test_month_starting_thursday(self):
        # Jan 2026 starts on a Thursday -> first Wed = Jan 7 -> 3rd Wed = Jan 21
        self.assertEqual(third_wednesday(2026, 1), date(2026, 1, 21))


class IsSettlementWindowTests(unittest.TestCase):
    def test_inside_window_on_settlement_day(self):
        self.assertTrue(is_settlement_window(datetime(2026, 6, 17, 13, 35)))
        self.assertTrue(is_settlement_window(datetime(2026, 6, 17, 13, 30)))   # inclusive start
        self.assertTrue(is_settlement_window(datetime(2026, 6, 17, 14, 59)))

    def test_boundaries_excluded(self):
        self.assertFalse(is_settlement_window(datetime(2026, 6, 17, 13, 29)))  # before 13:30
        self.assertFalse(is_settlement_window(datetime(2026, 6, 17, 15, 0)))   # 15:00 exclusive end

    def test_non_settlement_day(self):
        self.assertFalse(is_settlement_window(datetime(2026, 6, 16, 13, 35)))  # day before
        self.assertFalse(is_settlement_window(datetime(2026, 6, 18, 13, 35)))  # day after

    def test_override_date(self):
        # Holiday shifted settlement to 2026-06-18; that day is now in-window, 3rd-Wed is not.
        ov = date(2026, 6, 18)
        self.assertTrue(is_settlement_window(datetime(2026, 6, 18, 13, 35), override_date=ov))
        self.assertFalse(is_settlement_window(datetime(2026, 6, 17, 13, 35), override_date=ov))


if __name__ == "__main__":
    unittest.main()
