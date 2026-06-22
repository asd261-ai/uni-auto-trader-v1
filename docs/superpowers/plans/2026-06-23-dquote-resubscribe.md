# dquote auto-resubscribe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the dquote tick feed by resubscribing in-place — on a dtrade reconnect (trigger B) and when the tick-stale watchdog is alerting (trigger A) — before the tick-stale kill restarts the whole process.

**Architecture:** A pure `DquoteResubPolicy` rate-limits trigger A (gated on the existing `TickStaleWatchdog.alerting` signal). `trader.resubscribe_dquote(reason)` owns the SDK call (unsubscribe+subscribe via `call_with_timeout`), the observe/arm env gate, and a min-interval guard; it is invoked from `trader._on_connected` (B) and from the strategy poll loop (A, via the policy).

**Tech Stack:** Python 3.11 (VPS) / system `python3` (local tests), stdlib `unittest`, no third-party deps. SDK calls wrapped in existing `sdk_timeout.call_with_timeout`.

## Global Constraints

- The pure policy touches NO clock and NO SDK — `now`, `alerting`, `uptime` are passed in; tests never sleep.
- Resubscribe is **unverified SDK behavior on a live account** and there is **no paper env** → observe-first: `DQUOTE_RESUB` defaults **off** (log `[dquote-resub would-fire]`, no SDK call); `on` actually resubscribes.
- Every SDK call wrapped in `call_with_timeout(fn, *arg, timeout=SDK_READ_TIMEOUT_SEC)` (default 5s) — never block the poll loop or the reconnect callback.
- `resubscribe_dquote` never raises (preserves the no-zombie philosophy of `_subscribe`); on failure the tick-stale kill is the backstop.
- Trigger A fires only while `TickStaleWatchdog.alerting` is True (that flag is already session-active + non-weekend gated). Resub stale point = alert threshold (day 90 / night 300) which is below the kill threshold (day 180 / night 600).
- A min-interval guard (`DQUOTE_RESUB_MIN_INTERVAL`, default 30s) inside `resubscribe_dquote` prevents A+B overlap and reconnect-storm spam.
- Policy defaults: cooldown 60s, max_attempts 3 per episode, grace 180s.
- Production real-money trader: the deploy/restart task (Task 4) requires explicit Sean approval before running; ship Phase A observe first.
- Local test command: `python3 -m unittest <module> -v`.

---

### Task 1: Pure `DquoteResubPolicy` class

**Files:**
- Create: `dquote_resub.py`
- Test: `test_dquote_resub.py`

**Interfaces:**
- Produces:
  - `DquoteResubPolicy(*, cooldown:float=60.0, max_attempts:int=3, grace:float=180.0)`
  - `DquoteResubPolicy.should_attempt(now:float, *, alerting:bool, uptime:float) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# test_dquote_resub.py
"""Tests for dquote_resub. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_dquote_resub -v
Time, alerting flag and uptime are passed in — deterministic, instant.
"""
from __future__ import annotations

import unittest

from dquote_resub import DquoteResubPolicy


def make():
    return DquoteResubPolicy(cooldown=60, max_attempts=3, grace=180)


class DquoteResubPolicyTests(unittest.TestCase):
    def test_invalid_construction_raises(self):
        with self.assertRaises(ValueError):
            DquoteResubPolicy(cooldown=0, max_attempts=3, grace=180)
        with self.assertRaises(ValueError):
            DquoteResubPolicy(cooldown=60, max_attempts=0, grace=180)

    def test_no_attempt_within_grace(self):
        p = make()
        self.assertFalse(p.should_attempt(1000.0, alerting=True, uptime=100))  # uptime<=180

    def test_no_attempt_when_not_alerting(self):
        p = make()
        self.assertFalse(p.should_attempt(1000.0, alerting=False, uptime=999))

    def test_attempts_when_alerting_past_grace(self):
        p = make()
        self.assertTrue(p.should_attempt(1000.0, alerting=True, uptime=999))

    def test_cooldown_blocks_second_attempt(self):
        p = make()
        self.assertTrue(p.should_attempt(1000.0, alerting=True, uptime=999))   # attempt 1
        self.assertFalse(p.should_attempt(1030.0, alerting=True, uptime=999))  # 30 < 60 cooldown
        self.assertTrue(p.should_attempt(1061.0, alerting=True, uptime=999))   # 61 > 60 -> attempt 2

    def test_max_attempts_then_stop(self):
        p = make()
        self.assertTrue(p.should_attempt(1000.0, alerting=True, uptime=999))   # 1
        self.assertTrue(p.should_attempt(1100.0, alerting=True, uptime=999))   # 2
        self.assertTrue(p.should_attempt(1200.0, alerting=True, uptime=999))   # 3
        self.assertFalse(p.should_attempt(1300.0, alerting=True, uptime=999))  # max reached -> stop

    def test_recovery_resets_episode(self):
        p = make()
        for t in (1000.0, 1100.0, 1200.0):
            p.should_attempt(t, alerting=True, uptime=999)                     # exhaust 3
        self.assertFalse(p.should_attempt(1300.0, alerting=True, uptime=999))  # capped
        self.assertFalse(p.should_attempt(1400.0, alerting=False, uptime=999)) # feed recovered -> reset
        self.assertTrue(p.should_attempt(1500.0, alerting=True, uptime=999))   # new episode -> attempt


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_dquote_resub -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dquote_resub'`

