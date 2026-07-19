"""Tests for margin_headroom. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_margin_headroom -v
"""
import unittest

from margin_headroom import headroom_low, read_failure_alert_due, margin_alert_due


class HeadroomLow(unittest.TestCase):
    MIN = 100000.0

    def test_below_floor_is_low(self):
        self.assertTrue(headroom_low(99999.0, self.MIN))
        self.assertTrue(headroom_low(0.0, self.MIN))
        self.assertTrue(headroom_low(50000, self.MIN))

    def test_at_or_above_floor_is_not_low(self):
        # Exactly at floor is NOT low (bot can still place its worst-case order).
        self.assertFalse(headroom_low(100000.0, self.MIN))
        self.assertFalse(headroom_low(100001.0, self.MIN))
        self.assertFalse(headroom_low(5_000_000, self.MIN))

    def test_disabled_when_floor_zero_or_negative(self):
        # Feature off → never low, even with zero available margin.
        self.assertFalse(headroom_low(0.0, 0))
        self.assertFalse(headroom_low(0.0, -1))
        self.assertFalse(headroom_low(10.0, 0.0))

    def test_disabled_when_floor_none(self):
        self.assertFalse(headroom_low(0.0, None))

    def test_fail_safe_on_none_ordexcess(self):
        # Bad / no broker read → never alert.
        self.assertFalse(headroom_low(None, self.MIN))

    def test_fail_safe_on_non_numeric(self):
        # Schema drift (string/garbage) → never alert.
        self.assertFalse(headroom_low("oops", self.MIN))
        self.assertFalse(headroom_low(object(), self.MIN))

    def test_floor_non_numeric_disables(self):
        self.assertFalse(headroom_low(0.0, "bad"))

    def test_numeric_strings_parse(self):
        # twdordexcess often arrives as a numeric string from the SDK.
        self.assertTrue(headroom_low("99999", self.MIN))
        self.assertFalse(headroom_low("150000", self.MIN))


class ReadFailureAlertDue(unittest.TestCase):
    """2026-07-17 night: the margin query returned no reliable read all night and the
    watcher stayed silent (debug-only). After N consecutive failed reads in an active
    session the caller must escalate once — fire exactly at streak == N so the alert
    is one-shot without extra latch state; a successful read resets the streak."""

    N = 10

    def test_fires_exactly_at_threshold(self):
        self.assertTrue(read_failure_alert_due(self.N, self.N))

    def test_silent_before_threshold(self):
        for streak in (0, 1, self.N - 1):
            self.assertFalse(read_failure_alert_due(streak, self.N), streak)

    def test_one_shot_past_threshold(self):
        # Streak keeps growing during an outage; alert must NOT repeat every cycle.
        for streak in (self.N + 1, self.N + 50):
            self.assertFalse(read_failure_alert_due(streak, self.N), streak)

    def test_disabled_when_n_zero_negative_or_none(self):
        self.assertFalse(read_failure_alert_due(10, 0))
        self.assertFalse(read_failure_alert_due(10, -1))
        self.assertFalse(read_failure_alert_due(10, None))


class MarginAlertDue(unittest.TestCase):
    """2026-07-19 audit (fresh-diff lens): the reject-driven margin alert shares
    the headroom latch, whose ONLY release is a successful margin read showing
    healthy headroom. If the query keeps failing (exactly the 7/17 pattern), the
    latch never clears and every later starvation episode is silent. Fix: the
    latch expires after rearm_sec — a new reject after that fires one more alert."""

    REARM = 4 * 3600

    def test_unlatched_is_due(self):
        self.assertTrue(margin_alert_due(False, 0.0, 1000.0, self.REARM))

    def test_latched_recent_not_due(self):
        now = 100_000.0
        self.assertFalse(margin_alert_due(True, now - 60, now, self.REARM))

    def test_latched_expired_is_due_again(self):
        now = 100_000.0
        self.assertTrue(margin_alert_due(True, now - self.REARM - 1, now, self.REARM))

    def test_rearm_disabled_keeps_pure_latch(self):
        # rearm_sec <= 0 or None → classic one-shot latch semantics.
        now = 100_000.0
        self.assertFalse(margin_alert_due(True, now - 999_999, now, 0))
        self.assertFalse(margin_alert_due(True, now - 999_999, now, None))


if __name__ == "__main__":
    unittest.main()
