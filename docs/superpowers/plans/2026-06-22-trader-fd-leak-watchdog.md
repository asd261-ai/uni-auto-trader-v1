# Trader fd-leak watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 4th watchdog that self-restarts the trader (os._exit → systemd) before the SDK's leaked fds cross the Python `select()` 1024 wall, so the OS reclaims every leaked fd on the fresh process.

**Architecture:** A pure, fully-unit-tested `FdLeakWatchdog` class (mirrors `pollloop_watchdog.py`) makes the two-tier decision from values passed in (fd count, uptime, flat-ness). `strategy.py` runs it in its own daemon thread that reads `/proc/self/fd` and in-memory position state (never the broker SDK), and owns the env-gated arming + `os._exit`.

**Tech Stack:** Python 3.11 (VPS) / system `python3` (local tests), stdlib `unittest`, no third-party deps. systemd `Restart=always` reclaims fds.

## Global Constraints

- The pure watchdog class touches NO clock, NO `/proc`, NO network — all inputs passed in; tests never sleep (copied from `pollloop_watchdog.py` discipline).
- `select()` hard wall = **1024** (Python `FD_SETSIZE`, independent of `ulimit`). Hard threshold must stay below it with margin.
- Hard tier is evaluated **before** soft tier.
- Soft tier armed on deploy: `FD_LEAK_SOFT_KILL` default **on**. Hard tier observe-first: `FD_LEAK_HARD_KILL` default **off**.
- Flat = `not self._units["mtx"] and not self._units["fvg"] and not self._pending_fills` (in-memory only, no broker call).
- Production real-money trader: the deploy/restart task (Task 3) requires explicit Sean approval before running.
- Local test command convention: `python3 -m unittest <module> -v` (stdlib, system python3, no venv).

---

### Task 1: Pure `FdLeakWatchdog` class

**Files:**
- Create: `fd_watchdog.py`
- Test: `test_fd_watchdog.py`

**Interfaces:**
- Produces:
  - `FdLeakWatchdog(*, soft_threshold:int=800, hard_threshold:int=980, check_interval:float=30.0, kill_grace:float=180.0)`
  - `FdLeakWatchdog.check(now:float, *, fd_count:int, uptime:float, is_flat:bool, on_kill:Callable[[str,str],None]) -> None` — calls `on_kill(msg, tier)` where `tier` is `"hard"` or `"soft"`.

- [ ] **Step 1: Write the failing tests**

```python
# test_fd_watchdog.py
"""Tests for fd_watchdog. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_fd_watchdog -v
Time, fd count and flat-ness are passed in explicitly — deterministic, instant.
"""
from __future__ import annotations

import unittest

from fd_watchdog import FdLeakWatchdog


def make():
    return FdLeakWatchdog(soft_threshold=800, hard_threshold=980, check_interval=30, kill_grace=180)


def cap():
    fired = []
    return fired, (lambda msg, tier: fired.append((tier, msg)))


class FdLeakWatchdogTests(unittest.TestCase):
    def test_invalid_construction_raises(self):
        with self.assertRaises(ValueError):
            FdLeakWatchdog(soft_threshold=0, hard_threshold=980, check_interval=30, kill_grace=180)
        with self.assertRaises(ValueError):   # hard < soft
            FdLeakWatchdog(soft_threshold=900, hard_threshold=800, check_interval=30, kill_grace=180)

    def test_no_fire_within_grace(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=2000, uptime=100, is_flat=True, on_kill=on_kill)  # uptime<=180
        self.assertEqual(fired, [])

    def test_no_fire_below_soft(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=799, uptime=999, is_flat=True, on_kill=on_kill)
        self.assertEqual(fired, [])

    def test_soft_fires_when_flat(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=800, uptime=999, is_flat=True, on_kill=on_kill)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "soft")
        self.assertIn("800", fired[0][1])

    def test_soft_does_not_fire_when_in_position(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=850, uptime=999, is_flat=False, on_kill=on_kill)  # below hard
        self.assertEqual(fired, [])

    def test_hard_fires_regardless_of_position(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=980, uptime=999, is_flat=False, on_kill=on_kill)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "hard")

    def test_hard_takes_precedence_over_soft(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=1000, uptime=999, is_flat=True, on_kill=on_kill)
        self.assertEqual(fired[0][0], "hard")

    def test_throttle_skips_evaluation_within_interval(self):
        wd = make()
        fired, on_kill = cap()
        wd.check(1000.0, fd_count=500, uptime=999, is_flat=True, on_kill=on_kill)  # healthy; _last_check=1000
        self.assertEqual(fired, [])
        wd.check(1020.0, fd_count=2000, uptime=999, is_flat=True, on_kill=on_kill)  # 20<30 → throttled
        self.assertEqual(fired, [])
        wd.check(1031.0, fd_count=2000, uptime=999, is_flat=True, on_kill=on_kill)  # 31>30 → evaluates → hard
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], "hard")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_fd_watchdog -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fd_watchdog'`

