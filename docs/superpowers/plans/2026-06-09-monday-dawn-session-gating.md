# Monday-dawn session-gating fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_get_session()` weekday-aware so Monday 00:00–05:00 is labeled `break` (not a false `night`), gating the tick-stale watchdog off during the weekend→Monday broker-maintenance dead-feed window.

**Architecture:** Single-function change in `strategy.py` (`_get_session`, lines 177–183) plus a focused test file. The fix is at the session-labeling source; `tick_watchdog.py` is untouched — its existing gate (`session not in ACTIVE_SESSIONS`) naturally suppresses the watchdog once the session reads `break`. `_get_session` is importable on system python3 (no broker SDK at module top-level), so it is tested directly.

**Tech Stack:** Python 3 stdlib `unittest` (no deps), run with `python3 -m unittest`.

**Spec:** `docs/superpowers/specs/2026-06-09-monday-dawn-session-gating-design.md`

---

## File Structure

- **Modify:** `strategy.py` — `_get_session()` only (lines 177–183). No other function changes.
- **Create:** `test_session_gating.py` — unit tests for `_get_session` (weekday × time matrix) + an integration test that the watchdog stays silent for a dead feed at Monday dawn, plus a control that a real night-session outage still fires.

### Intentionally NOT changed (out of scope — do not "helpfully" edit)

`strategy.py:361–366` contains a parallel naive night-window computation
(`if t >= dtime(15,0) or t < dtime(5,0): base = ...`) used to derive the MTX
**restore cutoff** at startup. It is left as-is because: it runs only at boot, it
governs which already-open positions to restore, and on a Monday there are no
Sunday-night trades to restore — so its Monday-dawn mislabel is benign. Changing it
is scope creep beyond this spec.

### Weekday anchors used by the tests

The week of 2026-06-08: `Mon=8 (wd0)`, `Tue=9 (wd1)`, `Wed=10 (wd2)`, `Thu=11 (wd3)`,
`Fri=12 (wd4)`, `Sat=13 (wd5)`, `Sun=14 (wd6)`. `_get_session` only reads `dt.time()`
and `dt.weekday()`, so naive `datetime` objects are sufficient.

---

## Task 1: Write the failing tests for weekday-aware session labeling + Monday-dawn gating

