# Settlement-Day Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MTX settlement days non-disruptive — stop the `TICK_STALE_KILL` false-kill in the 13:30–13:45 settled window (#15) and stop a settled-into-13:30 position becoming a local-state phantom (#16).

**Architecture:** Direction B (safe automation + operator-driven position). A new pure `settlement_calendar.py` detects the settlement window (3rd Wednesday 13:30–15:00, optional holiday override). `_get_session` returns `"break"` during it, which (via existing code) makes the tick-watchdog skip and the trading gate halt. Separately, `mtx_restore.py` persists the active product and the strategy drops restored units when the product changed at a rollover restart (the operator's authoritative settlement signal).

**Tech Stack:** Python 3 stdlib only. Tests are **stdlib `unittest`** run with `python3 -m unittest test_<name> -v` (NO pytest, NO third-party deps — must run on the VPS system python3). Spec: `docs/superpowers/specs/2026-06-17-settlement-day-awareness-design.md`.

**Do NOT deploy from this plan.** Implement + test locally; deployment is a separate ask-first step in a clean break before the 2026-07-15 July settlement.

---

## File Structure

- **Create** `settlement_calendar.py` — pure settlement-window detection. One responsibility, no imports beyond `datetime`. Fully unit-tested.
- **Create** `test_settlement_calendar.py` — its tests.
- **Modify** `mtx_restore.py` — add `load_mtx_product`, `rolled_over`; extend `save_mtx_state` with an optional `product` param. Pure, unit-tested.
- **Modify** `test_mtx_restore.py` — add tests for the new helpers + product round-trip. (If the file does not exist, create it following the `test_settlement_calendar.py` shape.)
- **Modify** `strategy.py` — parse `MTX_SETTLEMENT_OVERRIDE_DATE`; one-line settlement check in `_get_session`; product-change drop in the startup restore; pass product through `_save_mtx_state`. Integration glue (strategy is SDK-coupled and not unit-importable, like `trader.py`); correctness rests on the pure-module tests + review + the 7/15 live acceptance.

---

## Task 1: `settlement_calendar.py` — pure settlement-window detection

**Files:**
- Create: `settlement_calendar.py`
- Test: `test_settlement_calendar.py`

- [ ] **Step 1: Write the failing tests**

Create `test_settlement_calendar.py`:

```python
"""Tests for settlement_calendar. Pure stdlib unittest (runs on system python3).
Run:  python3 -m unittest test_settlement_calendar -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from settlement_calendar import third_wednesday, is_settlement_window


class ThirdWednesdayTests(unittest.TestCase):
    def test_month_starting_monday(self):
        # June 2026 starts on a Monday -> 3rd Wed = June 17 (the 2026-06-17 settlement)
        self.assertEqual(third_wednesday(2026, 6), date(2026, 6, 17))

    def test_month_starting_wednesday(self):
        # July 2026 starts on a Wednesday -> 3rd Wed = July 15 (next settlement)
        self.assertEqual(third_wednesday(2026, 7), date(2026, 7, 15))

    def test_month_starting_thursday(self):
        # Jan 2026 starts on a Thursday -> first Wed = Jan 7 -> 3rd Wed = Jan 21
        self.assertEqual(third_wednesday(2026, 1), date(2026, 1, 21))


class IsSettlementWindowTests(unittest.TestCase):
    def test_inside_window_on_settlement_day(self):
        self.assertTrue(is_settlement_window(datetime(2026, 6, 17, 13, 35)))
        self.assertTrue(is_settlement_window(datetime(2026, 6, 17, 13, 30)))   # inclusive start
        self.assertTrue(is_settlement_window(datetime(2026, 6, 17, 14, 59)))

    def test_boundaries_excluded(self):
        self.assertFalse(is_settlement_window(datetime(2026, 6, 17, 13, 29)))  # before 13:30
        self.assertFalse(is_settlement_window(datetime(2026, 6, 17, 15, 0)))   # 15:00 exclusive end

    def test_non_settlement_day(self):
        self.assertFalse(is_settlement_window(datetime(2026, 6, 16, 13, 35)))  # day before
        self.assertFalse(is_settlement_window(datetime(2026, 6, 18, 13, 35)))  # day after

    def test_override_date(self):
        # Holiday shifted settlement to 2026-06-18; that day is now in-window, 3rd-Wed is not.
        ov = date(2026, 6, 18)
        self.assertTrue(is_settlement_window(datetime(2026, 6, 18, 13, 35), override_date=ov))
        self.assertFalse(is_settlement_window(datetime(2026, 6, 17, 13, 35), override_date=ov))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_settlement_calendar -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'settlement_calendar'`.

- [ ] **Step 3: Write the minimal implementation**

Create `settlement_calendar.py`:

```python
"""Pure settlement-window detection for the MTX auto-trader.

No SDK / network / strategy imports — unit-testable on system python3
(python3 -m unittest test_settlement_calendar). All inputs passed in, no hidden clock.

TAIFEX equity-index futures settle on the 3rd Wednesday of the delivery month at the
13:30 day-session close; the night session (15:00) trades the next month. During
13:30-15:00 on the settlement day the front contract has settled, so no ticks arrive
and any held position is gone at the broker. Callers use this to treat that window as
"break" (no tick-stale kill, no trading) — see strategy._get_session.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

SETTLEMENT_START = time(13, 30)   # day session settles
SETTLEMENT_END = time(15, 0)      # night session opens


def third_wednesday(year: int, month: int) -> date:
    """The 3rd Wednesday of the month (nominal TAIFEX settlement day)."""
    first = date(year, month, 1)
    first_wed_offset = (2 - first.weekday()) % 7   # weekday(): Mon=0 .. Wed=2 .. Sun=6
    return date(year, month, 1 + first_wed_offset + 14)


def is_settlement_window(now: datetime, override_date: Optional[date] = None) -> bool:
    """True iff `now` (TW-local) is on the settlement day AND time in [13:30, 15:00).

    Settlement day = override_date if given (holiday shift), else the 3rd Wednesday of
    now's month. override_date is passed in (no env coupling) so this stays pure.
    """
    settle_day = override_date or third_wednesday(now.year, now.month)
    return now.date() == settle_day and SETTLEMENT_START <= now.time() < SETTLEMENT_END
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_settlement_calendar -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add settlement_calendar.py test_settlement_calendar.py
git commit -m "feat(settlement): pure settlement-window calendar (3rd-Wed 13:30-15:00 + override)"
```

---

## Task 2: `_get_session` returns `"break"` during the settlement window (#15)

**Files:**
- Modify: `strategy.py` — env block (~`:174-197`); `_get_session` (`:205-221`)

Note: `strategy.py` imports the broker SDK transitively and is **not** unit-importable (same as `trader.py`), so this integration is verified by review + the pure tests in Task 1 + the 7/15 live acceptance. There is no new unit test here; the logic it calls (`is_settlement_window`) is fully covered by Task 1.

- [ ] **Step 1: Add the env parse + import**

In `strategy.py`, add the import near the other local-module imports (the file already has `from mtx_restore import ...` at line 15):

```python
from settlement_calendar import is_settlement_window
```

Then in the env block (immediately AFTER line 197 `SDK_READ_TIMEOUT_ALERT_N = ...`), add:

```python
# Settlement-day awareness: on the 3rd Wednesday the front contract settles at 13:30,
# so 13:30-15:00 has no ticks and any held position is gone. _get_session returns
# "break" then (tick-watchdog skips, trading halts). Optional override for holiday-
# shifted settlements: MTX_SETTLEMENT_OVERRIDE_DATE=YYYY-MM-DD.
def _parse_settlement_override(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        logger.warning(f"Ignoring unparseable MTX_SETTLEMENT_OVERRIDE_DATE={raw!r} (want YYYY-MM-DD)")
        return None

_SETTLEMENT_OVERRIDE_DATE = _parse_settlement_override(os.getenv("MTX_SETTLEMENT_OVERRIDE_DATE"))
```

(`datetime` and `logger` are already imported/defined in `strategy.py`.)

- [ ] **Step 2: Add the settlement check at the top of `_get_session`**

In `_get_session` (`:205`), insert the settlement check as the FIRST statement inside the function, before `t = dt.time()`:

```python
def _get_session(dt: datetime) -> str:
    # Settlement day 13:30-15:00: front contract has settled, treat as break so the
    # tick-watchdog doesn't false-kill on the no-tick feed and the bot stops trading
    # the settled contract. See settlement_calendar + design spec 2026-06-17.
    if is_settlement_window(dt, _SETTLEMENT_OVERRIDE_DATE):
        return "break"
    # Weekday-aware (2026-06-09): the night session runs 15:00 day D -> 05:00 day D+1
    # ... (rest of the existing function UNCHANGED)
```

- [ ] **Step 3: Syntax check**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m py_compile strategy.py && echo OK`
Expected: `OK`.

- [ ] **Step 4: Confirm no other `_get_session` consumer breaks**

Run: `grep -n "_get_session(" strategy.py`
Expected call sites: `:462` (current-session tracking), `:501` (`active = ... != "break"` trading gate), `:780` (`_check_session_change`). Read each and confirm `"break"` at 13:30 on a settlement day is the intended behavior (it is, per the spec's consumer-audit table: trading halts, watchdog skips, session-close summary fires at 13:30). No code change — this is a verification step. If a NEW consumer exists that assumes `"day"` until 13:45, STOP and report.

- [ ] **Step 5: Commit**

```bash
git add strategy.py
git commit -m "feat(settlement): _get_session returns break in settlement window (stops #15 false-kill)"
```

---

## Task 3: `mtx_restore.py` — persist product + rollover detection helpers (#16 core)

**Files:**
- Modify: `mtx_restore.py`
- Test: `test_mtx_restore.py` (extend; create if absent)

- [ ] **Step 1: Write the failing tests**

Add to `test_mtx_restore.py` (create the file with this content if it does not exist; if it exists, add these test classes and the new imports):

```python
"""Tests for mtx_restore. Pure stdlib unittest (runs on system python3).
Run:  python3 -m unittest test_mtx_restore -v
"""
import json
import os
import tempfile
import unittest

from mtx_restore import (
    load_mtx_state, save_mtx_state, load_mtx_product, rolled_over,
)


class RolledOverTests(unittest.TestCase):
    def test_changed_product_is_rollover(self):
        self.assertTrue(rolled_over("MXFG6", "MXFH6"))

    def test_same_product_is_not_rollover(self):
        self.assertFalse(rolled_over("MXFG6", "MXFG6"))

    def test_missing_stored_product_is_not_rollover(self):
        # legacy file (no product key) or first boot -> conservative, never drop
        self.assertFalse(rolled_over(None, "MXFG6"))

    def test_missing_current_product_is_not_rollover(self):
        self.assertFalse(rolled_over("MXFG6", None))


class ProductPersistenceTests(unittest.TestCase):
    def _tmp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_save_then_load_product(self):
        path = self._tmp()
        save_mtx_state(path, [{"id": 1, "dir": "long"}], product="MXFG6")
        self.assertEqual(load_mtx_product(path), "MXFG6")
        self.assertEqual(load_mtx_state(path), [{"id": 1, "dir": "long"}])

    def test_save_without_product_writes_none(self):
        path = self._tmp()
        save_mtx_state(path, [], )  # product defaults to None
        self.assertIsNone(load_mtx_product(path))

    def test_load_product_legacy_file_without_key(self):
        path = self._tmp()
        with open(path, "w") as f:
            json.dump({"mtx_units": [{"id": 1}]}, f)   # no "product" key
        self.assertIsNone(load_mtx_product(path))
        self.assertEqual(load_mtx_state(path), [{"id": 1}])  # units still load

    def test_load_product_missing_file(self):
        self.assertIsNone(load_mtx_product("/nonexistent/path/x.json"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_mtx_restore -v`
Expected: FAIL — `ImportError: cannot import name 'load_mtx_product'` (and `rolled_over`).

- [ ] **Step 3: Implement the helpers in `mtx_restore.py`**

Replace the existing `save_mtx_state` (lines 31-36) with a product-aware version, and add `load_mtx_product` + `rolled_over` right after `load_mtx_state`:

```python
def load_mtx_product(path):
    """Return the persisted active product code, or None if missing/corrupt/absent."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    prod = data.get("product")
    return prod if isinstance(prod, str) and prod else None


def save_mtx_state(path, units, product=None):
    """Atomic write of the MTX units list + active product (tmp + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"product": product, "mtx_units": list(units)}, f, ensure_ascii=False)
    os.replace(tmp, path)


def rolled_over(stored_product, current_product):
    """True iff the contract rolled since the last save (= settlement rollover).

    Only fires when BOTH are known and differ — a missing stored product (legacy file
    or first boot) is conservative and never triggers a drop.
    """
    return bool(stored_product) and bool(current_product) and stored_product != current_product
```

Leave `load_mtx_state` UNCHANGED (it still returns the units list; one caller depends on its signature).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_mtx_restore -v`
Expected: PASS (all RolledOver + ProductPersistence tests). Also run the existing reconcile tests if present in the same file — they must still pass.

- [ ] **Step 5: Commit**

```bash
git add mtx_restore.py test_mtx_restore.py
git commit -m "feat(settlement): persist product + rolled_over/load_mtx_product helpers (#16 core)"
```

---

## Task 4: Wire product-change drop into the startup restore (#16 integration)

**Files:**
- Modify: `strategy.py` — startup restore (`:429-457`); `_save_mtx_state` (`:987-991`)

Note: integration glue in SDK-coupled `strategy.py` (not unit-importable). Correctness rests on Task 3's pure tests + review + the 7/15 live acceptance. Startup order is safe: `main.py:61 trader.start()` (runs `_resolve_product`, sets `config["product"]`) precedes `:62 strategy.start()` (the restore), so `self.trader.config["product"]` is the resolved contract at restore time.

- [ ] **Step 1: Update the `mtx_restore` import**

In `strategy.py:15`, extend the import:

```python
from mtx_restore import reconcile_restore, load_mtx_state, save_mtx_state, load_mtx_product, rolled_over
```

- [ ] **Step 2: Pass the product through `_save_mtx_state`**

In `_save_mtx_state` (`:987-991`), change the `save_mtx_state(...)` call (`:991`) to pass the current product:

```python
        save_mtx_state(str(MTX_STATE_PATH), self._units.get("mtx", []),
                       self.trader.config.get("product"))
```

This is the single underlying save site (call sites `:453`, `:1796`, `:1910` all go through this wrapper), so the product is now written on every persist and never nulled out.

- [ ] **Step 3: Add the product-change drop at the top of the restore block**

In `start()`, immediately AFTER `local_units = load_mtx_state(str(MTX_STATE_PATH))` (`:429`) and BEFORE `skip_restore = ...` (`:430`), insert the rollover check. Wrap the existing `skip_restore`/`reconcile` block (current `:430-457`) into an `else:`:

```python
                local_units = load_mtx_state(str(MTX_STATE_PATH))
                stored_product = load_mtx_product(str(MTX_STATE_PATH))
                current_product = self.trader.config.get("product")
                if rolled_over(stored_product, current_product):
                    # Operator rolled UNITRADE_PRODUCT at settlement -> every local unit is on
                    # the now-settled old contract. Drop them; do not restore the phantom. (#16)
                    logger.info(
                        f"Startup: settlement rollover detected — product {stored_product} -> "
                        f"{current_product}; dropping {len(local_units)} unit(s) from the settled "
                        f"contract (not restored)"
                    )
                    self._last_seen_id["mtx"] = history[0]["id"]
                    self._save_mtx_state()   # persist empty units + the new product
                else:
                    skip_restore = os.getenv("MTX_SKIP_RESTORE", "0") == "1"
                    if skip_restore:
                        # ... existing skip_restore branch UNCHANGED ...
                    else:
                        rec = reconcile_restore(local_units, history, cutoff_ms)
                        # ... existing reconcile branch UNCHANGED ...
```

Keep the existing `skip_restore`/`reconcile` bodies exactly as they are (`:431-457`) — only their indentation shifts one level deeper under the new `else:`. Do not alter their logic.

- [ ] **Step 4: Syntax check**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m py_compile strategy.py && echo OK`
Expected: `OK`.

- [ ] **Step 5: Full test suite (no regressions)**

Run: `python3 -m unittest discover -s . -p 'test_*.py' 2>&1 | tail -15`
Expected: all tests pass EXCEPT any pre-existing failures from missing third-party deps (e.g. `dotenv`/`requests` import errors in unrelated tests — note them, they are not caused by this change). The new `test_settlement_calendar` and `test_mtx_restore` must pass.

- [ ] **Step 6: Commit**

```bash
git add strategy.py
git commit -m "feat(settlement): drop settled-contract units on rollover restart (#16 integration)"
```

---

## Self-Review (completed by plan author)

**Spec coverage:** Component 1 → Task 1. Component 2 (`_get_session` break) → Task 2. Component 3 (product persistence + drop) → Tasks 3 (helpers) + 4 (wiring). Non-goals (no P&L, no calendar auto-flatten) honored — nothing in any task books P&L or flattens on a calendar. Consumer audit → Task 2 Step 4. ✓

**Placeholder scan:** the only `# ... UNCHANGED ...` markers are explicit instructions to preserve existing code verbatim (Task 4 Step 3), not missing content — the surrounding new code is complete. No TBD/TODO. ✓

**Type/name consistency:** `is_settlement_window(now, override_date=None)`, `third_wednesday(year, month)`, `load_mtx_product(path)`, `rolled_over(stored, current)`, `save_mtx_state(path, units, product=None)` — names/signatures identical across Tasks 1/2/3/4 and the tests. ✓

## Deployment (separate, ask-first — NOT part of this plan's execution)

After all tasks pass locally: sha256 drift-check vs VPS → scp the new/changed files (`settlement_calendar.py`, `mtx_restore.py`, `strategy.py`) → in a clean break `trader-precheck.sh && systemctl restart` → observe. The **2026-07-15** July settlement is the live acceptance test (no `MTX_SETTLEMENT_OVERRIDE_DATE` needed unless a holiday shifts it).
