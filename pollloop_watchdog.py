"""Poll-loop liveness watchdog for uni-auto-trader.

Detects when MTXStrategy._poll_loop stops iterating — the process stays alive
(systemd sees it running; the heartbeat may even look fresh if the broker tick
thread keeps firing) but the loop is wedged, typically on a synchronous broker
SDK call with no timeout. The in-loop tick-stale watchdog cannot catch this:
it runs INSIDE the frozen loop. This watchdog runs in its OWN thread.

Pure, dependency-free, fully unit-tested. All time is passed in by the caller
(use time.monotonic()), so there is no hidden clock and tests never sleep.

No session gating: the poll loop completes an iteration every ~POLL_INTERVAL in
every session AND weekends/breaks (session checks + recon + margin + heartbeat
run regardless), so any gap past the threshold is a genuine freeze. Only an
uptime grace guards against a boot-time false kill. See the design spec
(2026-06-17-pollloop-liveness-watchdog-design.md), decision D1.

Side effects (os._exit, Telegram) live in strategy.py, not here.
"""
from __future__ import annotations

from typing import Callable, Optional


class PollLoopLivenessWatchdog:
    def __init__(
        self,
        *,
        freeze_threshold: float = 120.0,
        check_interval: float = 30.0,
        kill_grace: float = 180.0,
    ) -> None:
        if freeze_threshold <= 0 or check_interval <= 0 or kill_grace <= 0:
            raise ValueError("freeze_threshold, check_interval, kill_grace must be positive")
        self.freeze_threshold = float(freeze_threshold)
        self.check_interval = float(check_interval)
        self.kill_grace = float(kill_grace)
        self._last_complete_ts = 0.0     # 0.0 = no iteration completed yet
        self._last_check = 0.0
        self._kill_fired = False

    # ---- written from the poll thread. Plain float assignment is GIL-atomic. ----
    def record_poll_complete(self, now: float) -> None:
        self._last_complete_ts = now
        self._kill_fired = False         # loop alive again → re-arm for a future freeze

    def last_complete_age(self, now: float) -> Optional[float]:
        return (now - self._last_complete_ts) if self._last_complete_ts else None

    # ---- run from the dedicated watchdog thread (NOT the poll loop) ----
    def check(self, now: float, uptime: float, on_kill: Callable[[str], None]) -> None:
        # throttle the actual evaluation (belt-and-suspenders with the thread's own sleep)
        if now - self._last_check < self.check_interval:
            return
        self._last_check = now
        # anti boot-loop: never kill until the process has been up past the grace
        if uptime <= self.kill_grace:
            return
        # no iteration completed yet → nothing to compare against
        if self._last_complete_ts == 0.0:
            return
        age = now - self._last_complete_ts
        if age > self.freeze_threshold and not self._kill_fired:
            on_kill(
                f"POLL LOOP FROZEN — no iteration for {age:.0f}s "
                f"(threshold {self.freeze_threshold:.0f}s, uptime {uptime:.0f}s) "
                f"— escalating to process exit for systemd restart."
            )
            self._kill_fired = True
