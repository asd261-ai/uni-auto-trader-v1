# MTX ② Demote (MTX_DEMOTE_CODES gate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a trader-side, env-gated per-signal-code skip so MTX ② (and any future demoted code) is silent-absorbed (no real order) while the Worker keeps firing it into history as a paper record.

**Architecture:** A pure predicate `should_demote(sig_code, demote_codes)` in a new flat module `demote_gate.py` (mirroring `atr_gate.py`). `strategy.py` parses `MTX_DEMOTE_CODES` at module load into a set (mirroring the existing `HALF_SIZE_CODES` one-liner) and inserts one gate block in the MTX signal-consumption loop — after the regime gate, before the ④ ATR skip — that, on a code match, logs + fires a Health-Bot notification + advances `_last_seen_id` + `continue`s.

**Tech Stack:** Python 3 (stdlib only for the gate + tests; `python3 -m unittest`). No new dependencies.

---

## File Structure

- **Create** `demote_gate.py` — pure predicate `should_demote(sig_code, demote_codes) -> bool`. No SDK/network/strategy imports (unit-testable on system python3).
- **Create** `test_demote_gate.py` — stdlib `unittest` covering the predicate (mirrors `test_atr_gate.py`).
- **Modify** `strategy.py`:
  - module-config block (~line 105, right after `SKIP_CODE_4_ATR_GT`): add `import` + `MTX_DEMOTE_CODES` parse.
  - signal loop (~line 1416, between the regime-gate block ending ~1410 and the ATR-skip block starting ~1416): insert the demote gate block.

Env (not code, set on VPS at deploy time): `MTX_DEMOTE_CODES=2`.

---

## Task 1: Pure predicate `demote_gate.py`

**Files:**
- Create: `demote_gate.py`
- Test: `test_demote_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# test_demote_gate.py
"""Tests for demote_gate.should_demote. Pure stdlib unittest.
Run:  python3 -m unittest test_demote_gate -v
"""
import unittest

from demote_gate import should_demote


class DemoteGateTest(unittest.TestCase):

    # --- Empty demote set (env unset) — never demote ---
    def test_empty_set_never_demotes(self):
        self.assertFalse(should_demote(2, frozenset()))

    def test_empty_set_code8(self):
        self.assertFalse(should_demote(8, frozenset()))

    # --- Code in the demote set — demote ---
    def test_code2_in_set_demotes(self):
        self.assertTrue(should_demote(2, frozenset({2})))

    def test_code2_in_multi_set_demotes(self):
        self.assertTrue(should_demote(2, frozenset({2, 3})))

    def test_code3_in_multi_set_demotes(self):
        self.assertTrue(should_demote(3, frozenset({2, 3})))

    # --- Code not in the set — never demote ---
    def test_code8_not_in_set(self):
        self.assertFalse(should_demote(8, frozenset({2})))

    def test_code4_not_in_set(self):
        self.assertFalse(should_demote(4, frozenset({2})))

    # --- String sigCode coerces (Worker may send "2") ---
    def test_string_code_in_set_demotes(self):
        self.assertTrue(should_demote("2", frozenset({2})))

    def test_string_code_not_in_set(self):
        self.assertFalse(should_demote("8", frozenset({2})))

    # --- Fail-open on invalid sigCode (never demote on bad data) ---
    def test_none_code_does_not_demote(self):
        self.assertFalse(should_demote(None, frozenset({2})))

    def test_nonnumeric_string_does_not_demote(self):
        self.assertFalse(should_demote("abc", frozenset({2})))

    def test_bool_code_does_not_demote(self):
        # bool is subclass of int — True==1, exclude explicitly so True never
        # matches a {1} demote set by accident.
        self.assertFalse(should_demote(True, frozenset({1})))

    def test_float_code_does_not_demote(self):
        # Worker sends integer codes; a float is unexpected → fail-open.
        self.assertFalse(should_demote(2.0, frozenset({2})))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_demote_gate -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'demote_gate'`

- [ ] **Step 3: Write minimal implementation**