- [ ] **Step 3: Write minimal implementation**

```python
# dquote_resub.py
"""Resubscribe rate-limit policy for the dquote tick feed (trigger A: staleness).

The broker SDK leaves the dquote feed dead after a failed/dropped subscribe with no
retry (2026-06-23 incident: a restart in the quote-maintenance window left the feed
dead ~1.5h until the tick-stale watchdog restarted the process). This policy decides
WHEN to attempt an in-place resubscribe — driven by the tick-stale watchdog's own
`alerting` signal (which already does session-anchored, weekend-gated staleness
detection), so this class only rate-limits: an uptime grace, a cooldown between
attempts, and a max number of attempts per outage episode (after which it stops and
lets the tick-stale kill restart the process as backstop). Resets when the feed
recovers (alerting clears).

Pure and dependency-free: time and the alerting flag are passed in; no clock, no SDK.
The actual unsubscribe/subscribe SDK call and observe/arm gating live in trader.py.
See docs/superpowers/specs/2026-06-23-dquote-resubscribe-design.md.
"""
from __future__ import annotations


class DquoteResubPolicy:
    def __init__(self, *, cooldown: float = 60.0, max_attempts: int = 3, grace: float = 180.0):
        if cooldown <= 0 or max_attempts <= 0 or grace <= 0:
            raise ValueError("cooldown, max_attempts, grace must be positive")
        self.cooldown = float(cooldown)
        self.max_attempts = int(max_attempts)
        self.grace = float(grace)
        self._last_attempt = 0.0
        self._attempts = 0

    def should_attempt(self, now: float, *, alerting: bool, uptime: float) -> bool:
        # anti boot: don't act until past the uptime grace
        if uptime <= self.grace:
            return False
        # feed healthy (tick-wd not alerting) -> episode over, re-arm
        if not alerting:
            self._attempts = 0
            return False
        # stale episode: cap attempts, then defer to the tick-stale kill backstop
        if self._attempts >= self.max_attempts:
            return False
        if now - self._last_attempt < self.cooldown:
            return False
        self._last_attempt = now
        self._attempts += 1
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_dquote_resub -v`
Expected: PASS — 7 tests OK

- [ ] **Step 5: Commit**

```bash
git add dquote_resub.py test_dquote_resub.py
git commit -m "feat(dquote-resub): pure resubscribe rate-limit policy + tests"
```

---

### Task 2: `trader.resubscribe_dquote` + trigger B (dtrade reconnect)

**Files:**
- Modify: `trader.py` (env consts after line 10; `self._last_resub_ts` in `__init__` ~line 21; new `resubscribe_dquote` method after `_subscribe` ~line 120; trigger-B call in `_on_connected` ~line 176)

**Interfaces:**
- Consumes: `call_with_timeout`, `SDK_READ_TIMEOUT_SEC` (already imported in trader.py); `self.api.dquote.subscribe_trade_bid_offer(product) -> (ok, err)`; `self.api.dquote.unsubscribe_trade_bid_offer(product)`; `self.config["product"]`.
- Produces: `AutoTrader.resubscribe_dquote(reason: str) -> bool`; `self._last_resub_ts` attribute.

