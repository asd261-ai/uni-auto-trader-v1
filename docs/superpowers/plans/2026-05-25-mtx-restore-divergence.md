# MTX Restore-Divergence Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MTX startup restore reflects what the bot actually holds (local `mtx_state.json`), not what the Worker thinks — so lock-refused / HALF_SIZE-skipped signals never resurrect as phantom units.

**Architecture:** A new pure, dependency-free module `mtx_restore.py` holds the restore-reconciliation logic + local-state file I/O (unit-testable on system python3, like `tick_watchdog.py`). `strategy.py` persists MTX units to `mtx_state.json` on real open/close (mirroring the existing FVG `fvg_state.json` pattern) and rewrites the `start()` MTX restore to load local state and reconcile it against the Worker history.

**Tech Stack:** Python 3 stdlib only (`json`, `os`, `unittest`). Repo `asd261-ai/uni-auto-trader-v1`. Test runner: `python3 -m unittest test_<name> -v` (no deps, no venv).

**Spec:** `docs/superpowers/specs/2026-05-25-mtx-restore-divergence-design.md`

**Branch:** `mtx-restore-divergence` (already created off main; spec already committed here).

**Deploy note:** observe-first, no paper env — deploy via scp + sha256 + restart on a weekday when flat; the change only affects restart behavior.

---

## File Structure

- `mtx_restore.py` (NEW) — pure: `load_mtx_state`, `save_mtx_state`, `reconcile_restore`. No SDK/network/strategy imports.
- `test_mtx_restore.py` (NEW) — unittest for the above (state round-trip + reconcile 5 cases).
- `strategy.py` (MODIFY) — import mtx_restore; `MTX_STATE_PATH`; `_save_mtx_state`; persist in `_open_unit`/`_close_unit`; rewrite `start()` MTX restore; `_record_missed_exit` helper.
- `.gitignore` (MODIFY) — ignore `mtx_state.json` (runtime state, never commit live positions).

---

## Task 1: `mtx_restore.py` — local state save/load (atomic, corruption-safe)

**Files:**
- Create: `mtx_restore.py`
- Test: `test_mtx_restore.py`

- [ ] **Step 1: Write the failing test**

Create `test_mtx_restore.py`:

```python
"""Tests for mtx_restore. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_mtx_restore -v
"""
import os
import tempfile
import unittest

import mtx_restore as mr


class StateRoundTripTest(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "mtx_state.json")
        units = [{"id": 100, "dir": "long", "entry": 43419, "stop": 43559}]
        mr.save_mtx_state(path, units)
        self.assertEqual(mr.load_mtx_state(path), units)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(mr.load_mtx_state("/nonexistent/mtx_state.json"), [])

    def test_load_corrupt_file_returns_empty(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "mtx_state.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(mr.load_mtx_state(path), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_mtx_restore -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtx_restore'`.

- [ ] **Step 3: Write minimal implementation**

Create `mtx_restore.py`:

```python
"""Pure restore-reconciliation + local-state I/O for MTX units.

No SDK / network / strategy imports, so it is unit-testable on system python3
(python3 -m unittest test_mtx_restore).

The bot's AUTHORITATIVE record of which MTX units it actually opened is the local
mtx_state.json (written on real open/close). The Worker history is consulted only to
(a) refresh current exit levels and (b) detect exits that happened while the bot was down.
Worker-open signals the bot never filled (lock-refused / HALF_SIZE-skipped) are NOT in the
local file and must NOT be restored — that was the phantom-unit bug (2026-05-25).
"""
import json
import os

TERMINAL_STATUSES = ("profit", "loss", "trail", "reversed", "session_end")


def load_mtx_state(path):
    """Return the list of persisted MTX units, or [] if missing/corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    units = data.get("mtx_units")
    return units if isinstance(units, list) else []


def save_mtx_state(path, units):
    """Atomic write of the MTX units list (tmp + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mtx_units": list(units)}, f, ensure_ascii=False)
    os.replace(tmp, path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_mtx_restore -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add mtx_restore.py test_mtx_restore.py
git commit -m "feat(mtx-restore): local mtx_state save/load (atomic, corruption-safe)"
```

---

## Task 2: `reconcile_restore` — the core decision logic

**Files:**
- Modify: `mtx_restore.py`
- Test: `test_mtx_restore.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_mtx_restore.py` before nothing-special (end of file, the runner discovers all classes):