```python
# demote_gate.py
"""Per-signal-code demote skip for MTX signals (trader-side).

Pure function — no SDK / network / strategy imports, so unit-testable on
system python3 (`python3 -m unittest test_demote_gate`).

Spec: docs/superpowers/specs/2026-06-13-mtx-code2-demote-design.md

Rule: when a Worker MTX signal's code is in the configured demote set
(env MTX_DEMOTE_CODES, e.g. "2"), the trader silent-absorbs the entry
(no order, no unit) across ALL sessions and BOTH directions. The Worker
keeps firing it into signal_history as a paper record. Demote = the
signal's real-money expectancy is negative; remove the risk, keep the
data. First demoted code: ② 突破進場 (chronic soft bleed, real-fill
mean -22.9/trade, 90% CI [-41, -4.8]). See memory
project-per-signal-live-paper-review.

Fail-open: an invalid / missing sig_code never demotes.
"""


def should_demote(sig_code, demote_codes):
    """Return True iff this signal's code is in the demote set.

    Args:
        sig_code:     signal code from Worker (int, or numeric str like "2");
                      None / non-numeric / bool / float → fail-open (no demote)
        demote_codes: a set/frozenset of int codes to demote (empty = disabled)

    Returns:
        bool — True only when sig_code coerces to an int that is a member of
        demote_codes. bool is explicitly excluded (True==1 would otherwise
        match a {1} set).
    """
    if not demote_codes:
        return False  # disabled (env unset / empty)
    if isinstance(sig_code, bool):
        return False  # bool is an int subclass — never treat as a code
    if isinstance(sig_code, int):
        return sig_code in demote_codes
    if isinstance(sig_code, str):
        s = sig_code.strip()
        if s.lstrip("-").isdigit():
            return int(s) in demote_codes
        return False  # non-numeric string → fail-open
    return False  # float / None / anything else → fail-open
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_demote_gate -v`
Expected: PASS — 13 tests OK

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add demote_gate.py test_demote_gate.py
git commit -m "feat(demote): pure should_demote predicate for MTX per-code skip"
```

---

## Task 2: Parse `MTX_DEMOTE_CODES` at module load in `strategy.py`

**Files:**
- Modify: `strategy.py:19` (import) and `strategy.py:~105` (config block, right after the `SKIP_CODE_4_ATR_GT` try/except)

- [ ] **Step 1: Add the import**

In `strategy.py`, find line 19:

```python
from atr_gate import should_skip_code4_atr
```

Add immediately below it:

```python
from demote_gate import should_demote
```

- [ ] **Step 2: Add the env parse**

In `strategy.py`, find the end of the ATR-skip config block (the try/except that sets `SKIP_CODE_4_ATR_GT`, ~line 102-105):

```python
try:
    SKIP_CODE_4_ATR_GT = int(os.getenv("MTX_SKIP_CODE_4_ATR_GT", "0") or "0")
except (ValueError, TypeError):
    SKIP_CODE_4_ATR_GT = 0
```

Add immediately below it:

```python

# Per-code demote (manual switch; default OFF). MTX signals whose code is in
# MTX_DEMOTE_CODES are silent-absorbed (no order) across ALL sessions / both
# directions, while the Worker keeps firing them into history as a paper record.
# Mirrors the HALF_SIZE_CODES parse: non-digit tokens dropped, empty → disabled.
# First demoted: ② 突破進場 (real-fill mean -22.9/trade, CI excludes 0; filter
# rescue routes all NO-GO). Set via .env e.g. MTX_DEMOTE_CODES=2 ; empty → off.
# Spec: docs/superpowers/specs/2026-06-13-mtx-code2-demote-design.md
MTX_DEMOTE_CODES = {int(c) for c in os.getenv("MTX_DEMOTE_CODES", "").split(",") if c.strip().isdigit()}
```

- [ ] **Step 3: Verify the module imports cleanly (no live broker)**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -c "import ast; ast.parse(open('strategy.py').read()); print('strategy.py parses OK')"`
Expected: `strategy.py parses OK`

(Rationale: `import strategy` may pull broker/SDK side effects; an `ast.parse` confirms the edit is syntactically valid without standing up the trader.)

- [ ] **Step 4: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add strategy.py
git commit -m "feat(demote): parse MTX_DEMOTE_CODES at module load"
```

---

## Task 3: Insert the demote gate block in the signal loop

**Files:**
- Modify: `strategy.py` — insert between the regime-gate block (ends ~line 1410) and the ④ ATR-skip block (begins ~line 1416, the comment `# Code-4 ATR-gated skip ...`)

- [ ] **Step 1: Locate the insertion point**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && grep -n "fails-open: if no daily_closes\|# Code-4 ATR-gated skip" strategy.py`
Expected: two line numbers — the regime gate's trailing comment, then the ATR-skip comment. Insert between them (after the regime-gate block's `continue`/comment, before `# Code-4 ATR-gated skip`).

- [ ] **Step 2: Insert the gate block**

Immediately **before** the line `            # Code-4 ATR-gated skip (env-gated; default OFF; **night-only**`, insert:

```python
            # Per-code demote (env-gated; default OFF). MTX signals whose code
            # is in MTX_DEMOTE_CODES are silent-absorbed across all sessions /
            # both directions — the Worker still records them in history as a
            # paper trade for re-promote evaluation. Broadest signal-level gate,
            # so it runs before the direction-specific ATR/half-size skips.
            # Pyramid path returns earlier (canPyramid handled above), so this
            # only gates NEW entries — demoted code may still add as pyramid #2.
            # Spec: docs/superpowers/specs/2026-06-13-mtx-code2-demote-design.md
            if source == "mtx" and should_demote(trade.get("sigCode"), MTX_DEMOTE_CODES):
                logger.info(
                    f"MTX demote skip | code={trade.get('sigCode')} "
                    f"dir={direction} session={self._current_session} "
                    f"id={trade_id} entry={trade.get('entry')}"
                )
                _demote_msg = (
                    f"🚫 Demoted | code{trade.get('sigCode')} {direction}"
                    f" [{self._current_session}]"
                    f"\nentry={trade.get('entry')} id={trade_id}"
                )
                threading.Thread(
                    target=self._safe_health_notify, args=(_demote_msg,), daemon=True
                ).start()
                self._last_seen_id[source] = trade_id
                continue

```

