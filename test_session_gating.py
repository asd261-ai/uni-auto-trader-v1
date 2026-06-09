"""Tests for weekday-aware _get_session + the Monday-dawn watchdog gating fix.
Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_session_gating -v

Weekday anchors (week of 2026-06-08):
  08 Mon(0) 09 Tue(1) 10 Wed(2) 11 Thu(3) 12 Fri(4) 13 Sat(5) 14 Sun(6)
_get_session only reads dt.time() and dt.weekday() -> naive datetimes are fine.
"""
import unittest
from datetime import datetime

from strategy import _get_session
from tick_watchdog import TickStaleWatchdog

MON, TUE, WED, THU, FRI, SAT, SUN = 8, 9, 10, 11, 12, 13, 14


def dt(day, hh, mm=0):
    return datetime(2026, 6, day, hh, mm)


class GetSessionTest(unittest.TestCase):
    def test_day_session_mon_to_fri(self):
        for day in (MON, TUE, WED, THU, FRI):
            self.assertEqual(_get_session(dt(day, 10, 0)), "day", day)

    def test_day_window_is_break_on_weekend(self):
        for day in (SAT, SUN):
            self.assertEqual(_get_session(dt(day, 10, 0)), "break", day)

    def test_night_evening_leg_mon_to_fri(self):
        for day in (MON, TUE, WED, THU, FRI):
            self.assertEqual(_get_session(dt(day, 20, 0)), "night", day)

    def test_night_evening_leg_break_on_weekend(self):
        for day in (SAT, SUN):
            self.assertEqual(_get_session(dt(day, 20, 0)), "break", day)

    def test_night_tail_leg_tue_to_sat(self):
        for day in (TUE, WED, THU, FRI, SAT):
            self.assertEqual(_get_session(dt(day, 2, 0)), "night", day)

    def test_monday_dawn_is_break(self):
        # THE FIX: Monday 00:00-05:00 is not a real night session.
        self.assertEqual(_get_session(dt(MON, 0, 0)), "break")
        self.assertEqual(_get_session(dt(MON, 3, 0)), "break")
        self.assertEqual(_get_session(dt(MON, 4, 59)), "break")

    def test_sunday_dawn_is_break(self):
        self.assertEqual(_get_session(dt(SUN, 2, 0)), "break")

    def test_boundaries_monday(self):
        self.assertEqual(_get_session(dt(MON, 5, 0)), "break")
        self.assertEqual(_get_session(dt(MON, 8, 44)), "break")
        self.assertEqual(_get_session(dt(MON, 8, 45)), "day")
        self.assertEqual(_get_session(dt(MON, 13, 44)), "day")
        self.assertEqual(_get_session(dt(MON, 13, 45)), "break")
        self.assertEqual(_get_session(dt(MON, 14, 59)), "break")
        self.assertEqual(_get_session(dt(MON, 15, 0)), "night")

    def test_boundaries_tuesday_tail(self):
        self.assertEqual(_get_session(dt(TUE, 0, 0)), "night")
        self.assertEqual(_get_session(dt(TUE, 4, 59)), "night")
        self.assertEqual(_get_session(dt(TUE, 5, 0)), "break")


class WatchdogMondayDawnGatingTest(unittest.TestCase):
    def test_dead_feed_monday_dawn_no_alert_no_kill(self):
        # Reproduces 6/8 00:00 would-fire: feed dead ~9h, long uptime, not weekend (Mon).
        # With the fix, session=break -> watchdog gate suppresses both alert and kill.
        wd = TickStaleWatchdog(day_threshold=90.0, night_threshold=300.0,
                               check_interval=30.0)
        msgs, kills = [], []
        wd.record_tick(0.0)
        session = _get_session(dt(MON, 0, 0))
        self.assertEqual(session, "break")          # precondition: fix in effect
        wd.check(32419.0, session, False,           # is_weekend=False (Monday)
                 msgs.append, uptime=57651.0, on_kill=kills.append)
        self.assertEqual(msgs, [])                  # no stale alert
        self.assertEqual(kills, [])                 # no kill escalation

    def test_dead_feed_tuesday_night_still_fires(self):
        # Control: Tuesday 02:00 IS a real night session -> watchdog still escalates.
        # Two checks: first enters the session (grace anchor), then a stale gap later.
        wd = TickStaleWatchdog(day_threshold=90.0, night_threshold=300.0,
                               check_interval=30.0)
        msgs, kills = [], []
        session = _get_session(dt(TUE, 2, 0))
        self.assertEqual(session, "night")
        wd.record_tick(100.0)
        wd.check(100.0, session, False, msgs.append,
                 uptime=57651.0, on_kill=kills.append)   # enter session, age=0, no fire
        wd.check(7300.0, session, False, msgs.append,
                 uptime=57651.0, on_kill=kills.append)   # 7200s stale > kill 600s -> fire
        self.assertNotEqual(msgs, [])
        self.assertNotEqual(kills, [])
