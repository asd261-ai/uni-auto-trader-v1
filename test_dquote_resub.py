"""Tests for dquote_resub. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_dquote_resub -v
Time, alerting flag and uptime are passed in — deterministic, instant.
"""
from __future__ import annotations

import unittest

from dquote_resub import DquoteResubPolicy


def make():
    return DquoteResubPolicy(cooldown=60, max_attempts=3, grace=180)


class DquoteResubPolicyTests(unittest.TestCase):
    def test_invalid_construction_raises(self):
        with self.assertRaises(ValueError):
            DquoteResubPolicy(cooldown=0, max_attempts=3, grace=180)
        with self.assertRaises(ValueError):
            DquoteResubPolicy(cooldown=60, max_attempts=0, grace=180)

    def test_no_attempt_within_grace(self):
        p = make()
        self.assertFalse(p.should_attempt(1000.0, alerting=True, uptime=100))  # uptime<=180

    def test_no_attempt_when_not_alerting(self):
        p = make()
        self.assertFalse(p.should_attempt(1000.0, alerting=False, uptime=999))

    def test_attempts_when_alerting_past_grace(self):
        p = make()
        self.assertTrue(p.should_attempt(1000.0, alerting=True, uptime=999))

    def test_cooldown_blocks_second_attempt(self):
        p = make()
        self.assertTrue(p.should_attempt(1000.0, alerting=True, uptime=999))   # attempt 1
        self.assertFalse(p.should_attempt(1030.0, alerting=True, uptime=999))  # 30 < 60 cooldown
        self.assertTrue(p.should_attempt(1061.0, alerting=True, uptime=999))   # 61 > 60 -> attempt 2

    def test_max_attempts_then_stop(self):
        p = make()
        self.assertTrue(p.should_attempt(1000.0, alerting=True, uptime=999))   # 1
        self.assertTrue(p.should_attempt(1100.0, alerting=True, uptime=999))   # 2
        self.assertTrue(p.should_attempt(1200.0, alerting=True, uptime=999))   # 3
        self.assertFalse(p.should_attempt(1300.0, alerting=True, uptime=999))  # max reached -> stop

    def test_recovery_resets_episode(self):
        p = make()
        for t in (1000.0, 1100.0, 1200.0):
            p.should_attempt(t, alerting=True, uptime=999)                     # exhaust 3
        self.assertFalse(p.should_attempt(1300.0, alerting=True, uptime=999))  # capped
        self.assertFalse(p.should_attempt(1400.0, alerting=False, uptime=999)) # feed recovered -> reset
        self.assertTrue(p.should_attempt(1500.0, alerting=True, uptime=999))   # new episode -> attempt


if __name__ == "__main__":
    unittest.main()
