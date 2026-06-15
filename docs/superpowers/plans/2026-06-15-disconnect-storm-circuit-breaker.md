# Disconnect-Storm Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect a broker reconnect storm during an active trading session and cleanly `os._exit(1)` for systemd restart *before* the SDK's native recursion overflows the CPython C stack (the recurring `ip 0x551368` SEGV).

**Architecture:** A new pure, side-effect-free `DisconnectStormWatchdog` (sliding window, 20 disconnects / 120s) lives in `disconnect_watchdog.py` and is driven inline from `strategy.on_disconnect()` — guaranteed to run on every disconnect even if a tight storm starves the poll thread. All side effects (`os._exit`, env reads, Telegram) stay in `strategy.py` via `_disconnect_storm_kill`, which mirrors the existing `_tick_wd_kill` observe/arm pattern. Session-gating reuses `_get_session() != "break"` so weekend/maintenance disconnects never trip a restart. `trader.py` is unchanged.

**Tech Stack:** Python 3.11, stdlib `collections.deque`, stdlib `unittest` (system python3, no deps). Spec: `docs/superpowers/specs/2026-06-15-disconnect-storm-circuit-breaker-design.md`.

**Branch:** `feat/disconnect-storm-circuit-breaker` (already checked out).

---

### Task 1: Pure `DisconnectStormWatchdog` class + unit tests

**Files:**
- Create: `disconnect_watchdog.py`
- Test: `test_disconnect_watchdog.py`

- [ ] **Step 1: Write the failing test file**

Create `test_disconnect_watchdog.py`:

```python
"""
Tests for disconnect_watchdog. Pure stdlib unittest (runs on system python3, no deps).
Run:  python3 -m unittest test_disconnect_watchdog -v
Time is passed in explicitly, so these are deterministic and instant.
"""
from __future__ import annotations

import unittest

from disconnect_watchdog import DisconnectStormWatchdog

WINDOW, MAXN = 120.0, 20


def make():
    return DisconnectStormWatchdog(window_sec=WINDOW, max_disconnects=MAXN)


class BelowThreshold(unittest.TestCase):
    def test_few_disconnects_no_storm(self):
        wd = make()
        # 19 disconnects all within the window -> still below max (20)
        for i in range(19):
            storm = wd.record_and_check(1000.0 + i, active=True)
        self.assertFalse(storm)

    def test_single_disconnect_no_storm(self):
        wd = make()
        self.assertFalse(wd.record_and_check(1000.0, active=True))


class AtThreshold(unittest.TestCase):
    def test_max_within_window_is_storm(self):
        wd = make()
        storm = False
        # 20 disconnects within 120s (1s apart) -> the 20th trips
        for i in range(20):
            storm = wd.record_and_check(1000.0 + i, active=True)
        self.assertTrue(storm)

    def test_boundary_exactly_window(self):
        wd = make()
        # 20 events spanning exactly the window edge: t=1000..1019 (<120s span) -> storm
        for i in range(19):
            self.assertFalse(wd.record_and_check(1000.0 + i, active=True))
        self.assertTrue(wd.record_and_check(1019.0, active=True))


class WindowAging(unittest.TestCase):
    def test_events_older_than_window_drop_out(self):
        wd = make()
        # Spread 25 disconnects 10s apart: the trailing 120s never holds >= 20
        storm = False
        for i in range(25):
            storm = wd.record_and_check(1000.0 + i * 10.0, active=True)
        self.assertFalse(storm)

    def test_recovered_blip_does_not_accumulate(self):
        wd = make()
        # 10 disconnects, then a long quiet gap, then 10 more -> never 20 in any 120s
        for i in range(10):
            wd.record_and_check(1000.0 + i, active=True)
        for i in range(10):
            storm = wd.record_and_check(2000.0 + i, active=True)
        self.assertFalse(storm)


class SessionGating(unittest.TestCase):
    def test_inactive_clears_and_never_storms(self):
        wd = make()
        # A full storm's worth of disconnects while inactive (weekend/maintenance)
        storm = False
        for i in range(30):
            storm = wd.record_and_check(1000.0 + i, active=False)
        self.assertFalse(storm)

    def test_inactive_resets_then_active_starts_fresh(self):
        wd = make()
        # 19 active disconnects (just below), then one inactive tick clears the window,
        # then 19 more active -> still below max because the first 19 were cleared.
        for i in range(19):
            wd.record_and_check(1000.0 + i, active=True)
        self.assertFalse(wd.record_and_check(1019.0, active=False))   # clears
        storm = False
        for i in range(19):
            storm = wd.record_and_check(1020.0 + i, active=True)
        self.assertFalse(storm)


class Purity(unittest.TestCase):
    def test_module_source_has_no_os_exit(self):
        import disconnect_watchdog
        import inspect
        src = inspect.getsource(disconnect_watchdog)
        self.assertNotIn("os._exit", src)
        self.assertNotIn("import os", src)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_disconnect_watchdog -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'disconnect_watchdog'`