- [ ] **Step 1: Add env constants**

In `trader.py`, immediately after `SDK_READ_TIMEOUT_SEC = float(os.getenv("SDK_READ_TIMEOUT_SEC", "5"))` (line 10):

```python
# dquote auto-resubscribe: recover the tick feed in-place before the tick-stale kill
# restarts the process. Observe-first: off = log the would-fire only, on = actually
# unsubscribe+subscribe. See docs/superpowers/specs/2026-06-23-dquote-resubscribe-design.md.
DQUOTE_RESUB              = os.getenv("DQUOTE_RESUB", "off").lower() == "on"
DQUOTE_RESUB_MIN_INTERVAL = float(os.getenv("DQUOTE_RESUB_MIN_INTERVAL", "30"))  # min secs between resubscribe calls (any trigger)
```

- [ ] **Step 2: Initialize the min-interval anchor in `__init__`**

In `AutoTrader.__init__`, immediately after `self.strategy = None  # 由外部注入 MTXStrategy` (line 22):

```python
        self._last_resub_ts = 0.0  # last dquote resubscribe attempt (min-interval guard)
```

- [ ] **Step 3: Add the `resubscribe_dquote` method**

In `trader.py`, immediately after the `_subscribe` method (after line 120, before the `# ── 事件回調` comment):

```python
    def resubscribe_dquote(self, reason: str) -> bool:
        """Recover the dquote tick feed by unsubscribe+subscribe.

        Invoked on a dtrade reconnect (trigger B) and by the tick-stale-driven policy
        in the poll loop (trigger A). Observe-first: DQUOTE_RESUB off -> log the
        would-fire and make NO SDK call. A min-interval guard prevents A+B overlap and
        reconnect-storm spam. Never raises (preserves the no-zombie philosophy of
        _subscribe); on failure the tick-stale kill is the backstop. Returns True iff a
        subscribe succeeded.
        """
        now = time.time()
        if now - self._last_resub_ts < DQUOTE_RESUB_MIN_INTERVAL:
            return False
        self._last_resub_ts = now
        product = self.config["product"]
        if not DQUOTE_RESUB:
            logger.warning(f"[dquote-resub would-fire] reason={reason} product={product}")
            return False
        # Best-effort unsubscribe first (SDK may reject a duplicate subscribe); ignore outcome.
        try:
            call_with_timeout(self.api.dquote.unsubscribe_trade_bid_offer, product,
                              timeout=SDK_READ_TIMEOUT_SEC)
        except Exception as e:
            logger.debug(f"dquote unsubscribe (pre-resub) ignored: {e}")
        try:
            ok, err = call_with_timeout(self.api.dquote.subscribe_trade_bid_offer, product,
                                        timeout=SDK_READ_TIMEOUT_SEC)
        except Exception as e:
            logger.warning(f"[dquote-resub] subscribe call failed (reason={reason}): {e}")
            return False
        if not ok:
            logger.warning(f"[dquote-resub] subscribe not-ok (reason={reason}): {err}")
            return False
        logger.info(f"[dquote-resub] resubscribed to {product} (reason={reason})")
        return True
```

- [ ] **Step 4: Wire trigger B into `_on_connected`**

In `trader.py`, in `_on_connected`, immediately after `self.strategy.on_reconnect(broker_pos)` (line 176):

```python
        # Trigger B: a dtrade reconnect means connectivity is restored — the dquote feed
        # (a separate client with no auto-resubscribe) may still be dead. Attempt a
        # resubscribe so the feed recovers without waiting for the tick-stale kill.
        self.resubscribe_dquote("dtrade-reconnect")
```

- [ ] **Step 5: Verify syntax + existing tests still green**

Run: `python3 -c "import ast; ast.parse(open('trader.py').read()); print('trader.py parse OK')"`
Expected: `trader.py parse OK`

Run: `python3 -m unittest test_dquote_resub -v`
Expected: PASS — 7 tests OK (unchanged)

