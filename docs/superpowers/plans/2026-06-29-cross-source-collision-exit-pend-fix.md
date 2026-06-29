# Cross-source Collision Guard + Exit-Rejection Pend Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the net-position collision bug where MTX + FVG hold opposite-direction positions in the same contract (MXFG6) — which nets to broker-0, triggers FUF0092 close-rejections, exit-fill timeouts (null P&L rows), and a queue-poison cascade.

**Architecture:** Two independent, additive fixes. **Fix A** (`order_reject.py` + `strategy.py:on_order_rejected`) removes a rejected exit pend from the FIFO immediately so it can't poison later fills. **Fix B** (`entry_guard.py` + `strategy.py:_open_unit`) skips an entry that would open an opposite-direction position while another source already holds one. Both follow the codebase's existing pure-helper + skip-and-return idioms.

**Tech Stack:** Python 3 (system python3, stdlib only — no deps). `unittest` for tests. Trader runs on VPS via systemd; deploy is scp + sha256, ask-first.

## Global Constraints

- **Pure stdlib only** — no third-party imports in helpers or tests (`python3 -m unittest`).
- **Helpers fail open** — any missing/malformed input returns the safe default (False / None), never raise. A guard must not halt trading on bad data.
- **Real money, no paper env** — observe-first. Fix B ships `CROSS_SOURCE_OPP_MODE=observe`.
- **Authoritative P&L unchanged** — orders-FIFO stays the daily truth; do not touch `pnl_calc` daily/breaker logic.
- **strategy.py is DRIFTED** — local is behind VPS (VPS-only patches). Task 1 syncs VPS→local before any strategy.py edit. Per [[feedback-vps-trader-deploy-scp]].
- **Deploy is ask-first** — implementation produces tested code + diff only. No scp/restart without Sean's explicit GO; he runs `trader-precheck.sh && systemctl restart uni-trader` himself via `!`.
- **Env var idiom** — read once at module top of strategy.py, mirroring `ENTRY_PAST_TARGET_GUARD`.

---

### Task 1: Sync VPS strategy.py → local (prerequisite)

Local `strategy.py` is stale; all later strategy.py edits must be based on the running VPS version.

**Files:**
- Modify: `strategy.py` (overwrite local with VPS copy)

- [ ] **Step 1: Pull VPS strategy.py to local**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
scp uni-trader:/home/ubuntu/uni-auto-trader-v1/strategy.py ./strategy.py
```

- [ ] **Step 2: Verify local now matches VPS**

```bash
L=$(shasum -a 256 strategy.py | awk '{print $1}')
R=$(ssh uni-trader "sha256sum /home/ubuntu/uni-auto-trader-v1/strategy.py" | awk '{print $1}')
[ "$L" = "$R" ] && echo "MATCH" || echo "DRIFT"
```
Expected: `MATCH`

- [ ] **Step 3: Confirm anchor lines exist (plan references them)**

```bash
grep -nE 'def _open_unit|def on_order_rejected|ENTRY_PAST_TARGET_GUARD|def _record_trade|import order_reject|from entry_guard import|import entry_guard' strategy.py
```
Expected: `_open_unit`, `on_order_rejected`, `ENTRY_PAST_TARGET_GUARD` usage, and the entry_guard/order_reject imports all present. Note the actual line numbers for Tasks 3 & 5.

- [ ] **Step 4: Commit the synced reality**

```bash
git add strategy.py
git commit -m "chore: sync strategy.py from VPS (capture VPS-only patches before edits)"
```

---

### Task 2: Fix A helper — `rollback_rejected_exit` (TDD, pure)

**Files:**
- Modify: `order_reject.py` (add function; file is MATCH local↔VPS)
- Test: `test_order_reject.py` (extend; existing helpers `_unit`/`_entry`/`_exit`)

**Interfaces:**
- Produces: `rollback_rejected_exit(pending_fills: list, productid: str, bs: str, our_product: str) -> Optional[dict]` — removes and returns the single matching exit pend, or None (foreign product / ambiguous / none / competing same-bs unfilled entry).

- [ ] **Step 1: Write the failing tests**

Add to `test_order_reject.py` a new test class. Note `_exit` currently returns `{"kind":"exit","bs":bs}`; extend a local variant carrying a `pe` marker so the removal-identity assertion is meaningful:

```python
def _exit_pe(bs, pe="PE"):
    return {"kind": "exit", "bs": bs, "pe": pe}


