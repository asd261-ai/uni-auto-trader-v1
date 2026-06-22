"""Tests for fd_watchdog. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_fd_watchdog -v
Time, fd count and flat-ness are passed in explicitly — deterministic, instant.
"""
from __future__ import annotations

import unittest

from fd_watchdog import FdLeakWatchdog


def make():
    return FdLeakWatchdog(soft_threshold=800, hard_threshold=980, check_interval=30, kill_grace=180)


def cap():
    fired = []
    return fired, (lambda msg, tier: fired.append((tier, msg)))


class FdLeakWatchdogTests(unittest.TestCase):
    def test_invalid_construction_raises(self):
        with self.assertRaises(ValueError):
            FdLeakWatchdog(soft_threshold=0, hard_threshold=980, check_interval=30, kill_grace=180)
        with self.assertRaises(ValueError):   # hard < soft
            FdLeakWatchdog(soft_threshold=900, hard_threshold=800, check_interval=30, kill_grace=180)

    def test_no_fire_within_grace(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=2000, uptime=100, is_flat=True, on_kill=on_kill)  # uptime<=180
        self.assertEqual(fired, [])

    def test_no_fire_below_soft(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=799, uptime=999, is_flat=True, on_kill=on_kill)
        self.assertEqual(fired, [])

    def test_soft_fires_when_flat(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=800, uptime=999, is_flat=True, on_kill=on_kill)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "soft")
        self.assertIn("800", fired[0][1])

    def test_soft_does_not_fire_when_in_position(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=850, uptime=999, is_flat=False, on_kill=on_kill)  # below hard
        self.assertEqual(fired, [])

    def test_hard_fires_regardless_of_position(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=980, uptime=999, is_flat=False, on_kill=on_kill)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "hard")

    def test_hard_takes_precedence_over_soft(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=1000, uptime=999, is_flat=True, on_kill=on_kill)
        self.assertEqual(fired[0][0], "hard")

    def test_throttle_skips_evaluation_within_interval(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=500, uptime=999, is_flat=True, on_kill=on_kill)  # healthy; _last_check=1000
        self.assertEqual(fired, [])
        wd.check(1020.0, fd_count=2000, uptime=999, is_flat=True, on_kill=on_kill)  # 20<30 → throttled
        self.assertEqual(fired, [])
        wd.check(1031.0, fd_count=2000, uptime=999, is_flat=True, on_kill=on_kill)  # 31>30 → evaluates → hard
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "hard")


if __name__ == "__main__":
    unittest.main()