**Files:**
- Create: `test_session_gating.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for weekday-aware _get_session + the Monday-dawn watchdog gating fix.
Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_session_gating -v

Weekday anchors (week of 2026-06-08):
  08 Mon(0) 09 Tue(1) 10 Wed(2) 11 Thu(3) 12 Fri(4) 13 Sat(5) 14 Sun(6)
_get_session only reads dt.time() and dt.weekday() -> naive datetimes are fine.
"""
import unittest
from datetime import datetime

from strategy import _get_session
from tick_watchdog import TickStaleWatchdog

MON, TUE, WED, THU, FRI, SAT, SUN = 8, 9, 10, 11, 12, 13, 14


def dt(day, hh, mm=0):
    return datetime(2026, 6, day, hh, mm)


class GetSessionTest(unittest.TestCase):
    def test_day_session_mon_to_fri(self):
        for day in (MON, TUE, WED, THU, FRI):
            self.assertEqual(_get_session(dt(day, 10, 0)), "day", day)

    def test_day_window_is_break_on_weekend(self):
        for day in (SAT, SUN):
            self.assertEqual(_get_session(dt(day, 10, 0)), "break", day)

    def test_night_evening_leg_mon_to_fri(self):
        for day in (MON, TUE, WED, THU, FRI):
            self.assertEqual(_get_session(dt(day, 20, 0)), "night", day)

    def test_night_evening_leg_break_on_weekend(self):
        for day in (SAT, SUN):
            self.assertEqual(_get_session(dt(day, 20, 0)), "break", day)

    def test_night_tail_leg_tue_to_sat(self):
        for day in (TUE, WED, THU, FRI, SAT):
            self.assertEqual(_get_session(dt(day, 2, 0)), "night", day)

    def test_monday_dawn_is_break(self):
        # THE FIX: Monday 00:00-05:00 is not a real night session.
        self.assertEqual(_get_session(dt(MON, 0, 0)), "break")
        self.assertEqual(_get_session(dt(MON, 3, 0)), "break")
        self.assertEqual(_get_session(dt(MON, 4, 59)), "break")

    def test_sunday_dawn_is_break(self):
        self.assertEqual(_get_session(dt(SUN, 2, 0)), "break")

    def test_boundaries_monday(self):
        self.assertEqual(_get_session(dt(MON, 5, 0)), "break")
        self.assertEqual(_get_session(dt(MON, 8, 44)), "break")
        self.assertEqual(_get_session(dt(MON, 8, 45)), "day")
        self.assertEqual(_get_session(dt(MON, 13, 44)), "day")
        self.assertEqual(_get_session(dt(MON, 13, 45)), "break")
        self.assertEqual(_get_session(dt(MON, 14, 59)), "break")
        self.assertEqual(_get_session(dt(MON, 15, 0)), "night")

    def test_boundaries_tuesday_tail(self):
        self.assertEqual(_get_session(dt(TUE, 0, 0)), "night")
        self.assertEqual(_get_session(dt(TUE, 4, 59)), "night")
        self.assertEqual(_get_session(dt(TUE, 5, 0)), "break")


class WatchdogMondayDawnGatingTest(unittest.TestCase):
    def test_dead_feed_monday_dawn_no_alert_no_kill(self):
        # Reproduces 6/8 00:00 would-fire: feed dead ~9h, long uptime, not weekend (Mon).
        # With the fix, session=break -> watchdog gate suppresses both alert and kill.
        wd = TickStaleWatchdog(day_threshold=90.0, night_threshold=300.0,
                               check_interval=30.0)
        msgs, kills = [], []
        wd.record_tick(0.0)
        session = _get_session(dt(MON, 0, 0))
        self.assertEqual(session, "break")          # precondition: fix in effect
        wd.check(32419.0, session, False,           # is_weekend=False (Monday)
                 msgs.append, uptime=57651.0, on_kill=kills.append)
        self.assertEqual(msgs, [])                  # no stale alert
        self.assertEqual(kills, [])                 # no kill escalation

    def test_dead_feed_tuesday_night_still_fires(self):
        # Control: Tuesday 02:00 IS a real night session -> watchdog still escalates.
        # Two checks: first enters the session (grace anchor), then a stale gap later.
        wd = TickStaleWatchdog(day_threshold=90.0, night_threshold=300.0,
                               check_interval=30.0)
        msgs, kills = [], []
        session = _get_session(dt(TUE, 2, 0))
        self.assertEqual(session, "night")
        wd.record_tick(100.0)
        wd.check(100.0, session, False, msgs.append,
                 uptime=57651.0, on_kill=kills.append)   # enter session, age=0, no fire
        wd.check(7300.0, session, False, msgs.append,
                 uptime=57651.0, on_kill=kills.append)   # 7200s stale > kill 600s -> fire
        self.assertNotEqual(msgs, [])
        self.assertNotEqual(kills, [])
```

- [ ] **Step 2: Run the tests to confirm they fail on the right cases**

Run: `python3 -m unittest test_session_gating -v`
Expected: FAIL. The Monday-dawn / weekend cases fail because the current
`_get_session` returns `"night"` for `Mon 00:00` and `"day"`/`"night"` on weekends
(e.g. `test_monday_dawn_is_break`, `test_day_window_is_break_on_weekend`,
`test_night_evening_leg_break_on_weekend`, `test_boundaries_monday`,
`test_dead_feed_monday_dawn_no_alert_no_kill`). The Mon–Fri cases and the Tuesday
control already pass.

- [ ] **Step 3: Commit the failing tests**

