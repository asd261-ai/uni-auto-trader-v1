# Poll-loop Liveness Watchdog + Broker-Read Timeout (Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect and auto-recover a frozen `_poll_loop` via an independent watchdog thread, and stop broker *read* hangs from freezing the loop in the first place.

**Architecture:** A new pure module `pollloop_watchdog.py` tracks the timestamp of the last completed poll-loop iteration; a dedicated daemon thread (NOT the poll loop) compares it against a freeze threshold and, when armed, calls `os._exit(1)` for a systemd restart. A new `sdk_timeout.py` bounds the broker read calls (`get_position` / `get_margin`) so a hung SDK call no longer wedges the loop. Both ship behind observe-safe defaults; the kill is env-gated observe→arm exactly like `TICK_STALE_KILL`.

**Tech Stack:** Python 3.11, stdlib `threading` + `time.monotonic`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-17-pollloop-liveness-watchdog-design.md`

**Conventions:** modules are flat at repo root (`strategy.py`, `trader.py`, `tick_watchdog.py`, …); tests are flat `test_<module>.py`; run with `python -m pytest`. Pure watchdog modules pass all time in from the caller and never sleep in tests (mirror `tick_watchdog.py` / `disconnect_watchdog.py`).

**Out of scope (Phase 2, separate plan):** broker *write* timeout (`buy`/`sell`/`replace_order`) + phantom-fill reconciliation.

**Deploy is NOT part of this plan.** This plan ends at "merged to main, unit tests green." Deploy to the live VPS is a separate, ask-first, observe-first step per spec §11 (drift-check + sha256 + 休息窗 `precheck.sh && restart`).

---

## File Structure

- **Create** `pollloop_watchdog.py` — pure liveness detector (Task 1)
- **Create** `test_pollloop_watchdog.py` — unit tests (Task 1)
- **Create** `sdk_timeout.py` — bounded-call helper (Task 2)
- **Create** `test_sdk_timeout.py` — unit tests (Task 2)
- **Modify** `strategy.py` — env block, watchdog construction, monotonic anchor, end-of-loop stamp, watchdog thread, `_pollloop_wd_kill` (Task 3)
- **Modify** `trader.py` — wrap `get_position` / `get_margin` in `call_with_timeout` (Task 4)
- **Modify** `strategy.py` — consecutive-read-timeout alert counter in recon/margin (Task 4)

---

## Task 1: `pollloop_watchdog.py` — pure liveness detector

**Files:**
- Create: `pollloop_watchdog.py`
- Test: `test_pollloop_watchdog.py`

- [ ] **Step 1: Write the failing test**

Create `test_pollloop_watchdog.py`:

```python
import pytest
from pollloop_watchdog import PollLoopLivenessWatchdog


def make():
    return PollLoopLivenessWatchdog(freeze_threshold=120, check_interval=30, kill_grace=180)


def test_invalid_construction_raises():
    with pytest.raises(ValueError):
        PollLoopLivenessWatchdog(freeze_threshold=0, check_interval=30, kill_grace=180)


def test_last_complete_age_none_until_first_record():
    wd = make()
    assert wd.last_complete_age(1000.0) is None
    wd.record_poll_complete(1000.0)
    assert wd.last_complete_age(1005.0) == 5.0


def test_no_kill_within_grace():
    wd = make()
    wd.record_poll_complete(1000.0)
    fired = []
    wd.check(1500.0, uptime=100, on_kill=fired.append)   # uptime 100 <= grace 180
    assert fired == []


def test_no_kill_when_no_iteration_yet():
    wd = make()
    fired = []
    wd.check(1000.0, uptime=999, on_kill=fired.append)   # _last_complete_ts == 0
    assert fired == []


def test_no_kill_when_healthy():
    wd = make()
    wd.record_poll_complete(1000.0)
    fired = []
    wd.check(1060.0, uptime=999, on_kill=fired.append)   # age 60 < 120
    assert fired == []


def test_kill_when_age_exceeds_threshold():
    wd = make()
    wd.record_poll_complete(1000.0)
    fired = []
    wd.check(1130.0, uptime=999, on_kill=fired.append)   # age 130 > 120
    assert len(fired) == 1
    assert "FROZEN" in fired[0]


