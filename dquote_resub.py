"""Resubscribe rate-limit policy for the dquote tick feed (trigger A: staleness).

The broker SDK leaves the dquote feed dead after a failed/dropped subscribe with no
retry (2026-06-23 incident: a restart in the quote-maintenance window left the feed
dead ~1.5h until the tick-stale watchdog restarted the process). This policy decides
WHEN to attempt an in-place resubscribe — driven by the tick-stale watchdog's own
`alerting` signal (which already does session-anchored, weekend-gated staleness
detection), so this class only rate-limits: an uptime grace, a cooldown between
attempts, and a max number of attempts per outage episode (after which it stops and
lets the tick-stale kill restart the process as backstop). Resets when the feed
recovers (alerting clears).

Pure and dependency-free: time and the alerting flag are passed in; no clock, no SDK.
The actual unsubscribe/subscribe SDK call and observe/arm gating live in trader.py.
See docs/superpowers/specs/2026-06-23-dquote-resubscribe-design.md.
"""
from __future__ import annotations


class DquoteResubPolicy:
    def __init__(self, *, cooldown: float = 60.0, max_attempts: int = 3, grace: float = 180.0):
        if cooldown <= 0 or max_attempts <= 0 or grace <= 0:
            raise ValueError("cooldown, max_attempts, grace must be positive")
        self.cooldown = float(cooldown)
        self.max_attempts = int(max_attempts)
        self.grace = float(grace)
        self._last_attempt = 0.0
        self._attempts = 0

    def should_attempt(self, now: float, *, alerting: bool, uptime: float) -> bool:
        # anti boot: don't act until past the uptime grace
        if uptime <= self.grace:
            return False
        # feed healthy (tick-wd not alerting) -> episode over, re-arm fully:
        # clear the cooldown stamp too, or a new episode starting within `cooldown`
        # of the previous episode's last attempt gets its first attempt silently
        # delayed. Re-alerting already implies >=90s of fresh staleness (tick-wd
        # threshold) and trader.py holds its own min-interval guard, so the stale
        # stamp adds no protection across episodes.
        if not alerting:
            self._attempts = 0
            self._last_attempt = 0.0
            return False
        # stale episode: cap attempts, then defer to the tick-stale kill backstop
        if self._attempts >= self.max_attempts:
            return False
        if now - self._last_attempt < self.cooldown:
            return False
        self._last_attempt = now
        self._attempts += 1
        return True
