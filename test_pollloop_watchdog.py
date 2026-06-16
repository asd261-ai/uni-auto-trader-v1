"""Tests for pollloop_watchdog. Pure stdlib unittest (runs on system python3, no deps).
Run:  python3 -m unittest test_pollloop_watchdog -v
Time is passed in explicitly, so these are deterministic and instant.
"""
from __future__ import annotations

import unittest

from pollloop_watchdog import PollLoopLivenessWatchdog


def make():
    return PollLoopLivenessWatchdog(freeze_threshold=120, check_interval=30, kill_grace=180)


class PollLoopLivenessWatchdogTests(unittest.TestCase):
    def test_invalid_construction_raises(self):
        with self.assertRaises(ValueError):
            PollLoopLivenessWatchdog(freeze_threshold=0, check_interval=30, kill_grace=180)

    def test_last_complete_age_none_until_first_record(self):
        wd = make()
        self.assertIsNone(wd.last_complete_age(1000.0))
        wd.record_poll_complete(1000.0)
        self.assertEqual(wd.last_complete_age(1005.0), 5.0)

    def test_no_kill_within_grace(self):
        wd = make()
        wd.record_poll_complete(1000.0)
        fired = []
        wd.check(1500.0, uptime=100, on_kill=fired.append)   # uptime 100 <= grace 180
        self.assertEqual(fired, [])

    def test_no_kill_when_no_iteration_yet(self):
        wd = make()
        fired = []
        wd.check(1000.0, uptime=999, on_kill=fired.append)   # _last_complete_ts == 0
        self.assertEqual(fired, [])

    def test_no_kill_when_healthy(self):
        wd = make()
        wd.record_poll_complete(1000.0)
        fired = []
        wd.check(1060.0, uptime=999, on_kill=fired.append)   # age 60 < 120
        self.assertEqual(fired, [])

    def test_kill_when_age_exceeds_threshold(self):
        wd = make()
        wd.record_poll_complete(1000.0)
        fired = []
        wd.check(1130.0, uptime=999, on_kill=fired.append)   # age 130 > 120
        self.assertEqual(len(fired), 1)
        self.assertIn("FROZEN", fired[0])

    def test_throttle_does_not_evaluate_within_interval(self):
        wd = make()
        wd.record_poll_complete(1000.0)
        fired = []
        wd.check(1115.0, uptime=999, on_kill=fired.append)   # age 115 healthy; _last_check=1115
        self.assertEqual(fired, [])
        wd.check(1140.0, uptime=999, on_kill=fired.append)   # 1140-1115=25 < 30 → throttled
        self.assertEqual(fired, [])                           # no fire despite age 140 > 120
        wd.check(1146.0, uptime=999, on_kill=fired.append)   # 1146-1115=31 > 30 → evaluates → fire
        self.assertEqual(len(fired), 1)

    def test_kill_fires_once_per_episode_then_rearms(self):
        wd = make()
        wd.record_poll_complete(1000.0)
        fired = []
        wd.check(1130.0, uptime=999, on_kill=fired.append)   # fire #1
        wd.check(1200.0, uptime=999, on_kill=fired.append)   # still frozen, latched → no fire
        self.assertEqual(len(fired), 1)
        wd.record_poll_complete(1210.0)                       # loop recovered → re-arm
        wd.check(1400.0, uptime=999, on_kill=fired.append)   # frozen again since 1210 → fire #2
        self.assertEqual(len(fired), 2)


if __name__ == "__main__":
    unittest.main()
