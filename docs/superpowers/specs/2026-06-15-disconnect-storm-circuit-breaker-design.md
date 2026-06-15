# Disconnect-Storm Circuit Breaker — Design

**Date:** 2026-06-15
**Status:** Approved (brainstorm) → pending implementation plan
**Author:** Bob (with Sean)
**Memory:** `project-killtier-monday-dawn-storm`

## Problem

On 2026-06-15 the live trader SEGV-crashed (core-dump, `status=11/SEGV`) during the
day session. Root-cause investigation (VPS journal + kernel dmesg) found:

- **Trigger:** broker trade connection dropped at 08:55:48, then a sustained reconnect
  storm — **142 `dtrade disconnected`/`reconnected` events in 12 minutes** (08:45–08:57),
  each re-entering the SDK's native event dispatch.
- **Failure mode:** **CPython C-stack overflow from unbounded recursion.** 4 historical
  SEGVs all at the **identical instruction pointer `ip 0x551368`** (python3.11 binary),
  fault address pinned to the stack guard page, `sp` page-aligned — the textbook signature
  of stack exhaustion. The broker SDK's native callback dispatch nests synchronously during
  the storm until the C stack blows.
- **NOT fd-leak.** The previously-documented failure mode (`project-trader-fd-leak`,
  Errno24/fd exhaustion) is excluded: the live process held only 6 fds, `LimitNOFILE=65536`
  is intact, and there was zero `Errno 24` / `CLOSE_WAIT` buildup before the crash.
- **Recurrence:** ~every 25h of uptime when a long disconnect storm hits.

Our Python-level handlers (`trader._on_disconnected`, `strategy.on_disconnect`) are bounded
and not the recursion source — the recursion lives in the SDK native layer, which we cannot
patch. But our `on_disconnect` Python frame **does execute on every disconnect event** (its
log line appears for each), giving us a place to detect the storm and bail out cleanly
*before* the native dispatch recurses into stack overflow.

### Relationship to the existing tick-stale kill-tier

The existing `TICK_STALE_KILL` watchdog (`tick_watchdog.py` + `strategy._tick_wd_kill`)
fires on a *stale price feed*. On 2026-06-15 it logged a would-fire at 08:48 — 8 minutes
*before* the 08:56 SEGV — so an armed kill-tier would likely have prevented this crash.
But kill-tier only covers the "feed went silent" path. A disconnect storm that does **not**
begin with feed-stale would slip past it. This breaker is a **broader, independent** net
for the disconnect-storm path specifically.

## Goals

- Detect a broker disconnect storm during an active trading session and cleanly
  `os._exit(1)` (handing restart to systemd) **before** the SDK recursion blows the C stack.
- Ship **observe-first**: log would-fire without exiting until validated on real logs,
  then arm via env (ask-first), mirroring the kill-tier and demote rollout discipline.
- **Never** restart-loop during weekend / broker-maintenance windows, where disconnects
  are expected (the Monday-dawn-storm lesson — `project-killtier-monday-dawn-storm`).

## Non-goals

- Fixing the SDK's native recursion itself (vendor code, not patchable here).
- Replacing or merging with the tick-stale kill-tier — the two stay independent.
- Counter-reset / flap-recovery heuristics beyond the plain sliding window (YAGNI).

## Design

### Approach (chosen)

A new **pure, unit-testable** `DisconnectStormWatchdog` class in a new module
`disconnect_watchdog.py` (mirroring `tick_watchdog.py`). Detection runs **inline in the
disconnect callback path** (`strategy.on_disconnect`), so the check executes on every
disconnect even if a tight storm starves the 3s poll thread of CPU. All side effects
(`os._exit`, env reads, Telegram notify) stay in `strategy.py`; the class is side-effect-free.

Rejected alternatives: (B) poll-loop check like tick-wd — cleaner symmetry but risks lag if
the storm starves the poll thread; (C) hybrid callback+poll — most robust but more code than
the threat warrants.

### Component — `disconnect_watchdog.py`

```python
from collections import deque

class DisconnectStormWatchdog:
    """Sliding-window disconnect-storm detector. Pure: no I/O, no os._exit.

    record_and_check(now, active=...) records a disconnect at `now` and returns
    True iff the count within the trailing `window_sec` reaches `max_disconnects`
    AND the session is active. When inactive (weekend / broker maintenance / break),
    it clears the window and returns False — market-closed disconnects are expected
    and must not trip a restart (Monday-dawn-storm lesson)."""

    def __init__(self, *, window_sec: float = 120.0, max_disconnects: int = 20):
        self._window = window_sec
        self._max = max_disconnects
        self._events: deque[float] = deque()

    def record_and_check(self, now: float, *, active: bool) -> bool:
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
```