def test_throttle_does_not_evaluate_within_interval():
    wd = make()
    wd.record_poll_complete(1000.0)
    fired = []
    wd.check(1115.0, uptime=999, on_kill=fired.append)   # age 115 healthy; _last_check=1115
    assert fired == []
    wd.check(1140.0, uptime=999, on_kill=fired.append)   # 1140-1115=25 < 30 → throttled
    assert fired == []                                    # no fire despite age 140 > 120
    wd.check(1146.0, uptime=999, on_kill=fired.append)   # 1146-1115=31 > 30 → evaluates → fire
    assert len(fired) == 1


def test_kill_fires_once_per_episode_then_rearms():
    wd = make()
    wd.record_poll_complete(1000.0)
    fired = []
    wd.check(1130.0, uptime=999, on_kill=fired.append)   # fire #1
    wd.check(1200.0, uptime=999, on_kill=fired.append)   # still frozen, latched → no fire
    assert len(fired) == 1
    wd.record_poll_complete(1210.0)                       # loop recovered → re-arm
    wd.check(1400.0, uptime=999, on_kill=fired.append)   # frozen again since 1210 → fire #2
    assert len(fired) == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python -m pytest test_pollloop_watchdog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pollloop_watchdog'`

- [ ] **Step 3: Write the minimal implementation**

Create `pollloop_watchdog.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest test_pollloop_watchdog.py -v`
Expected: PASS — 8 passed.

- [ ] **Step 5: Commit**

```bash
git add pollloop_watchdog.py test_pollloop_watchdog.py
git commit -m "feat(watchdog): pure poll-loop liveness detector + unit tests"
```

---

## Task 2: `sdk_timeout.py` — bounded broker-call helper

**Files:**
- Create: `sdk_timeout.py`
- Test: `test_sdk_timeout.py`

- [ ] **Step 1: Write the failing test**

Create `test_sdk_timeout.py`:

```python
import time
import pytest
from sdk_timeout import call_with_timeout, SDKCallTimeout


def test_returns_value_when_fast():
    assert call_with_timeout(lambda: 42, timeout=1.0) == 42


def test_passes_args_and_kwargs():
    assert call_with_timeout(lambda a, b: a + b, 2, b=3, timeout=1.0) == 5


def test_raises_on_timeout():
    def slow():
        time.sleep(5)
    with pytest.raises(SDKCallTimeout):
        call_with_timeout(slow, timeout=0.1)


def test_propagates_fn_exception():
    def boom():
        raise ValueError("nope")
    with pytest.raises(ValueError):
        call_with_timeout(boom, timeout=1.0)


def test_stuck_call_does_not_block_next_call():
    def slow():
        time.sleep(2)
    t0 = time.monotonic()
    with pytest.raises(SDKCallTimeout):
        call_with_timeout(slow, timeout=0.1)            # abandons the stuck thread
    assert call_with_timeout(lambda: "ok", timeout=0.5) == "ok"   # next call works at once
    assert time.monotonic() - t0 < 1.5                   # didn't wait for the 2s stuck call
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest test_sdk_timeout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sdk_timeout'`

- [ ] **Step 3: Write the minimal implementation**

Create `sdk_timeout.py`:

```python
"""Run a blocking call with a hard timeout, in a throwaway daemon thread.

The Unitrade broker SDK makes synchronous C-native calls with no timeout; a hung
internal socket blocks the caller forever and wedges the whole poll loop.
call_with_timeout bounds that: it runs fn in a FRESH daemon thread and waits up
to `timeout`. On timeout it raises SDKCallTimeout and ABANDONS the thread (daemon
→ dies with the process). A fresh thread per call (not a shared pool) means a
stuck call never blocks the next one. A truly wedged SDK is then caught by the
poll-loop liveness watchdog. See spec 2026-06-17, component 3.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class SDKCallTimeout(Exception):
    pass


def call_with_timeout(fn: Callable[..., Any], *args: Any, timeout: float, **kwargs: Any) -> Any:
    result: list = []
    error: list = []

    def _run() -> None:
        try:
            result.append(fn(*args, **kwargs))
        except BaseException as e:        # surface the SDK's real error to the caller
            error.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise SDKCallTimeout(f"call to {getattr(fn, '__name__', fn)!r} exceeded {timeout}s")
    if error:
        raise error[0]
    return result[0] if result else None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest test_sdk_timeout.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add sdk_timeout.py test_sdk_timeout.py
git commit -m "feat(sdk): call_with_timeout helper to bound hung broker calls"
```

