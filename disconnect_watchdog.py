"""
Disconnect-storm detector — pure, side-effect-free, unit-testable.

Counts broker disconnect events in a trailing sliding window. When the count
reaches `max_disconnects` within `window_sec` AND the session is active, the
caller treats it as a storm and the caller should self-restart via systemd
BEFORE the SDK's native callback recursion overflows the CPython C stack (the
recurring ip 0x551368 SEGV; see docs/superpowers/specs/2026-06-15-disconnect-
storm-circuit-breaker-design.md).

When inactive (weekend / broker maintenance / break), disconnects are expected:
the window is cleared and no storm is reported, so the trader never restart-loops
while the market is closed (the Monday-dawn-storm lesson).

Side effects (process exit, env, Telegram) live in strategy.py, not here.
"""
from __future__ import annotations

from collections import deque


class DisconnectStormWatchdog:
    def __init__(self, *, window_sec: float = 120.0, max_disconnects: int = 20):
        self._window = window_sec
        self._max = max_disconnects
        self._events: deque = deque()

    def record_and_check(self, now: float, *, active: bool) -> bool:
        """Record a disconnect at `now`; return True iff it constitutes a storm.

        A storm = at least `max_disconnects` events within the trailing
        `window_sec`. When `active` is False the window is cleared and False is
        returned (market-closed disconnects are expected and must not trip)."""
        if not active:
            self._events.clear()
            return False
        self._events.append(now)
        cutoff = now - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events) >= self._max

    def reset(self) -> None:
        self._events.clear()