### Config (env, beside `TICK_STALE_KILL` at strategy.py ~179)

```python
DISCONNECT_STORM_KILL       = os.getenv("DISCONNECT_STORM_KILL", "off").lower() == "on"
DISCONNECT_STORM_WINDOW_SEC = int(os.getenv("DISCONNECT_STORM_WINDOW_SEC", "120"))
DISCONNECT_STORM_MAX        = int(os.getenv("DISCONNECT_STORM_MAX", "20"))
```

Defaults: **observe** (`off`), 20 disconnects / 120s. Threshold grounded in the 2026-06-15
storm (~12 disconnects/min, SEGV at minute 8): 20/120s fires ~2 min in, leaving ~6 min of
margin before the historical crash point, while a 1–2-event open-of-session reconnect blip
stays well under threshold.

### Data flow (wiring)

1. **`strategy.__init__`** (~line 318, beside `self._tick_wd`):
   ```python
   self._disc_wd = DisconnectStormWatchdog(
       window_sec=DISCONNECT_STORM_WINDOW_SEC,
       max_disconnects=DISCONNECT_STORM_MAX,
   )
   ```
2. **`strategy.on_disconnect()`** — at the very top, before the existing flatten/notify:
   ```python
   active = _get_session(datetime.now(TZ_TW)) != "break"
   if self._disc_wd.record_and_check(time.time(), active=active):
       self._disconnect_storm_kill(
           f"{DISCONNECT_STORM_MAX} disconnects in {DISCONNECT_STORM_WINDOW_SEC}s (active session)")
   ```
   `_get_session` is already weekday-aware (2026-06-09 fix): it returns `"break"` for
   weekends, the Mon 00:00–05:00 maintenance leg, and all non-session windows, so
   `!= "break"` is the correct "active" predicate and inherits the Monday-dawn fix.
3. **`strategy._disconnect_storm_kill(msg)`** — mirrors `_tick_wd_kill` (observe/arm):
   ```python
   def _disconnect_storm_kill(self, msg):
       if not DISCONNECT_STORM_KILL:
           logger.error(f"[disc-storm KILL would-fire] {msg}")   # Phase A: observe
           return
       logger.error(f"[disc-storm KILL] {msg}")
       try:
           self._safe_health_notify(f"🔪 Trader self-restart (disconnect storm): {msg}")
       except Exception:
           pass
       import os as _os
       _os._exit(1)                                              # Phase B: armed
   ```

`trader.py` is unchanged — disconnect events already flow through `strategy.on_disconnect()`.

### Error handling / edge cases

- **`os._exit` safety mid-storm:** the breaker bails at the 20th disconnect (~100s into a
  storm), far before the ~142-event / 8-minute SEGV point, so the C stack is nowhere near
  exhausted; `os._exit` is a direct syscall and will succeed.
- **Session-boundary false positives:** a handful of reconnects at the open is far below
  20/120s and will not trip.
- **Maintenance restart-loop:** `active=False` clears the window, so weekend/maintenance
  disconnects never accumulate and never kill.
- **Independence from kill-tier:** separate env, separate observe/arm. If both are armed and
  both trip, whichever `os._exit`s first is fine.
- **Observe-first rollout:** ships with `DISCONNECT_STORM_KILL` unset (observe). Validate via
  `[disc-storm KILL would-fire]` log lines on real storms, confirm no false trips, then
  arm via env — ask-first, mirroring tick-wd and the ② demote rollout.

### Testing — `test_disconnect_watchdog.py` (stdlib unittest, mirrors `test_tick_watchdog.py`)

1. Below `max` within the window → `False`.
2. Exactly `max` within the window → `True` (boundary 20/120s).
3. Events spaced wider than the window (>120s apart) never trip — old events age out.
4. `active=False` → clears and returns `False` (weekend/maintenance exemption).
5. `active` transition `False → True` starts the count fresh.
6. The class contains **no** `os._exit` — purity guard (all side effects in strategy).

The pure class is fully covered by unit tests; the strategy-side observe/arm gating follows
the existing tick-wd test approach.

## Rollout

1. Land code on `feat/disconnect-storm-circuit-breaker`, observe-default, all tests green.
2. Review (spec + quality), merge to `main` (trunk == VPS authority).
3. Deploy to VPS (scp + sha256 verify + precheck && restart) — **ask-first**.
4. Observe `[disc-storm KILL would-fire]` on a real storm; confirm no false trips during
   session boundaries / maintenance.
5. Arm via `DISCONNECT_STORM_KILL=on` in VPS `.env` (Sean edits env) + precheck && restart —
   **ask-first**.