---

## Task 3: Wire the liveness watchdog into `strategy.py`

This task is integration wiring around the live broker SDK, so it is validated by **(a)** the Task 1 unit tests covering the watchdog logic, **(b)** an import/syntax check, and **(c)** observe-first live per spec §11 (`POLLLOOP_FREEZE_KILL` defaults off → log-only, zero behavior change). Defaults are observe-safe; nothing here can exit the process until `POLLLOOP_FREEZE_KILL=on` is set on the VPS (a later, ask-first step).

**Files:**
- Modify: `strategy.py` (env block ~:177-180; `__init__` ~:276/:328; `start()` ~:453; `_poll_loop` end ~:739; new `_pollloop_wd_kill` beside `_tick_wd_kill`)

- [ ] **Step 1: Add the env block**

Beside the existing `TICK_STALE_KILL_*` block (around strategy.py:177-180), add:

```python
# Poll-loop liveness watchdog: detects a wedged _poll_loop (frozen on a hung
# broker SDK call) that the in-loop tick-stale watchdog cannot see. Observe by
# default; POLLLOOP_FREEZE_KILL=on arms os._exit. See design spec 2026-06-17.
POLLLOOP_FREEZE_SEC       = int(os.getenv("POLLLOOP_FREEZE_SEC", "120"))
POLLLOOP_FREEZE_CHECK_SEC = int(os.getenv("POLLLOOP_FREEZE_CHECK_SEC", "30"))
POLLLOOP_FREEZE_GRACE_SEC = int(os.getenv("POLLLOOP_FREEZE_GRACE_SEC", "180"))
POLLLOOP_FREEZE_KILL      = os.getenv("POLLLOOP_FREEZE_KILL", "off").lower() == "on"
```

- [ ] **Step 2: Add the import**

At the top of `strategy.py`, beside the existing watchdog imports (e.g. `from tick_watchdog import TickStaleWatchdog`), add:

```python
from pollloop_watchdog import PollLoopLivenessWatchdog
```

- [ ] **Step 3: Construct the watchdog + monotonic boot anchor in `__init__`**

Where `self._tick_wd = TickStaleWatchdog(...)` is constructed (~:328), and near `self._proc_start_ts = time.time()` (~:276), add:

```python
self._proc_start_monotonic = time.monotonic()   # monotonic boot anchor for liveness uptime
self._pollloop_wd = PollLoopLivenessWatchdog(
    freeze_threshold=POLLLOOP_FREEZE_SEC,
    check_interval=POLLLOOP_FREEZE_CHECK_SEC,
    kill_grace=POLLLOOP_FREEZE_GRACE_SEC,
)
```

- [ ] **Step 4: Add the kill callback (mirror `_tick_wd_kill`)**

Beside `_tick_wd_kill` / `_disconnect_storm_kill`, add:

```python
def _pollloop_wd_kill(self, msg: str) -> None:
    # Observe (POLLLOOP_FREEZE_KILL off): log the would-fire, do NOT exit.
    # Armed (on): alert then os._exit(1) so systemd restarts the wedged process.
    if not POLLLOOP_FREEZE_KILL:
        logger.error(f"[pollloop-wd KILL would-fire] {msg}")
        return
    logger.error(f"[pollloop-wd KILL] {msg}")
    try:
        self._safe_health_notify(f"🔪 Trader self-restart (poll-loop freeze): {msg}")
    except Exception:
        pass
    import os as _os
    _os._exit(1)
```

- [ ] **Step 5: Add the watchdog thread loop**

Beside `_poll_loop`, add the dedicated thread target:

```python
def _pollloop_wd_loop(self) -> None:
    # Runs in its OWN daemon thread, never the poll loop — so a wedged poll loop
    # cannot freeze the watchdog. Touches no broker SDK / network.
    while self._running:
        try:
            self._pollloop_wd.check(
                time.monotonic(),
                uptime=time.monotonic() - self._proc_start_monotonic,
                on_kill=self._pollloop_wd_kill,
            )
        except Exception as e:
            logger.debug(f"pollloop-wd error (silent): {e}")
        time.sleep(POLLLOOP_FREEZE_CHECK_SEC)
```

