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
        """True boundary test: first event at t=1000, 20th event at t=1000+window_sec=1120.

        The cutoff is now - window = 1120 - 120 = 1000.  The filter is strict
        less-than (events[0] < cutoff), so events[0]=1000 is NOT dropped.
        All 20 events remain in the window → storm=True.
        """
        wd = DisconnectStormWatchdog(window_sec=WINDOW, max_disconnects=MAXN)
        # First 19 events at t=1000..1018
        for i in range(19):
            self.assertFalse(wd.record_and_check(1000.0 + i, active=True))
        # 20th event exactly at t = 1000 + window_sec = 1120
        # cutoff = 1120 - 120 = 1000; events[0]=1000 not < 1000 → kept
        self.assertTrue(wd.record_and_check(1000.0 + WINDOW, active=True))


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


class MonotonicGuard(unittest.TestCase):
    def test_clock_rollback_does_not_freeze_window(self):
        """Monotonic guard: a future timestamp followed by past timestamps must
        not cause the window to freeze indefinitely.

        Scenario: inject a far-future event (t=5000), then a burst of events at
        t=1000..1018 (clock appeared to roll back).  With monotonic clamping,
        each rolled-back timestamp is silently raised to 5000, so all events land
        at t=5000 and the window ages normally from that anchor.  After 19 more
        events the storm threshold is reached (demonstrating the window is not
        permanently jammed from the rollback).
        """
        wd = make()
        # Inject one far-future event to advance _last_ts to 5000
        wd.record_and_check(5000.0, active=True)
        # Now feed events with "past" timestamps — they get clamped to 5000
        # After 19 more (total 20 at effective t=5000) we should see a storm
        storm = False
        for i in range(19):
            storm = wd.record_and_check(1000.0 + i, active=True)
        self.assertTrue(storm)

    def test_reset_clears_monotonic_anchor(self):
        """reset() restores _last_ts so a fresh sequence is not affected by
        a prior future timestamp."""
        wd = make()
        wd.record_and_check(9999.0, active=True)
        wd.reset()
        # After reset, a normal sequence starting at t=1000 should work fine
        for i in range(19):
            wd.record_and_check(1000.0 + i, active=True)
        self.assertTrue(wd.record_and_check(1019.0, active=True))


class LatchFreeContract(unittest.TestCase):
    def test_storm_stays_true_on_subsequent_calls(self):
        """Latch-free contract: once a storm is detected, every subsequent call
        during the storm window continues to return True — not just the first.

        The caller (strategy.py) is responsible for one-shot behaviour (e.g.
        armed flag); this module never latches or auto-resets on detection.
        """
        wd = make()
        # Reach the storm threshold
        for i in range(20):
            wd.record_and_check(1000.0 + i, active=True)
        # Additional events — still inside the window — must keep returning True
        for i in range(5):
            result = wd.record_and_check(1019.0 + i, active=True)
            self.assertTrue(result, f"call {i+1} after storm should still be True")


class InputValidation(unittest.TestCase):
    def test_zero_window_raises(self):
        with self.assertRaises(ValueError):
            DisconnectStormWatchdog(window_sec=0)

    def test_negative_window_raises(self):
        with self.assertRaises(ValueError):
            DisconnectStormWatchdog(window_sec=-1.0)

    def test_zero_max_disconnects_raises(self):
        with self.assertRaises(ValueError):
            DisconnectStormWatchdog(max_disconnects=0)

    def test_negative_max_disconnects_raises(self):
        with self.assertRaises(ValueError):
            DisconnectStormWatchdog(max_disconnects=-5)


class Purity(unittest.TestCase):
    def test_module_source_has_no_os_exit(self):
        import disconnect_watchdog
        import inspect
        src = inspect.getsource(disconnect_watchdog)
        self.assertNotIn("os._exit", src)
        self.assertNotIn("import os", src)


if __name__ == "__main__":
    unittest.main()