- [ ] **Step 3: Verify syntactic validity**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -c "import ast; ast.parse(open('strategy.py').read()); print('strategy.py parses OK')"`
Expected: `strategy.py parses OK`

- [ ] **Step 4: Verify the gate references resolve (names exist in scope)**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && grep -n "direction\s*=\|trade_id\s*=\|self\._current_session\|self\._last_seen_id\|self\._safe_health_notify\|^import threading\|^import\|threading" strategy.py | grep -E "direction =|trade_id =|threading" | head`
Expected: confirms `direction`, `trade_id` are assigned earlier in the same loop scope and `threading` is imported (it is — used by the ATR-skip block directly below). If `threading` is NOT already imported at top, add `import threading`.

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add strategy.py
git commit -m "feat(demote): gate MTX demoted codes in signal loop, notify Health Bot"
```

---

## Task 4: Regression guard — gate is inert when env unset

**Files:**
- Test: `test_demote_gate.py` (extend) — assert the module-level default parse yields an empty set, proving default-OFF.

- [ ] **Step 1: Add the env-default test**

Append to `test_demote_gate.py` inside the `DemoteGateTest` class:

```python
    # --- Env parse mirrors HALF_SIZE_CODES: bad tokens dropped, empty → off ---
    def test_env_parse_empty_string_is_disabled(self):
        raw = ""
        parsed = {int(c) for c in raw.split(",") if c.strip().isdigit()}
        self.assertEqual(parsed, set())
        self.assertFalse(should_demote(2, parsed))

    def test_env_parse_single_code(self):
        raw = "2"
        parsed = {int(c) for c in raw.split(",") if c.strip().isdigit()}
        self.assertEqual(parsed, {2})
        self.assertTrue(should_demote(2, parsed))

    def test_env_parse_multi_and_spaces(self):
        raw = " 2 , 3 "
        parsed = {int(c) for c in raw.split(",") if c.strip().isdigit()}
        self.assertEqual(parsed, {2, 3})

    def test_env_parse_drops_bad_tokens(self):
        raw = "2,x,3"
        parsed = {int(c) for c in raw.split(",") if c.strip().isdigit()}
        self.assertEqual(parsed, {2, 3})

    def test_env_parse_all_bad_is_disabled(self):
        raw = "abc"
        parsed = {int(c) for c in raw.split(",") if c.strip().isdigit()}
        self.assertEqual(parsed, set())
```

- [ ] **Step 2: Run the full test file**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_demote_gate -v`
Expected: PASS — 18 tests OK

- [ ] **Step 3: Run the neighboring gate's tests to confirm no collateral breakage**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_atr_gate test_demote_gate -v`
Expected: PASS — both suites green

- [ ] **Step 4: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add test_demote_gate.py
git commit -m "test(demote): env-parse regression + default-OFF coverage"
```

---

## Deployment (NOT part of this plan — ask-first, separate GO)

Per spec §5 and CLAUDE.md irreversible-ask-first / trader-precheck SOP. Do **not** execute during plan implementation:

1. `sha256sum` VPS `strategy.py` vs local to detect VPS-only patch drift before scp.
2. scp `demote_gate.py` + `strategy.py` to `uni-trader:/home/ubuntu/uni-auto-trader-v1/`.
3. Add `MTX_DEMOTE_CODES=2` to VPS `.env` (non-secret; Bob may edit).
4. `000_Agent/scripts/trader-precheck.sh && systemctl restart uni-trader` (`&&`, never `;`).
5. Acceptance: Monday first ② fire → Health Bot "🚫 Demoted | code2 long …", `orders.jsonl` has no matching `sent`, Worker history still records the trade.
6. Rollback: remove env line + precheck && restart.

---

## Self-Review Notes

- **Spec coverage:** §3.1 predicate → Task 1; §3.2 strategy insertion + §3.3 env → Tasks 2-3; §4 tests → Tasks 1 & 4; §5 deploy → out-of-plan section; §6 re-promote / §7 risks → spec-only (no code). All code-bearing requirements mapped.
- **Deviation from spec, intentional (DRY + Rule 11):** spec sketched a `parse_demote_codes()` function; the codebase already parses code-lists inline via the `HALF_SIZE_CODES` set-comprehension. The plan mirrors that one-liner instead of adding a redundant parser, and keeps only the testable `should_demote` predicate in `demote_gate.py`. Spec mentioned pytest; codebase uses stdlib `unittest` — plan follows the codebase.
- **Type consistency:** `should_demote(sig_code, demote_codes)` signature identical across Tasks 1, 3, 4. `MTX_DEMOTE_CODES` name identical in Tasks 2-3. `_safe_health_notify`, `_last_seen_id`, `_current_session`, `trade_id`, `direction` all verified to exist in the loop scope (used by the adjacent ATR-skip block).