```python
class ReconcileRestoreTest(unittest.TestCase):
    CUTOFF = 1000  # boot floor; ids must be > this to be in-session

    def _local(self, **kw):
        u = {"id": 2000, "dir": "long", "entry": 43419, "stop": 43559, "target": 43800}
        u.update(kw)
        return u

    def test_phantom_skipped_worker_open_not_in_local(self):
        # Worker says open, but bot never filled it (not in local) -> SKIP (the bug we kill)
        rec = mr.reconcile_restore(
            local_units=[],
            worker_history=[{"id": 2000, "status": "open"}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(rec["skipped_phantoms"], [2000])

    def test_normal_restore_refreshes_levels(self):
        rec = mr.reconcile_restore(
            local_units=[self._local(stop=43559, target=43800)],
            worker_history=[{"id": 2000, "status": "open", "stop": 43600, "target": 43900}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(len(rec["to_restore"]), 1)
        self.assertEqual(rec["to_restore"][0]["stop"], 43600)    # refreshed from Worker
        self.assertEqual(rec["to_restore"][0]["target"], 43900)

    def test_missed_exit_when_worker_terminal(self):
        rec = mr.reconcile_restore(
            local_units=[self._local()],
            worker_history=[{"id": 2000, "status": "loss", "exit": 43500}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(len(rec["to_record_exit"]), 1)
        unit, worker = rec["to_record_exit"][0]
        self.assertEqual(unit["id"], 2000)
        self.assertEqual(worker["status"], "loss")

    def test_worker_missing_id_restores_conservatively(self):
        rec = mr.reconcile_restore(
            local_units=[self._local()],
            worker_history=[],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(len(rec["to_restore"]), 1)   # don't lose a real position

    def test_stale_local_unit_below_cutoff_dropped(self):
        rec = mr.reconcile_restore(
            local_units=[self._local(id=500)],         # 500 <= CUTOFF 1000
            worker_history=[{"id": 500, "status": "open"}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(rec["dropped_stale"], [500])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_mtx_restore -v`
Expected: FAIL — `AttributeError: module 'mtx_restore' has no attribute 'reconcile_restore'`.

- [ ] **Step 3: Write minimal implementation**

Add to `mtx_restore.py`:

```python
def _worker_by_id(worker_history):
    out = {}
    for t in worker_history or []:
        if isinstance(t, dict) and "id" in t:
            out[t["id"]] = t
    return out


def reconcile_restore(local_units, worker_history, cutoff_ms):
    """Decide what to do with each locally-persisted MTX unit at startup.

    Returns dict:
      to_restore       : [unit]            restore as open; stop/target refreshed from Worker
      to_record_exit   : [(unit, worker)]  exited while bot was down; record once, do NOT restore
      dropped_stale    : [id]              local unit at/below the session boot floor; drop
      skipped_phantoms : [id]              Worker-open ids NOT in local (the phantom class); not restored
    """
    by_id = _worker_by_id(worker_history)
    local_ids = set()
    to_restore, to_record_exit, dropped_stale = [], [], []
    for u in local_units or []:
        if not isinstance(u, dict) or "id" not in u:
            continue
        uid = u["id"]
        local_ids.add(uid)
        if uid <= cutoff_ms:
            dropped_stale.append(uid)
            continue
        w = by_id.get(uid)
        if w is None:
            to_restore.append(dict(u))                       # conservative: keep local as-is
        elif w.get("status") == "open":
            merged = dict(u)
            for k in ("stop", "target"):                     # refresh current levels from Worker
                if w.get(k) is not None:
                    merged[k] = w[k]
            to_restore.append(merged)
        elif w.get("status") in TERMINAL_STATUSES:
            to_record_exit.append((dict(u), dict(w)))
        else:
            to_restore.append(dict(u))                       # unknown status: conservative
    skipped_phantoms = [
        t["id"] for t in (worker_history or [])
        if isinstance(t, dict) and t.get("status") == "open"
        and t.get("id") not in local_ids and t.get("id", 0) > cutoff_ms
    ]
    return {"to_restore": to_restore, "to_record_exit": to_record_exit,
            "dropped_stale": dropped_stale, "skipped_phantoms": skipped_phantoms}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_mtx_restore -v`
