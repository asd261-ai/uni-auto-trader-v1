"""Tests for the pure TAIFEX holiday calendar.
Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_market_holidays -v
"""
import unittest
from datetime import date

from market_holidays import TW_MARKET_HOLIDAYS, is_market_holiday, is_trading_day


class HolidayCalendarTest(unittest.TestCase):
    def test_all_listed_holidays_are_weekdays(self):
        # Weekend-falling holidays are intentionally omitted (weekday check handles them).
        for iso in TW_MARKET_HOLIDAYS:
            d = date.fromisoformat(iso)
            self.assertLessEqual(d.weekday(), 4, f"{iso} should be a weekday in the set")

    def test_known_holidays_flagged(self):
        for iso in ("2026-01-01", "2026-02-17", "2026-06-19", "2026-09-25", "2026-12-25"):
            self.assertTrue(is_market_holiday(date.fromisoformat(iso)), iso)

    def test_non_holidays_not_flagged(self):
        for iso in ("2026-06-18", "2026-06-22", "2026-02-23", "2026-09-24"):
            self.assertFalse(is_market_holiday(date.fromisoformat(iso)), iso)

    def test_is_trading_day(self):
        self.assertTrue(is_trading_day(date(2026, 6, 18)))    # Thu, normal
        self.assertTrue(is_trading_day(date(2026, 6, 22)))    # Mon, post-端午 reopen
        self.assertFalse(is_trading_day(date(2026, 6, 19)))   # 端午 (Fri holiday)
        self.assertFalse(is_trading_day(date(2026, 6, 20)))   # Sat (weekend)
        self.assertFalse(is_trading_day(date(2026, 2, 16)))   # 除夕 (Mon holiday)

    def test_lunar_new_year_cluster(self):
        for dom in (12, 13, 16, 17, 18, 19, 20):
            self.assertFalse(is_trading_day(date(2026, 2, dom)), f"2026-02-{dom}")
        self.assertTrue(is_trading_day(date(2026, 2, 23)))    # reopen Mon


if __name__ == "__main__":
    unittest.main()