```bash
git add test_session_gating.py
git commit -m "test: weekday-aware _get_session + Monday-dawn watchdog gating (red)"
```

---

## Task 2: Implement weekday-aware `_get_session`

**Files:**
- Modify: `strategy.py:177-183`

- [ ] **Step 1: Replace `_get_session`**

Find (lines 177–183):

```python
def _get_session(dt: datetime) -> str:
    t = dt.time()
    if dtime(8, 45) <= t < dtime(13, 45):
        return "day"
    if t >= dtime(15, 0) or t < dtime(5, 0):
        return "night"
    return "break"
```

Replace with:

```python
def _get_session(dt: datetime) -> str:
    # Weekday-aware (2026-06-09): the night session runs 15:00 day D -> 05:00 day D+1
    # for trading days D in Mon-Fri, so the early-morning leg (t < 05:00) is only a
    # real session on Tue-Sat. Without this, Monday 00:00-05:00 was mislabeled "night"
    # while the broker feed is legitimately dead (maintenance to ~07:20), which tripped
    # the tick watchdog (would-fire kill observed 2026-06-08 00:00). See
    # docs/superpowers/specs/2026-06-09-monday-dawn-session-gating-design.md
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

- [ ] **Step 2: Run the gating tests to confirm green**

Run: `python3 -m unittest test_session_gating -v`
Expected: PASS (all tests in `GetSessionTest` and `WatchdogMondayDawnGatingTest`).

- [ ] **Step 3: Byte-compile sanity**

Run: `python3 -m py_compile strategy.py`
Expected: no output, exit 0.

- [ ] **Step 4: Commit the implementation**

```bash
git add strategy.py
git commit -m "fix: weekday-aware _get_session — Monday 00:00-05:00 is break not night

Stops the tick-stale watchdog false-firing during the weekend->Monday
broker-maintenance dead feed. Evidence: 6/8 00:00 [tick-wd KILL would-fire].
Spec: docs/superpowers/specs/2026-06-09-monday-dawn-session-gating-design.md"
```

---

## Task 3: Full-suite regression

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python3 -m unittest discover -p 'test_*.py' -v`
Expected: PASS — all existing tests (`test_tick_watchdog`, `test_entry_guard`,
`test_session_timing`, `test_atr_gate`, `test_open_freeze`, `test_mtx_restore`,
`test_pnl_calc_cache`, `test_real_fill_pnl`, `test_reconcile_real_fill`,
`test_exit_reason`, `test_fill_schema`, `test_margin_headroom`, `test_order`,
`test_order_reject`) plus the new `test_session_gating`, all green. No test should
change behavior — the only consumers of `_get_session` act during Mon–Fri sessions,
which are unchanged.

- [ ] **Step 2: If everything is green, the implementation is complete.**

Deploy (scp `strategy.py` to the VPS + `trader-precheck.sh && systemctl restart`,
verify sha256) is a **separate ask-first step** — NOT part of this plan. After deploy,
observe one Monday dawn clean before re-attempting the Phase-2 Telegram flip and the
kill-tier arm (each still ask-first).

---

## Self-Review

- **Spec coverage:** weekday-aware `_get_session` (Task 2) ✓; behavior table cells all
  asserted (Task 1 `GetSessionTest`) ✓; Monday-dawn watchdog-gating integration
  (Task 1 `WatchdogMondayDawnGatingTest`) ✓; `tick_watchdog.py` untouched ✓; zero Mon–Fri
  regression (Task 3 full suite) ✓; deploy/Phase-2/kill-tier explicitly out of scope ✓.
- **Placeholder scan:** none — all test and implementation code is concrete.
- **Type/name consistency:** `_get_session(dt)` returns `"day"|"night"|"break"`;
  `TickStaleWatchdog.check(now, session, is_weekend, notify, uptime=, on_kill=)` matches
  the call site in `strategy.py` and the existing `test_tick_watchdog` usage.
