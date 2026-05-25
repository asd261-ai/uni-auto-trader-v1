# Session-Close P&L Summary Delay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fire the day/night session-close P&L summary ~5 min after the close transition (not at the instant) so bell/`session_end` closes are captured first.

**Architecture:** A pure, dependency-free `session_timing.py` holds the defer/fire decision (unit-testable like `tick_watchdog.py`/`mtx_restore.py`). `strategy.py`'s `_check_session_change` (runs every 3s poll) calls it: on day/night→break it schedules the summary for `now+300`, and on later polls it fires the deferred summary once due — then clears `_session_trades`.

**Tech Stack:** Python 3 stdlib only (`unittest`). Repo `asd261-ai/uni-auto-trader-v1`. Test: `python3 -m unittest test_<name> -v`.

**Spec:** `docs/superpowers/specs/2026-05-25-session-close-pnl-timing-design.md`

**Branch:** `session-summary-delay` (already created off main; spec committed here).

**Deploy:** observe-first, ask-first — scp `strategy.py` + `session_timing.py` + sha256 + restart on a weekday; only changes when the summary fires.

---

## File Structure

- `session_timing.py` (NEW) — pure `session_summary_action(...)`. No SDK/strategy imports.
- `test_session_timing.py` (NEW) — unittest for it (8 cases, all branches).
- `strategy.py` (MODIFY) — `SESSION_SUMMARY_DELAY_SEC` constant; import; `__init__` pending state; rewrite `_check_session_change` to call the helper (removes the early-return).

---

## Task 1: `session_timing.py` — pure defer/fire decision

**Files:**
- Create: `session_timing.py`
- Test: `test_session_timing.py`

- [ ] **Step 1: Write the failing tests**

Create `test_session_timing.py`:
```python
"""Tests for session_timing. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_session_timing -v
"""
import unittest

import session_timing as st

DELAY = 300.0


class SessionSummaryActionTest(unittest.TestCase):
    def test_day_to_break_defers(self):
        a = st.session_summary_action("day", "break", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertEqual(a["pending_session"], "day")
        self.assertEqual(a["due_at"], 1300.0)

    def test_night_to_break_defers(self):
        a = st.session_summary_action("night", "break", None, 0.0, 2000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertEqual(a["pending_session"], "night")
        self.assertEqual(a["due_at"], 2300.0)

    def test_poll_before_due_no_fire(self):
        a = st.session_summary_action("break", "break", "day", 1300.0, 1200.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertEqual(a["pending_session"], "day")
        self.assertEqual(a["due_at"], 1300.0)

    def test_poll_at_due_fires(self):
        a = st.session_summary_action("break", "break", "day", 1300.0, 1300.0, DELAY)
        self.assertEqual(a["fire"], "day")
        self.assertIsNone(a["pending_session"])

    def test_poll_after_due_fires(self):
        a = st.session_summary_action("break", "break", "day", 1300.0, 9999.0, DELAY)
        self.assertEqual(a["fire"], "day")
        self.assertIsNone(a["pending_session"])

    def test_no_transition_nothing_pending_noop(self):
        a = st.session_summary_action("day", "day", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertIsNone(a["pending_session"])

    def test_transition_with_stale_pending_fires_it_first(self):
        a = st.session_summary_action("night", "break", "day", 500.0, 2000.0, DELAY)
        self.assertEqual(a["fire"], "day")
        self.assertEqual(a["pending_session"], "night")
        self.assertEqual(a["due_at"], 2300.0)

    def test_break_to_day_no_summary(self):
        a = st.session_summary_action("break", "day", None, 0.0, 1000.0, DELAY)
        self.assertIsNone(a["fire"])
        self.assertIsNone(a["pending_session"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_session_timing -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'session_timing'`.

- [ ] **Step 3: Write minimal implementation**

Create `session_timing.py`:
```python
"""Pure timing decision for the deferred session-close summary.

No SDK/strategy imports -> unit-testable on system python3 (python3 -m unittest
test_session_timing). The session summary is delayed ~5 min after the day/night->break
transition so bell/session_end closes are captured before the tally is sent.
"""


def session_summary_action(prev_session, new_session, pending_session, due_at, now, delay):
    """Decide deferred session-summary firing. Returns dict:
       fire            : session str to send the summary for NOW, or None
       pending_session : new pending state (session str or None)
       due_at          : new due timestamp (float)

    Rules:
    - day/night -> break transition: schedule pending=prev, due=now+delay
      (and fire any already-pending summary first — shouldn't happen, delay << session gap).
    - no transition, pending set and now>=due: fire pending, clear it.
    - otherwise: state unchanged, no fire.
    """
    if prev_session in ("day", "night") and new_session == "break":
        return {"fire": pending_session, "pending_session": prev_session, "due_at": now + delay}
    if pending_session is not None and now >= due_at:
        return {"fire": pending_session, "pending_session": None, "due_at": 0.0}
    return {"fire": None, "pending_session": pending_session, "due_at": due_at}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_session_timing -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add session_timing.py test_session_timing.py
git commit -m "feat(session-timing): pure defer/fire decision for delayed session summary"
```

---

## Task 2: Wire the delay into `strategy.py`

**Files:**
- Modify: `strategy.py` (constant, import, `__init__`, `_check_session_change`)

- [ ] **Step 1: Add constant + import**