- [ ] **Step 3: Write minimal implementation**

```python
# fd_watchdog.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_fd_watchdog -v`
Expected: PASS — 8 tests OK

- [ ] **Step 5: Commit**

```bash
git add fd_watchdog.py test_fd_watchdog.py
git commit -m "feat(fd-wd): pure two-tier fd-leak watchdog class + tests"
```

---

### Task 2: Wire `FdLeakWatchdog` into `strategy.py`

**Files:**
- Modify: `strategy.py` (import ~line 28; env consts after line 196; construct after line 383; thread start after line 517; loop+helpers after line 820; kill method after line 2131)

**Interfaces:**
- Consumes: `FdLeakWatchdog` from Task 1; `self._units`, `self._pending_fills`, `self._proc_start_monotonic`, `self._running`, `self._safe_health_notify` (existing in `strategy.py`).
- Produces: `MTXStrategy._fd_wd`, `._fd_open_count()`, `._position_is_flat()`, `._fd_wd_loop()`, `._fd_wd_kill(msg, tier)`.

- [ ] **Step 1: Add the import**

Add after `from pollloop_watchdog import PollLoopLivenessWatchdog` (line 28):

```python
from fd_watchdog import FdLeakWatchdog
```

- [ ] **Step 2: Add env constants**

Insert immediately after line 196 (`POLLLOOP_FREEZE_KILL = ...`):

```python
# File-descriptor leak watchdog: the SDK leaks a socket per reconnect; open fds
# climb toward the Python select() 1024 wall (2026-06-22 incident, open_fds=1026).
# Restart to reclaim fds before the wall. Soft tier (flat-only) armed by default
# — a flat restart is benign; hard tier (any position) observe by default. See
# design spec 2026-06-22.
FD_LEAK_SOFT      = int(os.getenv("FD_LEAK_SOFT", "800"))
FD_LEAK_HARD      = int(os.getenv("FD_LEAK_HARD", "980"))
FD_LEAK_CHECK_SEC = int(os.getenv("FD_LEAK_CHECK_SEC", "30"))
FD_LEAK_GRACE_SEC = int(os.getenv("FD_LEAK_GRACE_SEC", "180"))
FD_LEAK_SOFT_KILL = os.getenv("FD_LEAK_SOFT_KILL", "on").lower() == "on"    # armed by default (flat restart benign)
FD_LEAK_HARD_KILL = os.getenv("FD_LEAK_HARD_KILL", "off").lower() == "on"   # observe-first
```

- [ ] **Step 3: Construct the watchdog**

Insert immediately after the `self._pollloop_wd = PollLoopLivenessWatchdog(...)` block (after line 383):

```python
        # File-descriptor leak watchdog: restart before the SDK's leaked fds hit
        # the Python select() 1024 wall. Own thread; reads /proc + in-memory state
        # only, never the broker SDK. Soft armed / hard observe by default.
        self._fd_wd = FdLeakWatchdog(
            soft_threshold=FD_LEAK_SOFT,
            hard_threshold=FD_LEAK_HARD,
            check_interval=FD_LEAK_CHECK_SEC,
            kill_grace=FD_LEAK_GRACE_SEC,
        )
```

- [ ] **Step 4: Start the watchdog thread**

Insert immediately after line 517 (`threading.Thread(target=self._pollloop_wd_loop, daemon=True).start()`):

```python
        threading.Thread(target=self._fd_wd_loop, daemon=True).start()
```

- [ ] **Step 5: Add the loop + helpers**

Insert immediately after the `_pollloop_wd_loop` method (after line 820, before `def _check_session_change`):

```python
    def _fd_open_count(self):
        # Pure-Python open-fd count via /proc; touches no broker SDK / network.
        # Returns None on read failure (never kill on missing data).
        try:
            import os as _os
            return len(_os.listdir("/proc/self/fd"))
        except Exception:
            return None

    def _position_is_flat(self) -> bool:
        # In-memory only: no broker call (the watchdog thread must never block on
        # the SDK). Mirrors the flat notion used by precheck Gate 3 / reconcile.
        return (not self._units.get("mtx")
                and not self._units.get("fvg")
                and not self._pending_fills)

    def _fd_wd_loop(self) -> None:
        # Runs in its OWN daemon thread, never the poll loop. Reads /proc/self/fd
        # (never blocks) and the in-memory position state — never the broker SDK.
        while self._running:
            try:
                fd_count = self._fd_open_count()
                if fd_count is not None:
                    self._fd_wd.check(
                        time.monotonic(),
                        fd_count=fd_count,
                        uptime=time.monotonic() - self._proc_start_monotonic,
                        is_flat=self._position_is_flat(),
                        on_kill=self._fd_wd_kill,
                    )
            except Exception as e:
                logger.debug(f"fd-wd error (silent): {e}")
            time.sleep(FD_LEAK_CHECK_SEC)
```

- [ ] **Step 6: Add the kill method**

Insert immediately after the `_pollloop_wd_kill` method (after line 2131):