- [ ] **Step 6: Stamp at the end of `_poll_loop` + spawn the thread in `start()`**

In `_poll_loop`, as the LAST statement of the loop body, immediately before `time.sleep(POLL_INTERVAL)` (~:739):

```python
            self._pollloop_wd.record_poll_complete(time.monotonic())
            time.sleep(POLL_INTERVAL)
```

In `start()`, beside the existing `threading.Thread(target=self._poll_loop, daemon=True).start()` (~:453):

```python
        threading.Thread(target=self._pollloop_wd_loop, daemon=True).start()
```

- [ ] **Step 7: Verify it imports and the full unit suite still passes**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python -c "import ast; ast.parse(open('strategy.py').read()); print('strategy.py parses OK')" && python -m pytest test_pollloop_watchdog.py test_sdk_timeout.py -q`
Expected: `strategy.py parses OK` then all tests pass. (A full `import strategy` may require the broker SDK / env; the `ast.parse` check confirms there is no syntax error in the edits. Behavior is validated observe-first on the VPS per spec §11.)

- [ ] **Step 8: Commit**

```bash
git add strategy.py
git commit -m "feat(watchdog): wire poll-loop liveness watchdog (observe-default) into strategy.py"
```

---

## Task 4: Bound the broker read calls with a timeout

Wrap the two read-path SDK calls so a hung read can no longer freeze the loop, and add a consecutive-timeout health alert. Writes are intentionally NOT wrapped (Phase 2). Validated by Task 2 unit tests + observe-first live.

**Files:**
- Modify: `trader.py` (`_query_broker_position` ~:165-196; `_query_broker_margin_excess` ~:198-235)
- Modify: `strategy.py` (env default for the read timeout + consecutive-timeout counter in `_check_broker_reconciliation` / `_check_margin_headroom`)

- [ ] **Step 1: Add the import + env to `trader.py`**

At the top of `trader.py`:

```python
import os
from sdk_timeout import call_with_timeout, SDKCallTimeout

SDK_READ_TIMEOUT_SEC = float(os.getenv("SDK_READ_TIMEOUT_SEC", "5"))
```

- [ ] **Step 2: Wrap `get_position` in `_query_broker_position`**

In `trader.py`, change the SDK call (currently `resp = self.api.daccount.get_position(self.actno)`, ~:176) to:

```python
        try:
            resp = call_with_timeout(
                self.api.daccount.get_position, self.actno,
                timeout=SDK_READ_TIMEOUT_SEC,
            )
        except SDKCallTimeout:
            logger.error(f"broker get_position timed out after {SDK_READ_TIMEOUT_SEC}s — skipping recon cycle")
            return None
```

> NOTE: `None` is this method's existing "unavailable" return — confirm the real sentinel by reading `_query_broker_position` (it returns `None` / an empty/`unknown` shape on its existing error paths) and match it exactly so the caller's existing skip logic triggers unchanged. Do not invent a new return shape.

- [ ] **Step 3: Wrap `get_margin` in `_query_broker_margin_excess`**

In `trader.py`, change the SDK call (currently `resp = self.api.daccount.get_margin(self.actno, currency)`, ~:215) to:

```python
        try:
            resp = call_with_timeout(
                self.api.daccount.get_margin, self.actno, currency,
                timeout=SDK_READ_TIMEOUT_SEC,
            )
        except SDKCallTimeout:
            logger.error(f"broker get_margin timed out after {SDK_READ_TIMEOUT_SEC}s — skipping margin cycle")
            return None
```

> NOTE: as above, match `_query_broker_margin_excess`'s existing "unavailable" return exactly (read the method first).

- [ ] **Step 4: Add a consecutive-timeout health alert in `strategy.py`**

In `strategy.py`, add an env + counter near the other env (Task 3 Step 1):

```python
SDK_READ_TIMEOUT_ALERT_N = int(os.getenv("SDK_READ_TIMEOUT_ALERT_N", "3"))
```

In `__init__`, initialise the counter:

```python
self._sdk_read_timeout_streak = 0
```

A broker read returning the unavailable sentinel within `_check_broker_reconciliation` already causes that cycle to skip. Add, at the point where recon detects the broker position is unavailable (the existing `broker_pos is None` / schema-drift skip branch ~strategy.py:1061-1070):

```python
            self._sdk_read_timeout_streak += 1
            if self._sdk_read_timeout_streak == SDK_READ_TIMEOUT_ALERT_N:
                try:
                    self._safe_health_notify(
                        f"⚠️ broker reads unavailable {self._sdk_read_timeout_streak}x in a row "
                        f"(SDK may be wedged; reads are timing out, loop kept alive)"
                    )
                except Exception:
                    pass
