"""Tests for the pure TAIFEX holiday calendar.
Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_market_holidays -v
"""
import unittest
from datetime import date

from market_holidays import TW_MARKET_HOLIDAYS, is_market_holiday, is_trading_day
import market_holidays as mh


class HolidayCalendarTest(unittest.TestCase):
    def test_all_listed_holidays_are_weekdays(self):
        # Weekend-falling holidays are intentionally omitted (weekday check handles them).
        for iso in TW_MARKET_HOLIDAYS:
            d = date.fromisoformat(iso)
            self.assertLessEqual(d.weekday(), 4, f"{iso} should be a weekday in the set")

    def test_known_holidays_flagged(self):
        for iso in ("2026-01-01", "2026-02-17", "2026-06-19", "2026-09-25", "2026-12-25"):
            self.assertTrue(is_market_holiday(date.fromisoformat(iso)), iso)

    def test_typhoon_adhoc_closure_2026_07_10(self):
        # Ad-hoc typhoon closure (announced 7/9 evening). 7/10 day+night halted;
        # the 7/9 eve night session's dawn tail (7/10 00:00-05:00) stays active via
        # the prev-day tail check in _get_session, which this calendar must NOT block.
        self.assertFalse(is_trading_day(date(2026, 7, 10)))   # Fri typhoon closure
        self.assertTrue(is_trading_day(date(2026, 7, 9)))     # eve is a normal trading day
        self.assertTrue(is_trading_day(date(2026, 7, 13)))    # Mon reopen (assuming typhoon passed)

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


class CalendarExpiryTest(unittest.TestCase):
    """2026-07-19 audit: the table only covers 2026 — on 2027-01-01 (a Friday
    holiday) is_trading_day() silently returns True and the armed tick-stale
    kill restart-storms all day on the dead feed. The calendar must announce
    its own expiry instead of aging out silently."""

    def test_covered_year_no_warning(self):
        self.assertIsNone(mh.expiry_warning(date(2026, 7, 20)))

    def test_december_warns_about_next_uncovered_year(self):
        w = mh.expiry_warning(date(2026, 12, 1))
        self.assertIsNotNone(w)
        self.assertIn("2027", w)

    def test_uncovered_year_warns(self):
        self.assertIsNotNone(mh.expiry_warning(date(2027, 1, 1)))

    def test_uncovered_year_still_weekday_based(self):
        # Behavior unchanged (fail toward trading) — the warning is the defense.
        self.assertTrue(mh.is_trading_day(date(2027, 1, 4)))   # Mon
        self.assertFalse(mh.is_trading_day(date(2027, 1, 2)))  # Sat