Run: `grep -n "resubscribe_dquote\|DQUOTE_RESUB\|_last_resub_ts" trader.py`
Expected: env consts (2), init anchor (1), method def + body refs, trigger-B call — ≥6 hits

- [ ] **Step 6: Commit**

```bash
git add trader.py
git commit -m "feat(dquote-resub): trader.resubscribe_dquote + trigger B (dtrade reconnect)"
```

---

### Task 3: Trigger A (tick-stale-driven) in the strategy poll loop

**Files:**
- Modify: `strategy.py` (import ~line 28; env consts near the tick-stale env ~line 183; construct policy near `self._tick_wd = ...` ~line 371; trigger-A check in the poll loop after the `_tick_wd.check` block ~line 795)

**Interfaces:**
- Consumes: `DquoteResubPolicy` from Task 1; `AutoTrader.resubscribe_dquote` from Task 2; `self.trader` (strategy.py:285), `self._tick_wd.alerting` (tick_watchdog.py property), `self._proc_start_ts` (used at strategy.py:791).
- Produces: `MTXStrategy._dquote_resub` (a `DquoteResubPolicy`).

- [ ] **Step 1: Add the import**

In `strategy.py`, immediately after `from pollloop_watchdog import PollLoopLivenessWatchdog` (line 28 area). If `from fd_watchdog import FdLeakWatchdog` is present, add after it instead:

```python
from dquote_resub import DquoteResubPolicy
```

- [ ] **Step 2: Add env constants**

In `strategy.py`, immediately after the tick-stale kill env block (after `TICK_STALE_KILL = os.getenv("TICK_STALE_KILL", "off").lower() == "on"  # Phase B arms os._exit`, line 183):

```python
# dquote resubscribe policy (trigger A): rate-limits staleness-driven feed recovery,
# gated on the tick-stale watchdog's `alerting` flag. See dquote_resub.py / design spec 2026-06-23.
DQUOTE_RESUB_COOLDOWN = float(os.getenv("DQUOTE_RESUB_COOLDOWN", "60"))
DQUOTE_RESUB_MAX      = int(os.getenv("DQUOTE_RESUB_MAX", "3"))
DQUOTE_RESUB_GRACE    = float(os.getenv("DQUOTE_RESUB_GRACE", "180"))
```

- [ ] **Step 3: Construct the policy**

In `strategy.py`, immediately after the `self._tick_wd = TickStaleWatchdog(...)` construction block ends (after line 371, before `self._disc_wd = ...`):

```python
        # dquote resubscribe policy (trigger A): when the tick-stale watchdog is alerting,
        # attempt an in-place resubscribe (rate-limited) before its kill tier restarts the
        # process. The SDK call + observe/arm gating live in trader.resubscribe_dquote.
        self._dquote_resub = DquoteResubPolicy(
            cooldown=DQUOTE_RESUB_COOLDOWN,
            max_attempts=DQUOTE_RESUB_MAX,
            grace=DQUOTE_RESUB_GRACE,
        )
```

- [ ] **Step 4: Wire trigger A into the poll loop**

In `strategy.py`, in `_poll_loop`, immediately after the tick-stale watchdog `try/except` block (after `logger.debug(f"tick watchdog error (silent): {e}")`, line 795), before the heartbeat block:

```python
            # Trigger A: if the tick-stale watchdog is alerting (feed stale past the alert
            # threshold, below the kill threshold), attempt an in-place dquote resubscribe
            # before the kill tier escalates to a process restart. Rate-limited by the policy;
            # the SDK call + observe/arm gating live in trader.resubscribe_dquote.
            try:
                if self._dquote_resub.should_attempt(
                    time.time(),
                    alerting=self._tick_wd.alerting,
                    uptime=time.time() - self._proc_start_ts,
                ):
                    self.trader.resubscribe_dquote("tick-stale")
            except Exception as e:
                logger.debug(f"dquote-resub error (silent): {e}")
```

- [ ] **Step 5: Verify syntax + tests + wiring**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); print('strategy.py parse OK')"`
Expected: `strategy.py parse OK`

Run: `python3 -m unittest test_dquote_resub -v`
Expected: PASS — 7 tests OK (unchanged)

