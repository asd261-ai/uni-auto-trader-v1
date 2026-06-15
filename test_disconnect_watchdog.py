"""
Tests for disconnect_watchdog. Pure stdlib unittest (runs on system python3, no deps).
Run:  python3 -m unittest test_disconnect_watchdog -v
Time is passed in explicitly, so these are deterministic and instant.
"""
from __future__ import annotations

import unittest

from disconnect_watchdog import DisconnectStormWatchdog

WINDOW, MAXN = 120.0, 20


def make():
    return DisconnectStormWatchdog(window_sec=WINDOW, max_disconnects=MAXN)


class BelowThreshold(unittest.TestCase):
    def test_few_disconnects_no_storm(self):
        wd = make()
        for i in range(19):
            storm = wd.record_and_check(1000.0 + i, active=True)
        self.assertFalse(storm)

    def test_single_disconnect_no_storm(self):
        wd = make()
        self.assertFalse(wd.record_and_check(1000.0, active=True))


class AtThreshold(unittest.TestCase):
    def test_max_within_window_is_storm(self):
        wd = make()
        storm = False
        for i in range(20):
            storm = wd.record_and_check(1000.0 + i, active=True)
        self.assertTrue(storm)

    def test_boundary_exactly_window(self):
        wd = make()
        for i in range(19):
            self.assertFalse(wd.record_and_check(1000.0 + i, active=True))
        self.assertTrue(wd.record_and_check(1019.0, active=True))


class WindowAging(unittest.TestCase):
    def test_events_older_than_window_drop_out(self):
        wd = make()
        storm = False
        for i in range(25):
            storm = wd.record_and_check(1000.0 + i * 10.0, active=True)
        self.assertFalse(storm)

    def test_recovered_blip_does_not_accumulate(self):
        wd = make()
        for i in range(10):
            wd.record_and_check(1000.0 + i, active=True)
        for i in range(10):
            storm = wd.record_and_check(2000.0 + i, active=True)
        self.assertFalse(storm)


class SessionGating(unittest.TestCase):
    def test_inactive_clears_and_never_storms(self):
        wd = make()
        storm = False
        for i in range(30):
            storm = wd.record_and_check(1000.0 + i, active=False)
        self.assertFalse(storm)

    def test_inactive_resets_then_active_starts_fresh(self):
        wd = make()
        for i in range(19):
            wd.record_and_check(1000.0 + i, active=True)
        self.assertFalse(wd.record_and_check(1019.0, active=False))
        storm = False
        for i in range(19):
            storm = wd.record_and_check(1020.0 + i, active=True)
        self.assertFalse(storm)


class Purity(unittest.TestCase):
    def test_module_source_has_no_os_exit(self):
        import disconnect_watchdog
        import inspect
        src = inspect.getsource(disconnect_watchdog)
        self.assertNotIn("os._exit", src)
        self.assertNotIn("import os", src)


if __name__ == "__main__":
    unittest.main()
