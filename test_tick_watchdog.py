"""
Tests for tick_watchdog. Pure stdlib unittest (runs on system python3, no deps).
Run:  python3 -m unittest test_tick_watchdog -v
Time is passed in explicitly, so these are deterministic and instant.
"""
from __future__ import annotations

import unittest

from tick_watchdog import TickStaleWatchdog

DAY, NIGHT, IVAL = 90.0, 300.0, 30.0


def make():
    wd = TickStaleWatchdog(day_threshold=DAY, night_threshold=NIGHT, check_interval=IVAL)
    msgs: list[str] = []
    return wd, msgs, msgs.append


class FreshFeed(unittest.TestCase):
    def test_recent_tick_no_alert(self):
        wd, msgs, notify = make()
        wd.record_tick(1000)
        wd.check(1000, "day", False, notify)
        self.assertEqual(msgs, [])
        self.assertFalse(wd.alerting)


class StaleDetection(unittest.TestCase):
    def test_day_stale_alerts_once(self):
        wd, msgs, notify = make()
        wd.record_tick(1000)
        wd.check(1000, "day", False, notify)          # anchor, no alert
        wd.check(1095, "day", False, notify)          # 95s > 90s -> alert
        self.assertEqual(len(msgs), 1)
        self.assertIn("STALE", msgs[0])
        self.assertTrue(wd.alerting)
        # subsequent stale checks must NOT re-alert (one-shot latch)
        wd.check(1130, "day", False, notify)
        wd.check(1200, "day", False, notify)
        self.assertEqual(len(msgs), 1)

    def test_recovery_clears_latch_and_notifies(self):
        wd, msgs, notify = make()
        wd.record_tick(1000)
        wd.check(1000, "day", False, notify)
        wd.check(1095, "day", False, notify)          # alert
        self.assertTrue(wd.alerting)
        wd.record_tick(1131)                          # ticks resume
        wd.check(1161, "day", False, notify)          # ref=1131, age=30 < 90 -> recovered
        self.assertEqual(len(msgs), 2)
        self.assertIn("recovered", msgs[1].lower())
        self.assertFalse(wd.alerting)

    def test_never_subscribed_alerts_after_grace(self):
        # no record_tick ever (subscribe failed at startup); enter day session
        wd, msgs, notify = make()
        wd.check(1000, "day", False, notify)          # transition -> grace anchor at 1000
        self.assertEqual(msgs, [])                    # within grace
        wd.check(1095, "day", False, notify)          # 95s since session start > 90 -> alert
        self.assertEqual(len(msgs), 1)
        self.assertIn("STALE", msgs[0])


class Gating(unittest.TestCase):
    def test_no_alert_during_break(self):
        wd, msgs, notify = make()
        wd.check(1000, "break", False, notify)
        wd.check(5000, "break", False, notify)        # long gap but market closed
        self.assertEqual(msgs, [])

    def test_no_alert_on_weekend(self):
        wd, msgs, notify = make()
        wd.check(1000, "day", True, notify)           # weekday()>=5
        wd.check(5000, "day", True, notify)
        self.assertEqual(msgs, [])

    def test_grace_after_break_to_session_transition(self):
        wd, msgs, notify = make()
        wd.check(1000, "break", False, notify)        # closed
        wd.check(1050, "day", False, notify)          # break->day: grace anchor at 1050
        self.assertEqual(msgs, [])                    # 0s into session
        wd.check(1145, "day", False, notify)          # 95s into session > 90 -> alert
        self.assertEqual(len(msgs), 1)


class SessionThresholds(unittest.TestCase):
    def test_night_tolerates_longer_gap_than_day(self):
        # night: 200s gap is fine (threshold 300)
        wd, msgs, notify = make()
        wd.record_tick(1000)
        wd.check(1000, "night", False, notify)
        wd.check(1200, "night", False, notify)        # 200 < 300 -> no alert
        self.assertEqual(msgs, [])
        wd.check(1310, "night", False, notify)        # 310 > 300 -> alert
        self.assertEqual(len(msgs), 1)

    def test_day_alerts_at_same_gap_night_tolerates(self):
        wd, msgs, notify = make()
        wd.record_tick(1000)
        wd.check(1000, "day", False, notify)
        wd.check(1200, "day", False, notify)          # 200 > 90 -> alert (day is strict)
        self.assertEqual(len(msgs), 1)


