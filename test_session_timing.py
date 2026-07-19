"""Tests for session_timing. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_session_timing -v
"""
import unittest

import session_timing as st

DELAY = 300.0


class SessionSummaryActionTest(unittest.TestCase):
    def test_day_to_break_defers(self):
        a = st.session_summary_action("day", "break", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertEqual(a["pending_session"], "day")
        self.assertEqual(a["due_at"], 1300.0)

    def test_night_to_break_defers(self):
        a = st.session_summary_action("night", "break", None, 0.0, 2000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertEqual(a["pending_session"], "night")
        self.assertEqual(a["due_at"], 2300.0)

    def test_poll_before_due_no_fire(self):
        a = st.session_summary_action("break", "break", "day", 1300.0, 1200.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertEqual(a["pending_session"], "day")
        self.assertEqual(a["due_at"], 1300.0)

    def test_poll_at_due_fires(self):
        a = st.session_summary_action("break", "break", "day", 1300.0, 1300.0, DELAY)
        self.assertEqual(a["fire"], "day")
        self.assertIsNone(a["pending_session"])
        self.assertEqual(a["due_at"], 0.0)

    def test_poll_after_due_fires(self):
        a = st.session_summary_action("break", "break", "day", 1300.0, 9999.0, DELAY)
        self.assertEqual(a["fire"], "day")
        self.assertIsNone(a["pending_session"])
        self.assertEqual(a["due_at"], 0.0)

    def test_no_transition_nothing_pending_noop(self):
        a = st.session_summary_action("day", "day", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertIsNone(a["pending_session"])

    def test_transition_with_stale_pending_fires_it_first(self):
        a = st.session_summary_action("night", "break", "day", 500.0, 2000.0, DELAY)
        self.assertEqual(a["fire"], "day")
        self.assertEqual(a["pending_session"], "night")
        self.assertEqual(a["due_at"], 2300.0)

    def test_break_to_day_no_summary(self):
        a = st.session_summary_action("break", "day", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertIsNone(a["pending_session"])

    def test_break_to_night_no_summary(self):
        a = st.session_summary_action("break", "night", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertIsNone(a["pending_session"])


class WeekendDormantTest(unittest.TestCase):
    """2026-07-19 audit: `weekday() >= 5` alone treats Sat 00:00–05:00 — the live
    tail of Friday's night session — as weekend: poll muted (held positions lose
    exit management) and recon/margin/tick watchdogs dormant while the market
    trades. Dormancy must require BOTH a weekend calendar day AND no active
    session (_get_session already knows the Sat-dawn tail is 'night')."""

    def test_sat_dawn_active_night_session_not_dormant(self):
        self.assertFalse(st.weekend_dormant(5, "night"))

    def test_sat_after_close_dormant(self):
        self.assertTrue(st.weekend_dormant(5, "break"))

    def test_sunday_dormant(self):
        self.assertTrue(st.weekend_dormant(6, "break"))

    def test_weekday_break_not_dormant(self):
        # Weekday lunch break: not weekend — other gates own this window.
        self.assertFalse(st.weekend_dormant(2, "break"))

    def test_weekday_sessions_not_dormant(self):
        self.assertFalse(st.weekend_dormant(0, "day"))
        self.assertFalse(st.weekend_dormant(4, "night"))
