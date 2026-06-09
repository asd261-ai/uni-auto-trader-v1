# Monday-dawn session-gating fix — design

**Date:** 2026-06-09
**Status:** Approved (Sean), ready for implementation plan
**Author:** Bob (Claude) + Sean

## Problem

The tick-stale watchdog (`tick_watchdog.py`) is gated to fire only during an
active session and not on weekends:

```python
if session not in ACTIVE_SESSIONS or is_weekend:   # tick_watchdog.py:95
    return
```
where `ACTIVE_SESSIONS = ("day", "night")` and `is_weekend = datetime.now(TZ_TW).weekday() >= 5`.

On **Monday 00:00–05:00** this gate fails to suppress the watchdog:

- `is_weekend` is `weekday() >= 5`, so Monday (weekday 0) is **not** treated as weekend.
- `_get_session()` (`strategy.py:177`) labels any time `< 05:00` as `"night"` purely
  by clock, ignoring the day of week. So Monday 00:00–05:00 is reported as an active
  `night` session.

But Monday 00:00–05:00 is **not** a real night session — the TW futures night session
runs 15:00 day D → 05:00 day D+1 only for trading days (Mon–Fri); the leg ending Monday
05:00 would have had to start Sunday 15:00, and there is no Sunday session. During this
window the broker quote feed is legitimately dead (broker quote maintenance runs until
**Monday 07:20**, see `reference_broker_maintenance_hours`).

### Evidence (VPS, observe mode, 2026-06-08 = Monday)

```
Jun 08 00:00:19 [tick-wd KILL would-fire] TICK FEED STALE 32419s > kill 600s
(session=night, uptime=57651s) — escalating to process exit for systemd restart
```

The kill tier *would have fired* at Monday 00:00. With `TICK_STALE_KILL=on` armed, this
becomes a real `os._exit(1)` → systemd restart → feed still dead (maintenance) →
re-kill after grace+threshold (~10–13 min) → **restart storm Monday 00:00–07:20**
(~30–40 restarts). This is why arming kill-tier was deferred on 2026-06-09 (only the
FVG past-target guard shipped that day).

The Phase-2 Telegram alert flip shares the same root cause: it would send a false
"9h feed stale" alert at Monday 00:00 (bounded to one message by the watchdog's
one-shot latch, but still a false alarm).

## Goal / success criteria

1. `_get_session()` returns `"break"` for Monday 00:00–05:00 (and other no-session
   windows), so the watchdog is naturally gated off there (`break ∉ ACTIVE_SESSIONS`).
2. **Zero regression** to Mon–Fri normal trading hours (day 08:45–13:45, night
   15:00–05:00 next day).
3. An integration check confirms the watchdog does **not** fire (alert or kill) for a
   dead feed at Monday 00:00.
4. `tick_watchdog.py` is **not** modified — the fix is entirely at the session-labeling
   source.

This unblocks (separately, each still ask-first) the Phase-2 Telegram flip and the
kill-tier arm.

## Design

Make `_get_session()` weekday-aware. The night session is "15:00 day D → 05:00 day D+1"
for D ∈ Mon–Fri, so:

- evening leg (`t >= 15:00`): valid on Mon–Fri (weekday 0–4).
- early-morning leg (`t < 05:00`): tail of *yesterday's* night, valid on Tue–Sat
  (weekday 1–5).
- day session (`08:45 ≤ t < 13:45`): valid on Mon–Fri (weekday 0–4).

```python
def _get_session(dt: datetime) -> str:
    t = dt.time()
    wd = dt.weekday()                                   # Mon=0 .. Sun=6
    if dtime(8, 45) <= t < dtime(13, 45) and wd <= 4:   # day: Mon-Fri 08:45-13:45
        return "day"
    if t >= dtime(15, 0) and wd <= 4:                   # night start leg: Mon-Fri 15:00+
        return "night"
    if t < dtime(5, 0) and 1 <= wd <= 5:                # night tail leg: Tue-Sat 00:00-05:00
        return "night"
    return "break"
```

### Behavior change table

| Window | weekday | Before | After | Notes |
|---|---|---|---|---|
| Mon–Fri 08:45–13:45 | 0–4 | day | day | unchanged |
| Mon–Fri 15:00–24:00 | 0–4 | night | night | unchanged |
| Tue–Sat 00:00–05:00 | 1–5 | night | night | unchanged (Sat already weekend-gated) |
| **Mon 00:00–05:00** | 0 | night | **break** | **the fix** — un-gated path |
| Sun 00:00–05:00 | 6 | night | break | weekend (watchdog was already gated) |
| Sat/Sun 08:45–13:45 | 5,6 | day | break | weekend (watchdog was already gated) |
| Sat/Sun 15:00–24:00 | 5,6 | night | break | weekend (watchdog was already gated) |

The only behavior change reaching a non-weekend-gated path is **Monday 00:00–05:00 →
break**. The weekend rows were already suppressed in the watchdog by `is_weekend`; for
other `_get_session` consumers they are effectively no-ops because no signals arrive on
weekends — and returning `break` there is strictly safer (entry logic explicitly idle).

## Blast radius — consumers of `_get_session` / `_current_session`

All must be confirmed regression-free by the test suite:

- `strategy.py:410` — boot session log line.
- `strategy.py:699 _check_session_change` — session transitions, open-notify, close summaries.
- `strategy.py:1008, :1113` — entry gating (`if session not in ("day","night")`).
- `strategy.py:1409` — `should_skip_code4_atr(..., session)`.
- watchdog via `self._current_session` (the path this fix targets).

No consumer should behave differently during Mon–Fri active sessions; the changed cells
are all weekend / Monday-pre-dawn windows where the trader should be idle anyway.

## Testing

1. **Unit — `_get_session` parametrized:** one assertion per (weekday × representative
   time) cell in the table above, plus the boundaries 05:00, 08:45, 13:45, 15:00, and a
   dedicated Monday-dawn case (`Mon 00:00 → break`, `Mon 04:59 → break`, `Mon 05:00 →
   break`, `Mon 08:45 → day`).
2. **Integration — watchdog gating:** simulate a dead feed at Monday 00:00 with
   `session=_get_session(Mon 00:00)` → `TickStaleWatchdog.check()` fires **neither** the
   notify callback **nor** `on_kill`. Mirror the existing `test_tick_watchdog` style.
3. **Regression:** run the full existing suite (`test_tick_watchdog`, `test_entry_guard`,
   and any others) — all green.

## Out of scope

- No change to `tick_watchdog.py`, env vars, or deploy flow.
- The Phase-2 Telegram flip and kill-tier arm are **separate** follow-ups, each ask-first
  after this fix is deployed and a Monday dawn is observed clean.
- Deploy of this fix (scp + restart) is a separate ask-first step, not part of this spec.
