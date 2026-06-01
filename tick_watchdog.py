"""
Tick-stale watchdog for uni-auto-trader.

Detects when the broker `dquote` tick feed silently stops delivering ticks — the trader
stays alive (heartbeat + recon look green) but goes blind to price, so exit checks never
fire. See docs/tick-stale-watchdog-design.md for the full rationale.

Pure, dependency-free, fully unit-tested. **NOT yet wired into strategy.py.** All time is
passed in by the caller so there's no hidden clock and tests never sleep.

Integration (3 hooks in strategy.py, when approved):
  * on_tick — at the TOP, BEFORE `if not all_units: return` (else flat-book ticks don't
    refresh the stamp and you get false alerts when flat):
        self._tick_wd.record_tick(time.time())
  * _poll_loop — beside the recon call:
        self._tick_wd.check(time.time(), self._current_session,
                            datetime.now(TZ_TW).weekday() >= 5,
                            self._safe_health_notify)
  * heartbeat dict (optional):
        "last_tick_age_sec": self._tick_wd.last_tick_age(time.time())

v1 is ALERT-ONLY. No auto-resubscribe (dquote has no resubscribe path and mid-session
re-subscription is unverified SDK behavior on a live account).
"""
from __future__ import annotations

from typing import Callable, Optional

ACTIVE_SESSIONS = ("day", "night")


class TickStaleWatchdog:
    def __init__(
        self,
        *,
        day_threshold: float = 90.0,     # liquid day session (08:45-13:45)
        night_threshold: float = 300.0,  # thin night session (15:00-05:00); allow longer gaps
        check_interval: float = 30.0,    # how often the staleness eval actually runs
        kill_day_threshold: float = 180.0,    # escalate (os._exit) past this in day session
        kill_night_threshold: float = 600.0,  # escalate past this in night session
        kill_grace: float = 180.0,            # min process uptime before kill is eligible
    ) -> None:
        if day_threshold <= 0 or night_threshold <= 0 or check_interval <= 0:
            raise ValueError("thresholds and interval must be positive")
        if kill_day_threshold <= 0 or kill_night_threshold <= 0 or kill_grace <= 0:
            raise ValueError("kill thresholds and grace must be positive")
        self.day_threshold = float(day_threshold)
        self.night_threshold = float(night_threshold)
        self.check_interval = float(check_interval)
        self.kill_day_threshold = float(kill_day_threshold)
        self.kill_night_threshold = float(kill_night_threshold)
        self.kill_grace = float(kill_grace)
        self._last_tick_ts = 0.0          # 0.0 = no tick seen yet
        self._active_session_since = 0.0  # when we last entered an active session (grace anchor)
        self._session: Optional[str] = None
        self._last_check = 0.0
        self._alert_sent = False          # one-shot latch, mirrors recon's _recon_alert_sent
        self._kill_fired = False

    # ---- written from on_tick (broker thread). Plain float assignment is GIL-atomic. ----
    def record_tick(self, now: float) -> None:
        self._last_tick_ts = now
        self._kill_fired = False  # feed alive again → re-arm kill for future outages

    def last_tick_age(self, now: float) -> Optional[float]:
        return (now - self._last_tick_ts) if self._last_tick_ts else None

    @property
    def alerting(self) -> bool:
        return self._alert_sent

    # ---- run every poll (poll thread) ----
    def check(
        self,
        now: float,
        session: str,
        is_weekend: bool,
        notify: Callable[[str], None],
        uptime: Optional[float] = None,
        on_kill: Optional[Callable[[str], None]] = None,
    ) -> None:
        # (1) session-transition grace — runs on every call, before the throttle, so we never
        #     miss a transition. Entering an active session (re)anchors the grace clock so the
        #     first ticks of the session have time to arrive without false-alarming.
        if session in ACTIVE_SESSIONS and self._session not in ACTIVE_SESSIONS:
            self._active_session_since = now
        self._session = session

        # (2) throttle the actual staleness evaluation
        if now - self._last_check < self.check_interval:
            return
        self._last_check = now

        # (3) gate: only during an active session, never on weekends (mirrors recon's gate)
        if session not in ACTIVE_SESSIONS or is_weekend:
            return

        threshold = self.day_threshold if session == "day" else self.night_threshold
        ref = max(self._last_tick_ts, self._active_session_since)
        age = now - ref

        if age > threshold:
            if not self._alert_sent:
                notify(
                    f"⚠️ TICK FEED STALE — no dquote tick for {age:.0f}s "
                    f"(session={session}, threshold={threshold:.0f}s). "
                    f"Trader alive but blind to price; exits won't fire. Check feed / restart."
                )
                self._alert_sent = True
        else:
            if self._alert_sent:
                notify(f"✅ Tick feed recovered (last tick {age:.0f}s ago).")
                self._alert_sent = False
            # _kill_fired is re-armed by record_tick() when the feed delivers a new tick,
            # not here — resetting on any healthy eval would allow the kill to double-fire
            # within the same outage episode (alert-tier age < threshold while kill-tier
            # kill_age > kill_threshold in a transitional window).

        # kill-tier: escalate a sustained outage to a process exit so systemd restarts
        # and the OS reclaims leaked fds. Gated by the same active-session check above,
        # plus a longer threshold and a process-uptime grace (anti self-kill-loop).
        # Uses the SAME session-grace-anchored `age` as the alert tier: once a session has
        # been delivering ticks, _last_tick_ts > _active_session_since so age == real
        # staleness; the anchor only diverges at session open, where it (correctly) prevents
        # a false kill from a prior-session stale tick before the first tick arrives.
        if on_kill is not None and uptime is not None and uptime > self.kill_grace:
            kill_threshold = self.kill_day_threshold if session == "day" else self.kill_night_threshold
            if age > kill_threshold and not self._kill_fired:
                on_kill(
                    f"TICK FEED STALE {age:.0f}s > kill {kill_threshold:.0f}s "
                    f"(session={session}, uptime={uptime:.0f}s) — escalating to process exit "
                    f"for systemd restart (fd reclaim)."
                )
                self._kill_fired = True
