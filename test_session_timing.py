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