```python
    def _fd_wd_kill(self, msg: str, tier: str) -> None:
        # Soft tier armed by default (FD_LEAK_SOFT_KILL on); hard tier observe by
        # default (FD_LEAK_HARD_KILL off) → log would-fire, do NOT exit until armed.
        # Armed: alert then os._exit(1) so systemd restarts and the OS reclaims fds.
        armed = FD_LEAK_HARD_KILL if tier == "hard" else FD_LEAK_SOFT_KILL
        if not armed:
            logger.error(f"[fd-wd KILL would-fire] {msg}")
            return
        logger.error(f"[fd-wd KILL] {msg}")
        try:
            self._safe_health_notify(f"🔪 Trader self-restart (fd-leak {tier}): {msg}")
        except Exception:
            pass
        import os as _os
        _os._exit(1)
```

- [ ] **Step 7: Verify syntax + Task 1 tests still green**

Run: `python3 -c "import ast,sys; ast.parse(open('strategy.py').read()); print('strategy.py parse OK')"`
Expected: `strategy.py parse OK`

Run: `python3 -m unittest test_fd_watchdog -v`
Expected: PASS — 8 tests OK (unchanged)

Run: `grep -n "_fd_wd\b\|_fd_wd_loop\|_fd_wd_kill\|FdLeakWatchdog\|FD_LEAK_SOFT_KILL" strategy.py`
Expected: import, env consts, construction, thread start, loop, kill method all present (≥8 hits)

- [ ] **Step 8: Commit**

```bash
git add strategy.py
git commit -m "feat(fd-wd): wire fd-leak watchdog into strategy (own thread, soft armed / hard observe)"
```

---

### Task 3: Deploy to VPS (ask Sean first)

**Files:**
- Copy to VPS: `fd_watchdog.py`, `strategy.py`, `test_fd_watchdog.py`

**⚠️ This task mutates the production real-money trader. Do NOT run any step until Sean gives explicit go.** Surface: "deploy fd-leak watchdog (soft armed / hard observe), scp + precheck && restart — OK?"

- [ ] **Step 1: Ask Sean for deploy approval**

State: files, that soft tier arms immediately, hard tier stays observe, restart required. Wait for explicit yes.

- [ ] **Step 2: scp the changed files to the VPS**

```bash
scp fd_watchdog.py test_fd_watchdog.py strategy.py uni-trader:/home/ubuntu/uni-auto-trader-v1/
```

- [ ] **Step 3: Drift check — confirm the VPS copies match local**

```bash
for f in fd_watchdog.py strategy.py test_fd_watchdog.py; do
  echo "$f: local=$(shasum -a 256 "$f" | cut -d' ' -f1)"
  ssh uni-trader "sha256sum /home/ubuntu/uni-auto-trader-v1/$f"
done
```
Expected: each local sha == VPS sha.

- [ ] **Step 4: Run the unit tests on the VPS (its python3.11)**

```bash
ssh uni-trader "cd /home/ubuntu/uni-auto-trader-v1 && python3 -m unittest test_fd_watchdog -v"
```
Expected: PASS — 8 tests OK.

- [ ] **Step 5: precheck && restart (`&&`, never `;`)**

```bash
ssh uni-trader "cd /home/ubuntu/uni-auto-trader-v1 && ./trader-precheck.sh && sudo systemctl restart uni-trader"
```
Expected: all precheck gates GO (exit 0) then restart. If precheck NO-GO → STOP, report to Sean, do not restart.

- [ ] **Step 6: Post-restart verification**

```bash
ssh uni-trader "systemctl is-active uni-trader; pid=\$(systemctl show uni-trader -p MainPID --value); echo fds=\$(sudo ls /proc/\$pid/fd | wc -l); sudo journalctl -u uni-trader --since '2 min ago' --no-pager | grep -iE 'fd-wd|Logged in|MTX restored|error' | tail -20"
```
Expected: `active`; fds low (~ <300); boot clean (`Logged in` / `MTX restored`); no `[fd-wd KILL]` at boot (fds far below thresholds + within grace).

- [ ] **Step 7: Record outcome**

Update memory `project_trader_fd_leak.md` (2026-06-22 section) + MEMORY.md index: watchdog DEPLOYED, soft armed / hard observe, next = arm `FD_LEAK_HARD_KILL` after one clean weekend (6/27-29) of zero false would-fire.

---

## Notes for the implementer

- `Optional`/`Callable` typing: `fd_watchdog.py` imports `Callable` only. `strategy.py` already imports what it needs; `_fd_open_count` returns `None` on failure without a type annotation to avoid touching the import block.
- Do not add a broker `get_position` call in the watchdog thread — flatness is read from in-memory `self._units`/`self._pending_fills` by design (a broker call could block/leak, defeating the purpose).
- No latch (unlike `pollloop_watchdog`): fd does not "recover" without a restart, and observe-mode repeated would-fire logs across check intervals help calibrate, mirroring `disconnect_watchdog`. The `check_interval` throttle bounds log frequency.