- [ ] **Step 3: Write the minimal implementation**

Create `disconnect_watchdog.py`:

```python
"""
Disconnect-storm detector — pure, side-effect-free, unit-testable.

Counts broker disconnect events in a trailing sliding window. When the count
reaches `max_disconnects` within `window_sec` AND the session is active, the
caller treats it as a storm and self-restarts (os._exit -> systemd) BEFORE the
SDK's native callback recursion overflows the CPython C stack (the recurring
ip 0x551368 SEGV; see docs/superpowers/specs/2026-06-15-disconnect-storm-
circuit-breaker-design.md).

When inactive (weekend / broker maintenance / break), disconnects are expected:
the window is cleared and no storm is reported, so the trader never restart-loops
while the market is closed (the Monday-dawn-storm lesson).

Side effects (os._exit, env, Telegram) live in strategy.py, not here.
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_disconnect_watchdog -v`
Expected: PASS — all 9 tests OK.

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add disconnect_watchdog.py test_disconnect_watchdog.py
git commit -m "feat(disc-storm): pure DisconnectStormWatchdog + unit tests"
```

---

### Task 2: Wire breaker into `strategy.py` (observe-default)

**Files:**
- Modify: `strategy.py` (import ~line 25; env consts ~line 179; instantiate ~line 318; `on_disconnect` ~line 460; new `_disconnect_storm_kill` beside `_tick_wd_kill` ~line 1976)

> No new unit test: the pure class is fully covered in Task 1, session-gating
> (`_get_session`) is covered by `test_session_gating.py`, and the thin observe/arm
> wrapper `_disconnect_storm_kill` mirrors the existing un-unit-tested `_tick_wd_kill`
> (its armed branch calls `os._exit`, which cannot run in-process). Verification =
> full suite stays green + an import/parse smoke check + observe-first live logs.

- [ ] **Step 1: Add the import**

In `strategy.py`, immediately after the existing line 25 `from tick_watchdog import TickStaleWatchdog`, add:

```python
from disconnect_watchdog import DisconnectStormWatchdog
```

- [ ] **Step 2: Add the env constants**

In `strategy.py`, immediately after the existing line 179 block defining `TICK_STALE_KILL = os.getenv("TICK_STALE_KILL", "off").lower() == "on"`, add:

```python
# Disconnect-storm circuit breaker: a broker reconnect storm recurses the SDK
# native dispatch into a CPython C-stack overflow (SEGV ip 0x551368). os._exit
# cleanly before that point. Independent of TICK_STALE_KILL. Observe-default.
DISCONNECT_STORM_KILL       = os.getenv("DISCONNECT_STORM_KILL", "off").lower() == "on"
DISCONNECT_STORM_WINDOW_SEC = int(os.getenv("DISCONNECT_STORM_WINDOW_SEC", "120"))
DISCONNECT_STORM_MAX        = int(os.getenv("DISCONNECT_STORM_MAX", "20"))
```

- [ ] **Step 3: Instantiate the watchdog in `__init__`**

In `strategy.py` `MTXStrategy.__init__`, immediately after the existing
`self._tick_wd = TickStaleWatchdog(...)` block (the one ending around line 323 with
`kill_grace=TICK_STALE_KILL_GRACE_SEC,` then `)`), add:

```python
        self._disc_wd = DisconnectStormWatchdog(
            window_sec=DISCONNECT_STORM_WINDOW_SEC,
            max_disconnects=DISCONNECT_STORM_MAX,
        )
```

- [ ] **Step 4: Hook detection at the top of `on_disconnect`**

In `strategy.py`, change the start of `on_disconnect` (line 460). Current:

```python
    def on_disconnect(self):
        with self._lock:
            units = self._flatten_units()
```

to:

```python
    def on_disconnect(self):
        # Disconnect-storm circuit breaker (runs first, on every disconnect event).
        # Active only inside a real trading session — _get_session is weekday-aware
        # and returns "break" for weekends / Mon-dawn maintenance / break windows,
        # so market-closed disconnects never trip a restart.
        active = _get_session(datetime.now(TZ_TW)) != "break"
        if self._disc_wd.record_and_check(time.time(), active=active):
            self._disconnect_storm_kill(
                f"{DISCONNECT_STORM_MAX} disconnects in {DISCONNECT_STORM_WINDOW_SEC}s (active session)")
        with self._lock:
            units = self._flatten_units()
