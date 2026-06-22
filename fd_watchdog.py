"""File-descriptor leak watchdog for uni-auto-trader.

The broker SDK (unitrade) leaks one TCP socket per reconnect attempt (its
TCPClient._connect opens a new socket without closing the previous one). During
the weekend quote-maintenance disconnect storm open fds climb ~4/min until they
cross the Python select() FD_SETSIZE 1024 hard limit — at which point the SDK's
own select([], [sock], ...) raises "filedescriptor out of range in select()",
ftrade disconnects, and the trader goes blind-but-active (2026-06-22 incident,
open_fds=1026). The SDK is a compiled .so so the leak cannot be fixed in Python;
this watchdog restarts the process (os._exit -> systemd) before the wall is hit,
letting the OS reclaim every leaked fd. See
docs/superpowers/specs/2026-06-22-trader-fd-leak-watchdog-design.md.

Two tiers:
  soft - fd >= soft_threshold AND flat: a benign restart (no position at risk).
  hard - fd >= hard_threshold regardless of position: restart-anyway beats
         hitting the 1024 wall blind while holding a position.

Pure and dependency-free: fd count, uptime and flat-ness are passed in by the
caller, so there is no hidden clock or /proc access here and tests never touch
the real process. The hard tier is checked before the soft tier. Side effects
(os._exit, env-gated arming, Telegram) live in strategy.py, not here.
"""
from __future__ import annotations

from typing import Callable


class FdLeakWatchdog:
    def __init__(
        self,
        *,
        soft_threshold: int = 800,
        hard_threshold: int = 980,
        check_interval: float = 30.0,
        kill_grace: float = 180.0,
    ) -> None:
        if soft_threshold <= 0 or hard_threshold <= 0 or check_interval <= 0 or kill_grace <= 0:
            raise ValueError("thresholds, check_interval, kill_grace must be positive")
        if hard_threshold < soft_threshold:
            raise ValueError("hard_threshold must be >= soft_threshold")
        self.soft_threshold = int(soft_threshold)
        self.hard_threshold = int(hard_threshold)
        self.check_interval = float(check_interval)
        self.kill_grace = float(kill_grace)
        self._last_check = 0.0

    def check(
        self,
        now: float,
        *,
        fd_count: int,
        uptime: float,
        is_flat: bool,
        on_kill: Callable[[str, str], None],
    ) -> None:
        # throttle (belt-and-suspenders with the watchdog thread's own sleep)
        if now - self._last_check < self.check_interval:
            return
        self._last_check = now
        # anti boot-loop: never fire until the process is past the grace
        if uptime <= self.kill_grace:
            return
        # hard tier first: restart-anyway beats hitting the select() 1024 wall blind
        if fd_count >= self.hard_threshold:
            on_kill(
                f"FD LEAK hard tier — open_fds={fd_count} >= {self.hard_threshold} "
                f"(select() wall=1024, uptime {uptime:.0f}s) — restart regardless of position.",
                "hard",
            )
            return
        if fd_count >= self.soft_threshold and is_flat:
            on_kill(
                f"FD LEAK soft tier — open_fds={fd_count} >= {self.soft_threshold} and flat "
                f"(uptime {uptime:.0f}s) — restart to reclaim fds.",
                "soft",
            )
            return