class RollbackRejectedExit(unittest.TestCase):
    def test_single_exit_removed_and_returned(self):
        ex = _exit_pe("S")
        pending = [ex]
        got = orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6")
        self.assertIs(got, ex)
        self.assertEqual(pending, [])

    def test_foreign_product_is_noop(self):
        ex = _exit_pe("S")
        pending = [ex]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFG6", "S", "MXFF6"))
        self.assertEqual(pending, [ex])

    def test_bs_mismatch_is_noop(self):
        ex = _exit_pe("B")
        pending = [ex]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6"))
        self.assertEqual(pending, [ex])

    def test_competing_same_bs_unfilled_entry_bails(self):
        # Ambiguous: reject for "S" could be the close OR the unfilled short entry → bail.
        u = _unit()
        ex = _exit_pe("S")
        pending = [ex, _entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6"))
        self.assertEqual(len(pending), 2)

    def test_filled_competing_entry_does_not_block(self):
        # A FILLED same-bs entry is not a reject candidate, so the exit is unambiguous.
        u = _unit(entry_fill=46130)
        ex = _exit_pe("S")
        pending = [ex, _entry(u, "S")]
        got = orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6")
        self.assertIs(got, ex)
        self.assertEqual(pending, [_entry(u, "S")])

    def test_two_same_bs_exits_ambiguous_noop(self):
        pending = [_exit_pe("S", "PE1"), _exit_pe("S", "PE2")]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6"))
        self.assertEqual(len(pending), 2)

    def test_empty_pending_is_noop(self):
        self.assertIsNone(orj.rollback_rejected_exit([], "MXFF6", "S", "MXFF6"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_order_reject.RollbackRejectedExit -v`
Expected: FAIL — `AttributeError: module 'order_reject' has no attribute 'rollback_rejected_exit'`

- [ ] **Step 3: Implement the helper**

Append to `order_reject.py`:

```python
def rollback_rejected_exit(pending_fills: list, productid: str, bs: str,
                           our_product: str) -> Optional[dict]:
    """Undo a rejected EXIT (close) order whose broker order was rejected (e.g. FUF0092
    no-position) so its pend stops poisoning the FIFO. The caller finalizes the pend's
    deferred record to exit_fill=null immediately instead of waiting for the 60s timeout.

    Conservative, ambiguity-averse (caller holds the strategy lock):
      - ignore foreign contracts;
      - if a same-side UNFILLED entry also pends, the reject may be for that entry → bail
        (the entry-rollback path owns that case);
      - candidates = pending EXIT orders on this side; act only when EXACTLY ONE, else bail
        and leave drift to broker reconciliation.
    Mutates pending_fills; returns the removed exit pend (carrying its 'pe'), or None.
    """
    if productid != our_product:
        return None
    if any(p.get("kind") == "entry" and p.get("bs") == bs
           and p.get("unit", {}).get("entry_fill") is None for p in pending_fills):
        return None
    candidates = [p for p in pending_fills
                  if p.get("kind") == "exit" and p.get("bs") == bs]
    if len(candidates) != 1:
        return None
    pend = candidates[0]
    pending_fills.remove(pend)
    return pend
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_order_reject -v`
Expected: PASS (new `RollbackRejectedExit` class + all existing `RollbackRejectedEntry`/`IsRejectStatus` still green)

- [ ] **Step 5: Commit**

```bash
git add order_reject.py test_order_reject.py
git commit -m "feat(order_reject): rollback_rejected_exit — clear rejected exit pend (stop queue-poison)"
```

---

### Task 3: Fix A wiring — `on_order_rejected` clears exit pend + books null now

**Files:**
- Modify: `strategy.py` — `on_order_rejected` (the method confirmed at ~line 729 on current VPS; re-confirm via Task 1 Step 3)

**Interfaces:**
- Consumes: `order_reject.rollback_rejected_exit(...)` (Task 2); existing `real_fill_pnl.finalize_exit(record, price, dir_)`, `self._record_trade(**rec)`, `self._save_pending_exit_records()`, `self._pending_exit_records`.

- [ ] **Step 1: Read the current method exactly**

```bash
grep -n 'def on_order_rejected' strategy.py    # get LINE
sed -n 'LINE,+18p' strategy.py                 # read the real body (lock, rollback_rejected_entry call, warning)
```
Confirm it currently: takes `with self._lock:`, calls `order_reject.rollback_rejected_entry(...)`, and on a returned unit logs `[order-rejected] ... rolled back`.

- [ ] **Step 2: Replace the method body to add the exit-rollback branch**

Inside the existing `with self._lock:` block, after the `rollback_rejected_entry` call, when no entry unit was rolled back, attempt the exit rollback and finalize its pend immediately. Use the EXACT existing variable names found in Step 1. The new logic:

```python
    def on_order_rejected(self, productid: str, bs: str, orderstatus: str):
        """Called from trader._on_reply (broker thread) when a reply is a rejection.
        Roll back the optimistic ENTRY unit, or — for a rejected EXIT (e.g. FUF0092
        no-position) — clear its stale pend and book exit_fill=null now so it can't
        poison the FIFO for the next fills."""
        booked_exit = None
        with self._lock:
            unit = order_reject.rollback_rejected_entry(
                self._pending_fills, self._units, productid, bs,
                self.trader.config.get("product"),
            )
            if unit is None:
                pend = order_reject.rollback_rejected_exit(
                    self._pending_fills, productid, bs,
                    self.trader.config.get("product"),
                )
                if pend is not None:
                    pe = pend.get("pe")
                    if pe is not None and pe in self._pending_exit_records:
                        self._pending_exit_records.remove(pe)
                        rec = real_fill_pnl.finalize_exit(pe["record"], None, pe["record"]["dir_"])
                        self._record_trade(**rec)
                        self._save_pending_exit_records()
                        booked_exit = rec
        if unit:
            logger.warning(
                f"[order-rejected] source={unit['source']} dir={unit['dir']} "
                f"id={unit['id']} status={orderstatus} → unit rolled back (no fill, no P&L)"
            )
        elif booked_exit is not None:
            logger.warning(
                f"[order-rejected] EXIT rejected status={orderstatus} "
                f"src={booked_exit.get('source')} id={booked_exit.get('id')} "
                f"reason={booked_exit.get('reason')} → pend cleared, exit_fill=null booked now "
                f"(no 60s wait, FIFO unpoisoned)"
            )
```

(Logging is done OUTSIDE the lock, matching the existing pattern. `_record_trade`/`_save_pending_exit_records` are called inside the lock, same as `on_fill` and `_flush_due_exit_records` do.)

- [ ] **Step 3: py_compile**

Run: `python3 -m py_compile strategy.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Run the full existing test suite (no regressions)**

Run: `python3 -m unittest discover -p 'test_*.py' -v 2>&1 | tail -20`
Expected: all tests pass (no strategy.py unit test exists for this path; py_compile + Task 2 helper tests + integration Task 6 cover it).

- [ ] **Step 5: Commit**

```bash
git add strategy.py
git commit -m "feat(strategy): on_order_rejected clears rejected exit pend, books null immediately"
```

---

### Task 4: Fix B helper — `cross_source_opposite` (TDD, pure)

**Files:**
- Modify: `entry_guard.py` (add function; file is MATCH local↔VPS)
- Test: `test_entry_guard.py` (CREATE — does not exist yet)

**Interfaces:**
- Produces: `cross_source_opposite(units: dict, source: str, direction: str) -> bool` — True iff another source key holds ≥1 unit of the opposite dir.

- [ ] **Step 1: Write the failing tests**

Create `test_entry_guard.py`:

```python
"""Tests for entry_guard. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_entry_guard -v
"""
import unittest

import entry_guard as eg


def _u(dir_):
    return {"dir": dir_}


class CrossSourceOpposite(unittest.TestCase):
    def test_other_source_opposite_blocks(self):
        # FVG long open; MTX wants short → opposite → True
        units = {"fvg": [_u("long")], "mtx": []}
        self.assertTrue(eg.cross_source_opposite(units, "mtx", "short"))

    def test_other_source_opposite_blocks_symmetric(self):
        units = {"mtx": [_u("short")], "fvg": []}
        self.assertTrue(eg.cross_source_opposite(units, "fvg", "long"))

    def test_other_source_same_direction_allowed(self):
        # MTX short + FVG short = 2 lots short at broker, nets fine → False
        units = {"mtx": [_u("short")], "fvg": []}
        self.assertFalse(eg.cross_source_opposite(units, "fvg", "short"))

    def test_no_other_source_position_allowed(self):
        units = {"mtx": [], "fvg": []}
        self.assertFalse(eg.cross_source_opposite(units, "mtx", "short"))

    def test_same_source_opposite_ignored(self):
        # Only OTHER sources count; this source's own units are not a cross-source collision.
        units = {"mtx": [_u("long")], "fvg": []}
        self.assertFalse(eg.cross_source_opposite(units, "mtx", "short"))

    def test_missing_source_key_allowed(self):
        units = {"mtx": [_u("short")]}
        self.assertFalse(eg.cross_source_opposite(units, "fvg", "short"))

    def test_malformed_units_fail_open(self):
        self.assertFalse(eg.cross_source_opposite(None, "mtx", "short"))
        self.assertFalse(eg.cross_source_opposite({"fvg": None}, "mtx", "short"))
        self.assertFalse(eg.cross_source_opposite({"fvg": [{}]}, "mtx", "short"))

    def test_unknown_direction_fail_open(self):
        units = {"fvg": [_u("long")]}
        self.assertFalse(eg.cross_source_opposite(units, "mtx", "sideways"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_entry_guard -v`
Expected: FAIL — `AttributeError: module 'entry_guard' has no attribute 'cross_source_opposite'`

- [ ] **Step 3: Implement the helper**

Append to `entry_guard.py`:

```python
def cross_source_opposite(units, source: str, direction: str) -> bool:
    """True ⇒ SKIP/observe: another strategy source already holds a position in the OPPOSITE
    direction. In a net-position account both legs net to broker-0, then closes hit FUF0092
    (no-position) and corrupt fill attribution. Same-direction cross-source is fine (it adds
    lots), so it is NOT blocked.

    Defensive: fail open (return False) on any malformed input — a guard must never halt
    entries on bad state.
    """
    opposite = {"long": "short", "short": "long"}.get(direction)
    if opposite is None:
        return False
    try:
        for src, src_units in units.items():
            if src == source or not src_units:
                continue
            for u in src_units:
                if isinstance(u, dict) and u.get("dir") == opposite:
                    return True
    except (AttributeError, TypeError):
        return False
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_entry_guard -v`
Expected: PASS (all 8 cases)

- [ ] **Step 5: Commit**

```bash
git add entry_guard.py test_entry_guard.py
git commit -m "feat(entry_guard): cross_source_opposite — detect opposite-direction cross-source collision"
```

---

### Task 5: Fix B wiring — `_open_unit` guard + `CROSS_SOURCE_OPP_MODE` env

**Files:**
- Modify: `strategy.py` — module-top env read (near `ENTRY_PAST_TARGET_GUARD`), import of `cross_source_opposite`, and the guard block in `_open_unit` (~line 1862 on current VPS; re-confirm via Task 1 Step 3)

**Interfaces:**
- Consumes: `entry_guard.cross_source_opposite(...)` (Task 4); existing `self._units`, `self._last_price` pattern, `logger`, `self._safe_health_notify`.

- [ ] **Step 1: Confirm the import + env idiom currently used**

```bash
grep -n 'entry_past_target\|ENTRY_PAST_TARGET_GUARD\|from entry_guard import\|import entry_guard' strategy.py
```
Note whether entry_guard is imported as `from entry_guard import entry_past_target` (add `cross_source_opposite` to that import) and how `ENTRY_PAST_TARGET_GUARD` is read from env (mirror it).

- [ ] **Step 2: Add the env read at module top**

Beside the existing `ENTRY_PAST_TARGET_GUARD = ...` line, add (mirror the exact idiom found in Step 1; this is the canonical form):

```python
# Cross-source opposite-direction collision guard (2026-06-29). Net-position account cannot
# hold MTX-short + FVG-long in one contract — they net to broker-0 → FUF0092 close rejects +
# null P&L rows. Modes: off (disabled) | observe (log WOULD-BLOCK, still trade) | on (skip).
CROSS_SOURCE_OPP_MODE = os.environ.get("CROSS_SOURCE_OPP_MODE", "observe").strip().lower()
```

And extend the entry_guard import:

```python
from entry_guard import entry_past_target, cross_source_opposite
```
(If the existing import is `import entry_guard`, instead call `entry_guard.cross_source_opposite(...)` in Step 3 and skip this import change.)

- [ ] **Step 3: Insert the guard in `_open_unit`**

Locate the past-target guard block (the `if place_order and ENTRY_PAST_TARGET_GUARD and entry_past_target(...)` block that ends with `return`). Insert this block immediately AFTER it, BEFORE the `if place_order:` that calls `_execute_order`:

```python
        # Cross-source opposite-collision guard (2026-06-29). Block opening an opposite-dir
        # position while another source holds one — the broker nets them to 0 and the close
        # rejects (FUF0092). off→skip check; observe→log only; on→skip-absorb.
        if place_order and CROSS_SOURCE_OPP_MODE != "off" and cross_source_opposite(
                self._units, source, direction):
            label = "加碼" if is_pyramid else "進場"
            if CROSS_SOURCE_OPP_MODE == "on":
                logger.warning(
                    f"{source.upper()} {label} SKIPPED cross-source opposite collision | "
                    f"dir={direction} id={trade.get('id')} (another source holds opposite; "
                    f"net-account would reject the close)")
                return
            else:  # observe
                logger.warning(
                    f"[cross-opp OBSERVE] WOULD BLOCK {source.upper()} {label} {direction} "
                    f"id={trade.get('id')} — another source holds opposite (still placing)")
```

(Observe mode logs and falls through; on mode returns before `_execute_order`. No Telegram per-skip, consistent with the past-target guard's structural-skip behaviour.)

- [ ] **Step 4: py_compile**

Run: `python3 -m py_compile strategy.py && echo OK`
Expected: `OK`

- [ ] **Step 5: Full test suite (no regressions)**

Run: `python3 -m unittest discover -p 'test_*.py' -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add strategy.py
git commit -m "feat(strategy): _open_unit cross-source opposite guard (CROSS_SOURCE_OPP_MODE, observe-first)"
```

---

### Task 6: Integration verification — replay the 6/29 sequence (logic-level, no broker)

**Files:**
- Test: `test_cross_source_integration.py` (CREATE — exercises the pure helpers against the real 6/29 unit shapes; does NOT import strategy.py to avoid the SDK/threading deps)

**Interfaces:**
- Consumes: `entry_guard.cross_source_opposite`, `order_reject.rollback_rejected_exit`.

- [ ] **Step 1: Write the integration test mirroring 6/29**

Create `test_cross_source_integration.py`:

```python
"""6/29 scenario regression at the pure-helper level (no SDK/threading).
Run:  python3 -m unittest test_cross_source_integration -v
"""
import unittest

import entry_guard as eg
import order_reject as orj


class June29Collision(unittest.TestCase):
    def test_fvg_long_blocked_when_mtx_short_held(self):
        # 12:30 MTX short open; 12:31 FVG long would collide.
        units = {"mtx": [{"dir": "short"}], "fvg": []}
        self.assertTrue(eg.cross_source_opposite(units, "fvg", "long"),
                        "FVG long must be flagged while MTX short is held")

    def test_rejected_exit_pend_cleared_before_next_fill(self):
        # FUF0092 close-reject for the short (B to close). Its exit pend must be removed so the
        # next entry fill is not mis-consumed.
        exit_pend = {"kind": "exit", "bs": "B", "pe": "PE-9G552"}
        pending = [exit_pend]
        got = orj.rollback_rejected_exit(pending, "MXFG6", "B", "MXFG6")
        self.assertIs(got, exit_pend)
        self.assertEqual(pending, [], "stale exit pend must be gone → no FIFO poison")
```

- [ ] **Step 2: Run it**

Run: `python3 -m unittest test_cross_source_integration -v`
Expected: PASS (both cases).

- [ ] **Step 3: Run the entire suite one final time**

Run: `python3 -m unittest discover -p 'test_*.py' -v 2>&1 | tail -25`
Expected: all green — `test_entry_guard`, `test_order_reject`, `test_cross_source_integration`, and every pre-existing test.

- [ ] **Step 4: Commit**

```bash
git add test_cross_source_integration.py
git commit -m "test: 6/29 collision + exit-reject regression at helper level"
```

---

## Deployment (NOT a plan task — ask-first, Sean executes)

After all tasks green, produce for Sean:
1. **The full diff** (`git diff 188214b..HEAD -- order_reject.py entry_guard.py strategy.py test_*.py`) and a one-paragraph summary.
2. **scp + sha256 deploy** of `order_reject.py`, `entry_guard.py`, `test_order_reject.py`, `test_entry_guard.py`, `test_cross_source_integration.py`, `strategy.py` to VPS — only after Sean's GO.
3. **Env:** set `CROSS_SOURCE_OPP_MODE=observe` in the trader env (Sean edits, per secret/env rules Bob doesn't touch values; this one is non-secret but env-change is ask-first).
4. **Restart:** `000_Agent/scripts/trader-precheck.sh && systemctl restart uni-trader` — Sean runs via `!` ([[feedback-trader-service-precheck-sop]]).
5. **Observe ≥1 trading day** of `[cross-opp OBSERVE] WOULD BLOCK` + `[order-rejected] EXIT rejected … null booked` logs; confirm the guard fires only on genuine opposite collisions and the exit-reject path books cleanly. Then ask Sean to flip `CROSS_SOURCE_OPP_MODE=on`.

## Self-Review

- **Spec coverage:** Fix B helper (Task 4) ✓, Fix B wiring + env (Task 5) ✓, Fix A helper (Task 2) ✓, Fix A wiring (Task 3) ✓, TDD test_entry_guard new (Task 4) ✓, test_order_reject extend (Task 2) ✓, strategy.py sync-first (Task 1) ✓, observe-first + deploy ask-first (Deployment) ✓, out-of-scope items not implemented ✓.
- **Placeholders:** none — every code/test step has complete content.
- **Type consistency:** `cross_source_opposite(units, source, direction)->bool` and `rollback_rejected_exit(pending_fills, productid, bs, our_product)->Optional[dict]` used identically in helper, tests, integration, and the strategy.py call sites. `finalize_exit(record, None, dir_)` matches the existing `on_fill`/`_flush_due_exit_records` signature.
- **Note for implementer:** Tasks 3 & 5 edit strategy.py at line numbers that shift after Task 1's sync — always re-grep (`grep -n 'def on_order_rejected' / 'def _open_unit' / 'ENTRY_PAST_TARGET_GUARD'`) before editing; never trust a hardcoded line number.
