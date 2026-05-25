# Spec: Delay session-close P&L summary ~5 min so close-bell trades settle

**Date:** 2026-05-25
**Status:** Approved (design), proceeding to implementation plan
**Area:** `strategy.py` — `_check_session_change` / `_send_session_summary` timing
**Repo:** `asd261-ai/uni-auto-trader-v1`

## Context

The day/night session-close P&L summary (Telegram "日盤總結 / 夜盤總結") is sent by
`_send_session_summary(session)`, which `_check_session_change()` calls the **instant** the session
transitions day/night → break (~13:45 / ~05:00 TW), inside the 3-second poll loop. The summary tallies
`self._session_trades` (the per-session list of CLOSED trades, signal-based pnl appended in
`_close_unit`).

Problem: a position still closing **at the bell** — the Worker drives a `session_end` exit at ~13:45,
which the bot processes (and appends to `_session_trades`) over the next poll cycle(s) — can land
AFTER the summary has already fired. So a trade that closes right at close can be **missing from the
day's summary**. Sean asked to delay the tally/report to ~5 min after close so everything settles.

(Note: the summary is signal-based, not `pnl_calc` real-fill, so this is about capturing the bell's
`session_end` closes — not about real-fill Match lag. Settlement-day phantom handling
[[settlement-day-phantom-trade]] is a separate concern, out of scope.)

## Goal / Non-goals

- **Goal:** the session-close summary fires ~5 minutes after the close transition, so all bell/`session_end`
  closes are captured in `_session_trades` first. Applies to BOTH day (13:45→13:50) and night (05:00→05:05).
- **Non-goal:** changing what the summary reports (still signal-based `_session_trades`); adding
  `pnl_calc` real-fill to the summary; settlement-day (13:30) phantom handling; the heartbeat/DAILY_MAX_LOSS
  P&L (those are continuous, unaffected).

## Decision (from brainstorming)

Defer `_send_session_summary` by a fixed delay after the day/night→break transition, via a poll-loop
due-time check. Both sessions, delay = 5 min (`SESSION_SUMMARY_DELAY_SEC = 300`, a module constant).

## Design

### State + constant (strategy.py)
- `SESSION_SUMMARY_DELAY_SEC = 300` (module constant near the other timing constants).
- In `__init__`: `self._pending_summary_session: Optional[str] = None` and
  `self._pending_summary_due: float = 0.0`.

### Pure decision helper (testable, mirrors the `mtx_restore`/`tick_watchdog` pure-module pattern)
Extract the timing state-machine into a pure function so it is unit-testable without importing the
SDK-heavy `strategy.py`. New module `session_timing.py`:

```
def session_summary_action(prev_session, new_session, pending_session, due_at, now, delay):
    """Pure decision for deferred session-summary firing. Returns:
       {"fire": <session or None>,        # send the summary for this session NOW
        "pending_session": <session or None>,  # new pending state
        "due_at": <float>}                 # new due timestamp
    Rules:
      - On a day/night -> break transition: schedule (pending=prev, due=now+delay).
        If a summary was already pending (shouldn't happen; <delay between sessions), fire it first.
      - On a poll with no transition: if pending and now>=due -> fire it, clear pending.
    """
```
Behavior table (transition = `prev in (day,night) and new == break`):

| situation | fire | pending_session | due_at |
|---|---|---|---|
| transition, nothing pending | None | prev | now+delay |
| transition, something already pending | the old pending | prev | now+delay |
| no transition, pending & now>=due | pending | None | 0 |
| no transition, pending & now<due | None | (unchanged) | (unchanged) |
| no transition, nothing pending | None | None | 0 |

### Wiring (strategy.py)
- `_check_session_change`: compute `new = _get_session(now)`. Call `session_summary_action(self._current_session, new, self._pending_summary_session, self._pending_summary_due, now_ts, SESSION_SUMMARY_DELAY_SEC)`. If it returns `fire`, call `self._send_session_summary(fire)` then `self._session_trades = []`. Update `self._pending_summary_session`/`_pending_summary_due` from the result. Then set `self._current_session = new` (as today). **Remove the old immediate `_send_session_summary` + `_session_trades=[]` on transition** — that now happens only on fire.
- **CRITICAL:** the existing early-return `if session == self._current_session: return` (top of
  `_check_session_change`) MUST be removed/restructured. The deferred fire happens during the "break"
  period when there is NO transition (session unchanged) — if the method early-returns on
  "no change", the pending summary would never fire. Restructure so the helper is called on EVERY poll:
  call `session_summary_action` first (it returns `fire` for the due case even with no transition),
  act on its result, and only update `_current_session` when it actually changed.
- Because the poll loop calls `_check_session_change` every 3s, the "no transition, due" branch fires
  the deferred summary on the first poll at/after `due_at`. No separate poll-loop method needed — the
  pure helper handles both the transition and the poll cases.

### Data flow
13:45 poll: transition day→break → helper returns pending=day, due=13:50, fire=None → summary deferred,
`_session_trades` NOT cleared, `_current_session=break`. 13:45–13:50 polls: bell `session_end` closes
append to `_session_trades`. First poll ≥13:50: helper returns fire=day → summary sent (now complete),
`_session_trades` cleared, pending cleared.

## Error handling / edge cases

- **Restart in the 13:45–13:50 window:** `_pending_summary_session` and `_session_trades` are both
  in-memory → lost on restart → that session's Telegram summary is skipped. Acceptable (rare); the
  persistent trade log (`trades.jsonl`) is unaffected — only the Telegram recap is missed. Do NOT add
  persistence (the trades list is in-memory anyway, so persistence wouldn't reconstruct it).
- **`_send_session_summary` already returns early on empty `_session_trades`** → no empty summary.
- **A second transition while pending** (shouldn't happen, 5min ≪ inter-session gap): helper fires the
  stale pending first, then schedules the new one — no lost/overlapping summary.

## Testing (TDD)

`session_timing.py` is pure → `test_session_timing.py` (stdlib unittest, `python3 -m unittest`):
- transition with nothing pending → defers (pending=prev, due=now+delay, fire=None).
- poll before due → no fire, pending unchanged.
- poll at/after due → fire=pending, pending cleared.
- transition while already pending → fires the stale one, schedules new.
- no transition, nothing pending → no-op.
- both `prev="day"` and `prev="night"` schedule correctly.

Integration (the actual `_send_session_summary` Telegram send + `_session_trades` clearing) verified by
code review + a manual simulation (set due in the past, call `_check_session_change`, confirm one send
then cleared).

## Verification (end-to-end)

1. `python3 -m unittest test_session_timing -v` → green.
2. `python3 -m py_compile strategy.py session_timing.py` → OK.
3. Simulation: with `_current_session="day"` and a non-empty `_session_trades`, drive
   `_check_session_change` at a faked `now` just past a break transition → confirm summary is DEFERRED
   (not sent), then at faked `now ≥ due` → confirm it fires once and clears `_session_trades`.
4. Deploy observe-first (ask-first): after deploy, confirm the day summary Telegram arrives ~13:50 (not
   13:45) and includes any bell close.

## Files touched

- `session_timing.py` (new, pure) + `test_session_timing.py` (new)
- `strategy.py` (modify: constant, `__init__` state, `_check_session_change` rewrite to use the helper)