```

And reset it on a successful broker read (where recon successfully obtains `broker_pos`):

```python
            self._sdk_read_timeout_streak = 0
```

> NOTE: place the increment on the unavailable/skip branch and the reset on the success branch of `_check_broker_reconciliation`. Read the method first to attach to the correct branches; do not duplicate the skip logic.

- [ ] **Step 5: Verify imports/parse + full unit suite**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python -c "import ast; ast.parse(open('trader.py').read()); ast.parse(open('strategy.py').read()); print('parse OK')" && python -m pytest test_pollloop_watchdog.py test_sdk_timeout.py -q`
Expected: `parse OK` then tests pass.

- [ ] **Step 6: Commit**

```bash
git add trader.py strategy.py
git commit -m "feat(sdk): bound broker read calls with timeout + consecutive-timeout alert"
```

---

## Task 5: Full suite + finish

- [ ] **Step 1: Run the entire test suite**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python -m pytest -q`
Expected: all tests pass (the new `test_pollloop_watchdog.py` + `test_sdk_timeout.py` plus the existing suite — `test_atr_gate`, `test_demote_gate`, `test_disconnect_watchdog`, `test_entry_guard`, `test_exit_reason`, …).

- [ ] **Step 2: Confirm no behavior change is active by default**

Verify the observe-safe defaults: `POLLLOOP_FREEZE_KILL` unset → `_pollloop_wd_kill` only logs; the read-timeout is active but only skips an already-throttled safety-net cycle on a (rare) hung read. Nothing exits the process and no trading path changes until the VPS env is armed.

- [ ] **Step 3: Push**

```bash
git push origin main
```

- [ ] **Step 4: Hand off for deploy (do NOT deploy here)**

Report that Phase 1 is merged and unit-green, and that deploy is the separate ask-first, observe-first step per spec §11: drift-check VPS `strategy.py`/`trader.py` vs baseline, scp the new modules + edited files, sha256 double-check, 休息窗 `precheck.sh && restart` with `POLLLOOP_FREEZE_KILL` unset (observe), then watch for zero false `[pollloop-wd KILL would-fire]` across sessions + an overnight before proposing arm.

---

## Self-Review

**Spec coverage:**
- G1 (detect/recover freeze, watchdog outside the loop) → Tasks 1, 3. ✓
- G2 (bound broker reads) → Tasks 2, 4. ✓
- Component 1 `pollloop_watchdog.py` → Task 1. ✓
- Component 2 strategy.py wiring (env, construct, stamp, thread, kill) → Task 3. ✓
- Component 3 `sdk_timeout.py` + trader.py read wrapping + consecutive-timeout alert → Tasks 2, 4. ✓
- D1 no session-gate → Task 1 `check()` has no session param. ✓
- D2 monotonic clock → Task 1 (caller passes monotonic), Task 3 Steps 3/5/6 use `time.monotonic()`. ✓
- D3 single threshold → Task 1 `freeze_threshold`; Task 3 single `POLLLOOP_FREEZE_SEC`. ✓
- D4 phantom-order-on-kill → out of plan scope; recon safety net is existing behavior; noted in spec. ✓
- Rollout observe→arm, deploy ask-first → Task 5 Step 4 (deploy explicitly excluded). ✓

**Placeholder scan:** the two `NOTE: match the existing sentinel / attach to the correct branch` items in Task 4 are deliberate "read before write" instructions (the exact existing return shape of `_query_broker_position` must be matched, not invented) — they instruct the engineer to read the method, with the surrounding code fully specified. No TBD/TODO/"add error handling" placeholders. ✓

**Type consistency:** `PollLoopLivenessWatchdog(freeze_threshold, check_interval, kill_grace)`, `record_poll_complete(now)`, `last_complete_age(now)`, `check(now, uptime, on_kill)` — identical across Task 1 and Task 3. `call_with_timeout(fn, *args, timeout, **kwargs)` / `SDKCallTimeout` — identical across Task 2 and Task 4. ✓