In `strategy.py`, after `POLL_INTERVAL = 3     # seconds` (line 82), add:
```python
SESSION_SUMMARY_DELAY_SEC = 300   # delay session-close summary so bell/session_end trades settle
```
Next to the other local module imports (e.g. `import pnl_calc` / `from mtx_restore import ...`, ~line 14-15), add:
```python
from session_timing import session_summary_action
```

- [ ] **Step 2: Add `__init__` pending state**

After `self._current_session: Optional[str] = None` (line 183), add:
```python
        self._pending_summary_session: Optional[str] = None  # deferred session-close summary
        self._pending_summary_due:     float         = 0.0   # epoch seconds it becomes due
```

- [ ] **Step 3: Rewrite `_check_session_change`**

Replace the entire current method (strategy.py:572-584):
```python
    def _check_session_change(self):
        now     = datetime.now(TZ_TW)
        session = _get_session(now)
        if session == self._current_session:
            return
        if self._current_session in ("day", "night") and session == "break":
            self._send_session_summary(self._current_session)
            self._session_trades = []
        self._current_session = session
        if session in ("day", "night"):
            logger.info(f"{'日盤' if session == 'day' else '夜盤'}開始")
            threading.Thread(target=self._send_open_notify, args=(session,), daemon=True).start()
```
with (NOTE: the early-return is removed so the helper runs every poll — the deferred fire happens during "break" with no transition):
```python
    def _check_session_change(self):
        now     = datetime.now(TZ_TW)
        session = _get_session(now)
        # Defer the session-close summary ~5 min so bell/session_end closes land in
        # _session_trades first. session_summary_action runs every poll (no early-return).
        act = session_summary_action(
            self._current_session, session,
            self._pending_summary_session, self._pending_summary_due,
            time.time(), SESSION_SUMMARY_DELAY_SEC,
        )
        if act["fire"] is not None:
            self._send_session_summary(act["fire"])
            self._session_trades = []
        self._pending_summary_session = act["pending_session"]
        self._pending_summary_due     = act["due_at"]
        if session != self._current_session:
            self._current_session = session
            if session in ("day", "night"):
                logger.info(f"{'日盤' if session == 'day' else '夜盤'}開始")
                threading.Thread(target=self._send_open_notify, args=(session,), daemon=True).start()
```

- [ ] **Step 4: Syntax check + unit tests**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m py_compile strategy.py && echo OK`
Expected: `OK`.
Run: `python3 -m unittest test_session_timing -v`
Expected: 8 PASS (unchanged).

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add strategy.py
git commit -m "feat(session-timing): defer session-close summary ~5min via session_summary_action"
```

---

## Task 3: End-to-end verification

**Files:** none (verification)

- [ ] **Step 1: Unit suite + compile**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_session_timing -v && python3 -m py_compile strategy.py session_timing.py && echo OK`
Expected: 8 tests PASS, `OK`.

- [ ] **Step 2: Defer-then-fire logic simulation (no Telegram)**

Run:
```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
python3 -c "
import session_timing as st
DELAY=300.0
# 13:45 transition day->break at t=1000 -> defer
a = st.session_summary_action('day','break', None, 0.0, 1000.0, DELAY)
assert a['fire'] is None and a['pending_session']=='day' and a['due_at']==1300.0, a
# 13:47 poll (t=1120) still in break, before due -> no fire
b = st.session_summary_action('break','break', a['pending_session'], a['due_at'], 1120.0, DELAY)
assert b['fire'] is None and b['pending_session']=='day', b
# 13:50 poll (t=1300) due -> fire once, clear
c = st.session_summary_action('break','break', b['pending_session'], b['due_at'], 1300.0, DELAY)
assert c['fire']=='day' and c['pending_session'] is None, c
# next poll (t=1303) nothing pending -> no double fire
d = st.session_summary_action('break','break', c['pending_session'], c['due_at'], 1303.0, DELAY)
assert d['fire'] is None, d
print('defer-then-fire sim OK: deferred at 13:45, fired once at 13:50, no double-fire')
"
```
Expected: `defer-then-fire sim OK: ...`.

- [ ] **Step 3: Deploy readiness note (controller acts later, ask-first)**

Deploy is observe-first + ask-first (real trader). When approved: scp `strategy.py` + `session_timing.py` to VPS, `sha256sum` both ends, restart on a weekday. Confirm: the next day-close Telegram summary arrives ~13:50 (not 13:45). Until restart, behavior is unchanged.

---

## Self-Review

**Spec coverage:** pure `session_summary_action` with the full behavior table (Task 1) ✓; `SESSION_SUMMARY_DELAY_SEC=300` + `__init__` pending state (Task 2) ✓; `_check_session_change` rewrite that removes the early-return so the helper runs every poll, defers on transition, fires when due, clears `_session_trades` only on fire (Task 2) ✓; both day & night (the helper treats `prev in (day,night)` symmetrically; tests cover both) ✓; empty-summary guard already in `_send_session_summary` (unchanged) ✓; TDD pure module (Task 1) + simulation (Task 3) ✓.

**Placeholder scan:** none — full code in every code step.

**Type consistency:** `session_summary_action` returns dict keys `fire`/`pending_session`/`due_at`, used identically in the Task 1 tests and the Task 2 `_check_session_change` rewrite. `_pending_summary_session`/`_pending_summary_due` names + `SESSION_SUMMARY_DELAY_SEC` consistent across Task 2.

**Edge note (from spec, accepted):** a restart in the 13:45–13:50 window drops the in-memory pending + `_session_trades` → that session's Telegram summary is skipped (rare; `trades.jsonl` unaffected). No persistence added by design.