```

- [ ] **Step 5: Add the `_disconnect_storm_kill` method**

In `strategy.py`, immediately after the existing `_tick_wd_kill` method (ends ~line 1988 with `_os._exit(1)`), add a sibling method:

```python
    def _disconnect_storm_kill(self, msg: str) -> None:
        # Phase A (DISCONNECT_STORM_KILL off): observe only — log the would-fire, do NOT exit.
        # Phase B (on): alert then os._exit(1) so systemd restarts BEFORE the SDK native
        # reconnect recursion overflows the C stack (SEGV ip 0x551368).
        if not DISCONNECT_STORM_KILL:
            logger.error(f"[disc-storm KILL would-fire] {msg}")
            return
        logger.error(f"[disc-storm KILL] {msg}")
        try:
            self._safe_health_notify(f"🔪 Trader self-restart (disconnect storm): {msg}")
        except Exception:
            pass
        import os as _os
        _os._exit(1)
```

- [ ] **Step 6: Smoke-check the module parses and imports**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -c "import ast; ast.parse(open('strategy.py').read()); print('strategy.py parses OK')" && python3 -c "import disconnect_watchdog; print('disconnect_watchdog imports OK')"`
Expected: both print OK (full `import strategy` may require runtime env/deps; AST parse is the deterministic gate).

- [ ] **Step 7: Run the full test suite to verify nothing regressed**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest discover -p 'test_*.py' -v 2>&1 | tail -20`
Expected: OK — all existing tests + the 9 new disconnect-watchdog tests pass, zero failures.

- [ ] **Step 8: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add strategy.py
git commit -m "feat(disc-storm): wire breaker into on_disconnect (observe-default)"
```

---

### Task 3: Final verification + ready-for-review

**Files:** none (verification only)

- [ ] **Step 1: Confirm observe-default and no behavior change without env**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -c "import os; assert os.getenv('DISCONNECT_STORM_KILL','off')=='off'; print('observe-default confirmed (no os._exit unless DISCONNECT_STORM_KILL=on)')"`
Expected: prints confirmation. Reasoning: with the env unset, `_disconnect_storm_kill` only logs `[disc-storm KILL would-fire]` — zero behavior change to live trading.

- [ ] **Step 2: Confirm the full suite is green and diff is scoped**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest discover -p 'test_*.py' 2>&1 | tail -3 && git diff --stat main...HEAD`
Expected: tests OK; diff touches only `disconnect_watchdog.py`, `test_disconnect_watchdog.py`, `strategy.py`, and the two docs files. `trader.py` is NOT in the diff.

- [ ] **Step 3: Hand off for review (do NOT deploy)**

The branch is ready for code review (requesting-code-review). **Deployment to the VPS and arming `DISCONNECT_STORM_KILL=on` are both ask-first** — do not scp, restart, or edit VPS `.env`. Rollout sequence is in the spec's Rollout section.

---

## Self-Review

**Spec coverage:**
- Pure `DisconnectStormWatchdog` (20/120s sliding window) → Task 1 ✓
- `record_and_check(now, active)` semantics incl. inactive-clears → Task 1 tests ✓
- Env consts `DISCONNECT_STORM_KILL/_WINDOW_SEC/_MAX`, observe-default → Task 2 Step 2 ✓
- Instantiate beside `_tick_wd` → Task 2 Step 3 ✓
- Hook at top of `on_disconnect`, session-gate via `_get_session != "break"` → Task 2 Step 4 ✓
- `_disconnect_storm_kill` observe/arm mirror of `_tick_wd_kill` → Task 2 Step 5 ✓
- `trader.py` unchanged → asserted in Task 3 Step 2 ✓
- Tests mirror `test_tick_watchdog.py`, stdlib unittest → Task 1 ✓
- Observe-first rollout, deploy + arm ask-first → Task 3 Step 3 ✓

**Placeholder scan:** none — every code step has complete code; every run step has an exact command + expected output.

**Type consistency:** `DisconnectStormWatchdog(window_sec=, max_disconnects=)` and `record_and_check(now, *, active)` are used identically in the test (Task 1), the instantiation (Task 2 Step 3), and the call site (Task 2 Step 4). Method name `_disconnect_storm_kill` matches between its call site (Step 4) and definition (Step 5).