Run: `grep -n "_dquote_resub\|DquoteResubPolicy\|DQUOTE_RESUB_COOLDOWN" strategy.py`
Expected: import, env consts, construction, poll-loop trigger — ≥5 hits

- [ ] **Step 6: Commit**

```bash
git add strategy.py
git commit -m "feat(dquote-resub): trigger A (tick-stale-driven) in strategy poll loop"
```

---

### Task 4: Deploy Phase A (observe) to VPS — ask Sean first

**Files:**
- Copy to VPS: `dquote_resub.py`, `test_dquote_resub.py`, `trader.py`, `strategy.py`

**⚠️ Mutates the production real-money trader. Do NOT run any step until Sean gives explicit go.** Phase A only: `DQUOTE_RESUB` stays **off** (observe — logs `[dquote-resub would-fire]`, no SDK call). Surface: "deploy dquote-resub Phase A (observe, no SDK call), scp + precheck && restart — OK?"

- [ ] **Step 1: Ask Sean for deploy approval (Phase A observe)**

State: files, that `DQUOTE_RESUB` stays off (observe-only, no real resubscribe), restart required. Wait for explicit yes.

- [ ] **Step 2: scp the changed files to the VPS**

```bash
scp dquote_resub.py test_dquote_resub.py trader.py strategy.py uni-trader:/home/ubuntu/uni-auto-trader-v1/
```

- [ ] **Step 3: Drift check — confirm VPS copies match local**

```bash
for f in dquote_resub.py test_dquote_resub.py trader.py strategy.py; do
  echo "$f: local=$(shasum -a 256 "$f" | cut -d' ' -f1)"
  ssh uni-trader "sha256sum /home/ubuntu/uni-auto-trader-v1/$f"
done
```
Expected: each local sha == VPS sha.

- [ ] **Step 4: Run the unit tests on the VPS**

```bash
ssh uni-trader "cd /home/ubuntu/uni-auto-trader-v1 && python3 -m unittest test_dquote_resub -v"
```
Expected: PASS — 7 tests OK.

- [ ] **Step 5: precheck && restart (`&&`, never `;`)**

```bash
ssh uni-trader "cd /home/ubuntu/uni-auto-trader-v1 && ./trader-precheck.sh && sudo systemctl restart uni-trader"
```
Expected: all precheck gates GO (exit 0) then restart. If precheck NO-GO → STOP, report to Sean, do not restart, do not add ignore flags.

- [ ] **Step 6: Post-restart verification**

```bash
ssh uni-trader "systemctl is-active uni-trader; sudo journalctl -u uni-trader --since '2 min ago' --no-pager | grep -iE 'Logged in|MTX restored|dquote-resub|Subscribed|error' | tail -20"
```
Expected: `active`; boot clean; `DQUOTE_RESUB` off so NO `[dquote-resub]` real action at boot; any `[dquote-resub would-fire]` only if a trigger genuinely fires.

- [ ] **Step 7: Record outcome + define Phase B gate**

Update memory `project_killtier_monday_dawn_storm.md` (the 2026-06-22/23 note about dquote no-retry): Phase A observe DEPLOYED; next = watch that triggers fire at the right time (a real feed-stale or dtrade reconnect logs `[dquote-resub would-fire]`) and never spuriously, then arm `DQUOTE_RESUB=on` (ask Sean).

---

## Notes for the implementer

- `resubscribe_dquote` and the trigger-A/B wiring are NOT separately unit-tested — this matches the repo pattern (the three watchdogs' kill/wiring are verified by ast-parse + grep + observe-phase deploy logs; only the pure decision classes carry unit tests). The pure `DquoteResubPolicy` (Task 1) is fully tested.
- Do not call the broker SDK anywhere except inside `resubscribe_dquote` (so the single min-interval guard + observe gate covers every path).
- Trigger A is gated entirely on `self._tick_wd.alerting`; do not re-derive staleness thresholds in the policy or the poll loop — reuse the watchdog's signal.
- `call_with_timeout(fn, *args, timeout=...)` runs the SDK call in a throwaway daemon thread and raises on timeout — keep both SDK calls wrapped so the maintenance-window block that caused the 2026-06-23 incident cannot wedge the caller.
