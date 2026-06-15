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

Latch-free by design: while a storm is active (threshold events remain in the
trailing window), every call to `record_and_check` returns True — not only the
first crossing.  The caller (strategy.py) is responsible for one-shot behaviour
via an armed flag; strategy.py exits the process on the first True it acts on,
so subsequent Trues are never reached in the armed phase.  In observe-only
(Phase A) mode multiple Trues generate repeated log lines which help calibrate
the storm's breadth — suppressing them here would lose that signal.

Side effects (process exit, env, Telegram) live in strategy.py, not here.

Thread safety
-------------
This class holds no internal lock.  `_events` (a deque) and `_last_ts` are
accessed without synchronisation.  The design assumes the caller invokes
`record_and_check` from a single thread (the broker SDK's callback thread in
the standard deployment).  If the caller might invoke it concurrently, the
caller must serialise access externally (e.g. wrap each call in a threading
lock).

Monotonic timestamp guarantee
------------------------------
`record_and_check` enforces that stored timestamps never go backwards.  If the
wall clock steps back (NTP correction, DST fold, VM resume/snapshot) a raw `now`
smaller than the last recorded timestamp would cause future events to sit far
ahead in the deque and never age out — freezing the window in "storm" state
permanently.  The guard clamps: ``now = max(now, self._last_ts)`` before
appending, so a rollback becomes a no-op advance (the event lands at the same
instant as the previous one) and the window self-heals as real time moves
forward.

When `active` is False the event window is cleared, but `_last_ts` is
intentionally *not* reset.  If the clock rolled forward to an absurd value just
before going inactive, resetting the anchor here would let a stale far-future
timestamp re-inject the same freeze on the next active burst.  Keeping the
anchor across inactive periods is the safer choice.  `reset()` *does* clear
`_last_ts` because a manual reset signals an intentional restart of the entire
detection cycle (e.g. after a confirmed clean reconnect).
"""
from __future__ import annotations

from collections import deque


class DisconnectStormWatchdog:
    def __init__(self, *, window_sec: float = 120.0, max_disconnects: int = 20):
        if window_sec <= 0:
            raise ValueError(
                f"window_sec must be positive, got {window_sec!r}"
            )
        if max_disconnects <= 0:
            raise ValueError(
                f"max_disconnects must be positive, got {max_disconnects!r}"
            )
        self._window = window_sec
        self._max = max_disconnects
        self._events: deque[float] = deque()
        self._last_ts: float = float("-inf")

    def record_and_check(self, now: float, *, active: bool) -> bool:
        """Record a disconnect at `now`; return True iff it constitutes a storm.

        A storm = at least `max_disconnects` events within the trailing
        `window_sec`. When `active` is False the window is cleared and False is
        returned (market-closed disconnects are expected and must not trip).

        Repeated True returns are intentional (latch-free — see module docstring).
        """
        if not active:
            self._events.clear()
            # _last_ts intentionally preserved across inactive periods;
            # see module docstring for rationale.
            return False
        # Monotonic guard: never let timestamps go backwards.
        now = max(now, self._last_ts)
        self._last_ts = now
        self._events.append(now)
        cutoff = now - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events) >= self._max

    def reset(self) -> None:
        """Clear the event window and the monotonic anchor.

        Intentionally NOT wired to on_reconnect.  This class operates as a
        pure sliding window: rapid flapping (disconnect ↔ reconnect in quick
        succession) must accumulate in the window to trigger the breaker.
        Resetting on every reconnect would drain the counter before it reaches
        the threshold, rendering the breaker ineffective against the exact
        storm it is designed to catch (high-frequency cycling causes the SDK
        to overflow the CPython C stack regardless of reconnect interleaving).
        Only the passage of time ages events out via the window.

        Use reset() in tests or when intentionally restarting the entire
        detection lifecycle (e.g. a clean process restart).  Resets _last_ts
        so the subsequent timestamp sequence is not anchored to a stale value.
        """
        self._events.clear()
        self._last_ts = float("-inf")
