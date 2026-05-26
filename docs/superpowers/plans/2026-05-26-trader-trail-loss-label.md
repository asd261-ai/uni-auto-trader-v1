# Trader Trail/Loss Exit-Reason Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a local tick-level stop hit, the trader labels the exit `reason="trail"` when the stop has been trailed onto entry's profit side, else `reason="loss"` — fixing profitable trailing-stop exits being mislabeled as stop-losses.

**Architecture:** A new pure module `exit_reason.py` exports `stop_hit_reason(direction, stop, entry)` returning `"trail"`/`"loss"`. `strategy.py._check_exit_unit` calls it in place of the two hardcoded `"loss"` arguments on stop-hit closes. Nothing else changes — exit price, pnl, and order placement are untouched.

**Tech Stack:** Python 3 (system interpreter, stdlib only — no venv/pytest), `unittest`. LIVE real-money trader; deploy via scp + restart (ask-first).

**Spec:** `docs/superpowers/specs/2026-05-26-trader-trail-loss-label-design.md`

---

### Task 0: Branch

- [ ] **Step 1: Create a feature branch**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git checkout -b trader-trail-loss-label
```

---

### Task 1: Pure module `stop_hit_reason` (TDD)

**Files:**
- Create: `exit_reason.py`
- Test: `test_exit_reason.py`

- [ ] **Step 1: Write the failing test**

Create `test_exit_reason.py`:

```python
"""Tests for exit_reason. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_exit_reason -v
"""
import unittest

from exit_reason import stop_hit_reason


class StopHitReasonTest(unittest.TestCase):
    def test_long_trailed_into_profit(self):
        # stop trailed above entry → trailing-take-profit
        self.assertEqual(stop_hit_reason("long", 44050, 44000), "trail")

    def test_long_original_protective_stop(self):
        # stop below entry → real stop-loss
        self.assertEqual(stop_hit_reason("long", 43950, 44000), "loss")

    def test_long_breakeven_is_loss(self):
        self.assertEqual(stop_hit_reason("long", 44000, 44000), "loss")

    def test_short_trailed_into_profit_0906_case(self):
        # 2026-05-26 09:06: entry 44219, stop trailed to 44151 (below entry)
        self.assertEqual(stop_hit_reason("short", 44151, 44219), "trail")

    def test_short_original_protective_stop(self):
        # stop above entry → real stop-loss
        self.assertEqual(stop_hit_reason("short", 44349, 44219), "loss")

    def test_short_breakeven_is_loss(self):
        self.assertEqual(stop_hit_reason("short", 44219, 44219), "loss")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_exit_reason -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'exit_reason'`.

- [ ] **Step 3: Write minimal implementation**

Create `exit_reason.py`:

```python
"""Pure helper: classify a stop-hit exit as trailing-take-profit vs stop-loss.

A unit's stop is trailed in place (strategy.py overwrites unit["stop"] as price
moves favorably), so the original stop is not retained. At a stop hit, the stop's
side relative to entry tells us which kind of exit it is — equivalent to the exit
pnl sign because exit_price ≈ stop at a stop hit. Mirrors the Worker's isTrailing
semantics (worker/index.js).
"""


def stop_hit_reason(direction: str, stop: float, entry: float) -> str:
    """Return "trail" if the stop has been trailed onto entry's profit side,
    else "loss". Breakeven (stop == entry) counts as "loss"."""
    if direction == "long":
        return "trail" if stop > entry else "loss"
    return "trail" if stop < entry else "loss"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_exit_reason -v`
Expected: PASS — 6 tests OK.

- [ ] **Step 5: Commit**

```bash
git add exit_reason.py test_exit_reason.py
git commit -m "feat(trader): pure stop_hit_reason + tests"
```

---

### Task 2: Wire `stop_hit_reason` into `_check_exit_unit`

**Files:**
- Modify: `strategy.py` (import near other pure-module imports; two `reason` substitutions in `_check_exit_unit`, ~lines 1417-1426)

- [ ] **Step 1: Add the import**

Find the existing pure-module imports near the top of `strategy.py` (the lines importing `mtx_restore` and `session_timing`, e.g. `from session_timing import session_summary_action`). Add alongside them:

```python
from exit_reason import stop_hit_reason
```

- [ ] **Step 2: Substitute the long stop-hit reason**

In `_check_exit_unit`, the long branch currently reads:

```python
        if unit["dir"] == "long":
            if unit["stop"] and price <= unit["stop"]:
                logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, "loss", exit_price=price)
```

Change only the `_close_unit` reason argument:

```python
        if unit["dir"] == "long":
            if unit["stop"] and price <= unit["stop"]:
                logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, stop_hit_reason("long", unit["stop"], unit["entry"]), exit_price=price)
```

- [ ] **Step 3: Substitute the short stop-hit reason**

In the same method, the short branch currently reads:

```python
        elif unit["dir"] == "short":
            if unit["stop"] and price >= unit["stop"]:
                logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, "loss", exit_price=price)
```

Change only the `_close_unit` reason argument:

```python
        elif unit["dir"] == "short":
            if unit["stop"] and price >= unit["stop"]:
                logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
                self._close_unit(unit, stop_hit_reason("short", unit["stop"], unit["entry"]), exit_price=price)
```

(Leave the Target-hit `profit` branches and the Worker-driven path unchanged.)

- [ ] **Step 4: Verify compile + tests green**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m py_compile strategy.py exit_reason.py && echo COMPILE_OK`
Expected: prints `COMPILE_OK` (no syntax/import errors).

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_exit_reason -v`
Expected: PASS — 6 tests OK.

- [ ] **Step 5: Commit**

```bash
git add strategy.py
git commit -m "fix(trader): label trailing-stop-in-profit exit as trail not loss"
```

---

## Deployment (ask-first — do NOT run without Sean's explicit go)

LIVE real-money trader, no paper env → observe-first. scp + restart is irreversible. After the final code review, STOP and ask Sean. On his go, in a flat/break window:

```bash
# from repo root, with Sean's explicit go:
scp strategy.py exit_reason.py uni-trader:/home/ubuntu/uni-auto-trader-v1/
# sha256 verify both files match local↔VPS, then:
ssh uni-trader "sudo systemctl restart uni-trader"   # only when flat/break
```

Then confirm boot clean (login OK, modules import, no crash), and on the first post-deploy trailing-stop-hit exit confirm the log shows `reason=trail` + Telegram `🔒 移動停利` (an original-stop loss still shows `reason=loss`).