Expected: PASS (3 + 5 = 8 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add mtx_restore.py test_mtx_restore.py
git commit -m "feat(mtx-restore): reconcile_restore — local-authoritative, no phantoms"
```

---

## Task 3: Wire `mtx_state.json` persistence into strategy.py

**Files:**
- Modify: `strategy.py` (import; `MTX_STATE_PATH`; `_save_mtx_state`; persist in `_open_unit`/`_close_unit`)
- Modify: `.gitignore`

- [ ] **Step 1: Add import + path constant**

In `strategy.py`, next to `import pnl_calc` (line 14), add:
```python
from mtx_restore import reconcile_restore, load_mtx_state, save_mtx_state
```
Next to `FVG_STATE_PATH = Path(__file__).parent / "fvg_state.json"` (line 108), add:
```python
MTX_STATE_PATH       = Path(__file__).parent / "mtx_state.json"
```

- [ ] **Step 2: Add `_save_mtx_state` method**

In `strategy.py`, immediately after the `_save_fvg_state` method (ends ~line 757), add:
```python
    def _save_mtx_state(self) -> None:
        """Atomic write of _units['mtx'] to disk — the bot's authoritative record of
        which MTX units it actually opened. Read at startup by the restore reconciler."""
        try:
            save_mtx_state(str(MTX_STATE_PATH), self._units.get("mtx", []))
        except Exception as e:
            logger.error(f"MTX state save failed: {e}")
```

- [ ] **Step 3: Persist on open + close**

In `_open_unit`, change the FVG-only persist (line 1323-1325):
```python
        # Persist FVG units to disk (MTX is recoverable from Worker KV)
        if source == "fvg":
            self._save_fvg_state()
```
to:
```python
        # Persist unit state to disk so restart restores what the bot ACTUALLY holds.
        if source == "fvg":
            self._save_fvg_state()
        elif source == "mtx" and place_order:   # real open only; restore path saves at end
            self._save_mtx_state()
```

In `_close_unit`, change the FVG-only persist (line 1421-1422):
```python
        # Persist FVG state after change (MTX is recoverable from Worker KV)
        if source == "fvg":
            self._save_fvg_state()
```
to:
```python
        # Persist state after change so disk reflects live positions crash-safely.
        if source == "fvg":
            self._save_fvg_state()
        elif source == "mtx":
            self._save_mtx_state()
```

- [ ] **Step 4: gitignore the runtime state file**

Append to `.gitignore` (create the line if not present):
```
mtx_state.json
mtx_state.json.tmp
```

- [ ] **Step 5: Syntax check + commit**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m py_compile strategy.py && echo OK`
Expected: `OK`.
Run the suite (still green; no behavior change yet to restore): `python3 -m unittest test_mtx_restore -v`
```bash
git add strategy.py .gitignore
git commit -m "feat(mtx-restore): persist mtx_state.json on real open/close; gitignore it"
```

---

## Task 4: Rewrite `start()` MTX restore to reconcile local vs Worker

**Files:**
- Modify: `strategy.py` (`start()` restore block ~325-336; add `_record_missed_exit`)

- [ ] **Step 1: Add `_record_missed_exit` helper**

In `strategy.py`, immediately after `_save_mtx_state` (from Task 3), add a helper that records a trade that closed while the bot was down — ONCE, without going through `_open_unit`/`_close_unit` (no double-count):
```python
    def _record_missed_exit(self, unit: dict, worker: dict) -> None:
        """A locally-held MTX unit that the Worker shows already closed (exited while the
        bot was down). Record the trade once to trades.jsonl; do NOT restore it as open."""
        exit_price = worker.get("exit", worker.get("exitPrice"))
        reason = worker.get("status", "session_end")
        entry = unit.get("entry")
        try:
            pnl_pts = None
            if entry is not None and exit_price is not None:
                pnl_pts = (exit_price - entry) if unit.get("dir") == "long" else (entry - exit_price)
            self._record_trade(
                source="mtx", label=unit.get("sig_label", ""), dir_=unit.get("dir"),
                entry=entry, exit_price=exit_price, reason=reason, pnl_pts=pnl_pts,
            )
            logger.info(f"Startup: recorded missed MTX exit id={unit.get('id')} "
                        f"reason={reason} pnl={pnl_pts}")
        except Exception as e:
            logger.warning(f"Startup: missed-exit record failed id={unit.get('id')}: {e}")
```
NOTE: verify `_record_trade`'s exact keyword signature at `strategy.py:580` and match it (it is `def _record_trade(self, *, source, label, dir_, entry, exit_price, ...)`). If `pnl_pts` is not a parameter, drop it — `_record_trade` computes pnl internally; pass only the params it declares.

- [ ] **Step 2: Replace the restore block**

In `start()`, replace the block that currently iterates `open_trades` (strategy.py ~325-336, from `skip_restore = os.getenv(...)` through the `else:` logging branch) with:
```python
                local_units = load_mtx_state(str(MTX_STATE_PATH))
                skip_restore = os.getenv("MTX_SKIP_RESTORE", "0") == "1"
                if skip_restore:
                    self._last_seen_id["mtx"] = history[0]["id"]
                    logger.info(f"Startup: MTX SKIP_RESTORE — flat boot, last id={self._last_seen_id['mtx']}")
                else:
                    rec = reconcile_restore(local_units, history, cutoff_ms)
                    mtx_cap = MAX_UNITS_PER_SOURCE["mtx"]
                    for u in rec["to_restore"][:mtx_cap]:
                        u = self._normalize(u, "mtx")
                        logger.info(f"Startup: restoring MTX id={u['id']} dir={u['dir']} "
                                    f"(local-confirmed, no order placed)")
                        self._open_unit(u, source="mtx", notify=False, place_order=False)
                    for unit, worker in rec["to_record_exit"]:
                        self._record_missed_exit(unit, worker)
                    for pid in rec["skipped_phantoms"]:
                        logger.warning(f"Startup: SKIP phantom MTX id={pid} "
                                       f"(Worker-open but bot never filled — not restored)")
                    if rec["dropped_stale"]:
                        logger.info(f"Startup: dropped {len(rec['dropped_stale'])} stale local MTX unit(s)")
                    self._save_mtx_state()   # persist the reconciled set (drops recorded-exit + stale)
                    self._last_seen_id["mtx"] = history[0]["id"]
                    logger.info(f"Startup: MTX restored {len(self._units['mtx'])}, "
                                f"recorded {len(rec['to_record_exit'])} missed-exit, "
                                f"skipped {len(rec['skipped_phantoms'])} phantom")
```
Leave the surrounding `try/except` (`Startup MTX fetch failed`), the `cutoff_ms`/`session_start` computation above it, and the `open_trades` sort intact — `open_trades` is no longer used for restore but `cutoff_ms` still is; if `open_trades` becomes unused, the linter is fine (it's a local var) — remove its construction only if trivial, else leave it.

- [ ] **Step 3: Syntax check**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m py_compile strategy.py && echo OK`
Expected: `OK`.

- [ ] **Step 4: Confirm `_record_trade` signature match**

Run: `sed -n '580,615p' strategy.py` and confirm `_record_missed_exit` passes only declared kwargs. Fix the call if needed. Re-run `py_compile`.

- [ ] **Step 5: Commit**

```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
git add strategy.py
git commit -m "feat(mtx-restore): start() restores from local mtx_state, reconciled vs Worker (kills phantoms)"
```

---

## Task 5: End-to-end verification (no deploy in this task)

**Files:** none (verification)

- [ ] **Step 1: Full unit suite green**

Run: `cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1 && python3 -m unittest test_mtx_restore -v`
Expected: 8 tests PASS.

- [ ] **Step 2: Whole-repo compile**

Run: `python3 -m py_compile strategy.py mtx_restore.py && echo OK`
Expected: `OK`.

- [ ] **Step 3: Simulate the incident (the phantom must NOT restore)**

Run:
```bash
cd /Users/seanchen/Claude_Agent/600_CODE/uni-auto-trader-v1
python3 -c "
import mtx_restore as mr
# local file has NO unit (bot refused the entry); Worker still shows it open
rec = mr.reconcile_restore([], [{'id': 1779673901758, 'status': 'open'}], cutoff_ms=0)
assert rec['to_restore'] == [], rec
assert rec['skipped_phantoms'] == [1779673901758], rec
print('incident sim OK: phantom skipped, not restored')
"
```
Expected: `incident sim OK: phantom skipped, not restored`.

- [ ] **Step 4: Deploy readiness note (controller acts later, with Sean's go)**

Deploy is observe-first + ask-first (real money). When approved: scp `strategy.py` + `mtx_restore.py` to the VPS, `sha256sum` both ends, restart on a weekday while flat. Watch the next restart's startup log: confirm `skipped N phantom` / `restoring MTX … local-confirmed` lines and that `_units['mtx']` matches the broker. `mtx_state.json` will be created on the first real MTX open after deploy (until then, restore boots flat — conservative, correct).

---

## Self-Review

**Spec coverage:** local `mtx_state.json` save/load (Task 1) ✓; persist on real open/close mirroring FVG (Task 3) ✓; restore = local ∩ Worker reconciliation, 4 cases incl. phantom-skip + missed-exit + Worker-missing-conservative + stale-drop (Task 2 pure fn + Task 4 wiring) ✓; error handling missing/corrupt→empty (Task 1) + Worker-fetch-failure keeps existing try/except (Task 4) + MTX_SKIP_RESTORE preserved (Task 4) + cutoff_ms applied (Task 2) ✓; `reconcile_restore` pure & TDD'd (Task 2) ✓; gitignore mtx_state.json (Task 3) ✓; incident simulation (Task 5) ✓.

**Placeholder scan:** none — full code in every code step. The one runtime check ("confirm `_record_trade` signature at :580") is an explicit verify-and-match step with the known signature stated, not a placeholder.

**Type consistency:** `load_mtx_state`/`save_mtx_state`/`reconcile_restore` names + the returned dict keys (`to_restore`/`to_record_exit`/`dropped_stale`/`skipped_phantoms`) are used identically in Task 2 tests and Task 4 wiring. `_save_mtx_state`/`_record_missed_exit` method names consistent. `MTX_STATE_PATH` used in Tasks 3 & 4.

**Known follow-up (out of scope, noted in spec):** the rare crash-window between `_record_missed_exit` and the end-of-restore `_save_mtx_state` could re-record a missed exit on a double-restart; acceptable (rare) — a stricter version would record+save atomically per unit.