class Throttle(unittest.TestCase):
    def test_eval_is_throttled_to_check_interval(self):
        # threshold tiny so it WOULD alert, but throttle defers the eval
        wd = TickStaleWatchdog(day_threshold=10, night_threshold=10, check_interval=30)
        msgs: list[str] = []
        wd.record_tick(1000)
        wd.check(1000, "day", False, msgs.append)     # anchor, last_check=1000
        wd.check(1015, "day", False, msgs.append)     # 15s < 30s interval -> throttled, no eval
        self.assertEqual(msgs, [])                    # even though age 15 > threshold 10
        wd.check(1031, "day", False, msgs.append)     # 31s >= interval -> eval, age 31 > 10 -> alert
        self.assertEqual(len(msgs), 1)


class Helpers(unittest.TestCase):
    def test_last_tick_age(self):
        wd, _msgs, _notify = make()
        self.assertIsNone(wd.last_tick_age(1000))     # no tick yet
        wd.record_tick(1000)
        self.assertEqual(wd.last_tick_age(1030), 30)

    def test_record_tick_independent_of_position_state(self):
        # The watchdog only knows about ticks, not positions. This guards the integration
        # contract: record_tick must be called for EVERY tick (flat or not), which is why
        # the strategy hook goes ABOVE on_tick's `if not all_units: return`.
        wd, msgs, notify = make()
        wd.record_tick(1000)
        wd.record_tick(1100)                          # ticks keep arriving while flat
        wd.check(1100, "day", False, notify)
        self.assertEqual(msgs, [])
        self.assertEqual(wd.last_tick_age(1100), 0)

    def test_invalid_config_rejected(self):
        for kw in ({"day_threshold": 0}, {"night_threshold": -1}, {"check_interval": 0}):
            with self.assertRaises(ValueError):
                TickStaleWatchdog(**kw)


class KillTier(unittest.TestCase):
    def _wd(self):
        return TickStaleWatchdog(
            day_threshold=DAY, night_threshold=NIGHT, check_interval=IVAL,
            kill_day_threshold=180.0, kill_night_threshold=600.0, kill_grace=120.0,
        )

    def test_kill_fires_when_stale_beyond_kill_threshold_and_past_grace(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        # age 200s > kill 180s, uptime 500s > grace 120s, day session
        wd.check(1200, "day", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        self.assertEqual(len(kills), 1)
        self.assertIn("escalating", kills[0])

    def test_kill_suppressed_within_grace(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        wd.check(1200, "day", False, lambda m: None, uptime=60.0, on_kill=kills.append)
        self.assertEqual(kills, [])

    def test_kill_not_fired_below_kill_threshold(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        # age 100s < kill 180s (alert fires at 90s but kill does not)
        wd.check(1100, "day", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        self.assertEqual(kills, [])

    def test_kill_skipped_on_break_and_weekend(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        wd.check(1300, "break", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        wd.check(1300, "day", True, lambda m: None, uptime=500.0, on_kill=kills.append)
        self.assertEqual(kills, [])

    def test_kill_one_shot_until_recovery(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        wd.check(1200, "day", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        wd.check(1240, "day", False, lambda m: None, uptime=540.0, on_kill=kills.append)
        self.assertEqual(len(kills), 1)            # latched, not re-fired
        wd.record_tick(1240)                       # feed recovers
        wd.check(1241, "day", False, lambda m: None, uptime=541.0, on_kill=kills.append)
        wd.record_tick(1241)
        wd.check(1500, "day", False, lambda m: None, uptime=800.0, on_kill=kills.append)
        # stale again after recovery → kill may fire again
        self.assertEqual(len(kills), 2)

    def test_kill_noop_when_callback_or_uptime_absent(self):
        wd = self._wd()
        wd.record_tick(1000)
        wd.check(1200, "day", False, lambda m: None)                 # no on_kill / uptime
        wd.check(1200, "day", False, lambda m: None, uptime=500.0)   # no on_kill
        # no exception, nothing to assert beyond "did not raise"

    def test_kill_invalid_config_rejected(self):
        for kw in ({"kill_day_threshold": 0}, {"kill_night_threshold": -1}, {"kill_grace": 0}):
            with self.assertRaises(ValueError):
                TickStaleWatchdog(**kw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
