# Trader fd-leak self-heal + phantom-pnl rollback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the trader self-heal from runtime feed-death (fd-leak blindness) and stop recording phantom P&L on broker-rejected orders.

**Architecture:** Two independent, co-shipped fixes. Part 1 extends the existing pure `TickStaleWatchdog` with a kill-tier (escalate to `os._exit(1)` → systemd restart → OS reclaims leaked fds) plus systemd guard-rails. Part 2 adds a pure `order_reject` module that FIFO-rolls-back the optimistic unit when a broker reply is a rejection, wired through `trader._on_reply`. Both follow the codebase's "pure logic in a tested module, thin wrapper on the class" convention (cf. `mtx_restore.py`, `session_timing.py`).

**Tech Stack:** Python 3 stdlib `unittest` (system python3, no deps — `python3 -m unittest <mod> -v`), systemd unit file, deploy via scp (`feedback-vps-trader-deploy-scp`).

**Spec:** `docs/superpowers/specs/2026-06-01-trader-fd-leak-and-phantom-pnl-fixes-design.md`

---

## File Structure

- `tick_watchdog.py` — MODIFY: add kill-tier (constructor params + `check()` escalation). Stays pure.
- `test_tick_watchdog.py` — MODIFY: add kill-tier tests.
- `strategy.py` — MODIFY: pass `uptime` + Phase-A `on_kill` callback into `_tick_wd.check()`; add `self._proc_start_ts`; add thin `on_order_rejected()` wrapper.
- `order_reject.py` — CREATE: pure `is_reject_status()` + `rollback_rejected_entry()`.
- `test_order_reject.py` — CREATE: pure tests for the rollback logic.
- `trader.py` — MODIFY: `_on_reply` classifies rejections and routes to `strategy.on_order_rejected`.
- `/etc/systemd/system/uni-trader.service` — MODIFY (on VPS): `LimitNOFILE` + `StartLimit*`.

---

# PART 1 — fd-leak runtime self-heal

### Task 1: tick_watchdog kill-tier (pure)

**Files:**
- Modify: `tick_watchdog.py`
- Test: `test_tick_watchdog.py`

- [ ] **Step 1: Write failing tests** — append to `test_tick_watchdog.py`:

```python
class KillTier(unittest.TestCase):
    def _wd(self):
        return TickStaleWatchdog(
            day_threshold=DAY, night_threshold=NIGHT, check_interval=IVAL,
            kill_day_threshold=180.0, kill_night_threshold=600.0, kill_grace=120.0,
        )

    def test_kill_fires_when_stale_beyond_kill_threshold_and_past_grace(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        # age 200s > kill 180s, uptime 500s > grace 120s, day session
        wd.check(1200, "day", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        self.assertEqual(len(kills), 1)
        self.assertIn("escalating", kills[0])

    def test_kill_suppressed_within_grace(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        wd.check(1200, "day", False, lambda m: None, uptime=60.0, on_kill=kills.append)
        self.assertEqual(kills, [])

    def test_kill_not_fired_below_kill_threshold(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        # age 100s < kill 180s (alert fires at 90s but kill does not)
        wd.check(1100, "day", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        self.assertEqual(kills, [])

    def test_kill_skipped_on_break_and_weekend(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        wd.check(1300, "break", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        wd.check(1300, "day", True, lambda m: None, uptime=500.0, on_kill=kills.append)
        self.assertEqual(kills, [])

    def test_kill_one_shot_until_recovery(self):
        wd = self._wd()
        kills = []
        wd.record_tick(1000)
        wd.check(1200, "day", False, lambda m: None, uptime=500.0, on_kill=kills.append)
        wd.check(1240, "day", False, lambda m: None, uptime=540.0, on_kill=kills.append)
        self.assertEqual(len(kills), 1)            # latched, not re-fired
        wd.record_tick(1240)                       # feed recovers
        wd.check(1241, "day", False, lambda m: None, uptime=541.0, on_kill=kills.append)
        wd.record_tick(1241)
        wd.check(1500, "day", False, lambda m: None, uptime=800.0, on_kill=kills.append)
        # stale again after recovery → kill may fire again
        self.assertEqual(len(kills), 2)

    def test_kill_noop_when_callback_or_uptime_absent(self):
        wd = self._wd()
        wd.record_tick(1000)
        wd.check(1200, "day", False, lambda m: None)                 # no on_kill / uptime
        wd.check(1200, "day", False, lambda m: None, uptime=500.0)   # no on_kill
        # no exception, nothing to assert beyond "did not raise"

    def test_kill_invalid_config_rejected(self):
        for kw in ({"kill_day_threshold": 0}, {"kill_night_threshold": -1}, {"kill_grace": 0}):
            with self.assertRaises(ValueError):
                TickStaleWatchdog(**kw)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_tick_watchdog.KillTier -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'kill_day_threshold'`.

- [ ] **Step 3: Implement kill-tier in `tick_watchdog.py`**

In `__init__`, extend the signature and validation/state. Replace the constructor head:

```python
    def __init__(
        self,
        *,
        day_threshold: float = 90.0,
        night_threshold: float = 300.0,
        check_interval: float = 30.0,
        kill_day_threshold: float = 180.0,    # escalate (os._exit) past this in day session
        kill_night_threshold: float = 600.0,  # escalate past this in night session
        kill_grace: float = 180.0,            # min process uptime before kill is eligible
    ) -> None:
        if day_threshold <= 0 or night_threshold <= 0 or check_interval <= 0:
            raise ValueError("thresholds and interval must be positive")
        if kill_day_threshold <= 0 or kill_night_threshold <= 0 or kill_grace <= 0:
            raise ValueError("kill thresholds and grace must be positive")
        self.day_threshold = float(day_threshold)
        self.night_threshold = float(night_threshold)
        self.check_interval = float(check_interval)
        self.kill_day_threshold = float(kill_day_threshold)
        self.kill_night_threshold = float(kill_night_threshold)
        self.kill_grace = float(kill_grace)
        self._last_tick_ts = 0.0
        self._active_session_since = 0.0
        self._session: Optional[str] = None
        self._last_check = 0.0
        self._alert_sent = False
        self._kill_fired = False
```

Extend `check()` signature and add kill evaluation. Replace the `check()` method's signature and final block:

```python
    def check(
        self,
        now: float,
        session: str,
        is_weekend: bool,
        notify: Callable[[str], None],
        uptime: Optional[float] = None,
        on_kill: Optional[Callable[[str], None]] = None,
    ) -> None:
        if session in ACTIVE_SESSIONS and self._session not in ACTIVE_SESSIONS:
            self._active_session_since = now
        self._session = session

        if now - self._last_check < self.check_interval:
            return
        self._last_check = now

        if session not in ACTIVE_SESSIONS or is_weekend:
            return

        threshold = self.day_threshold if session == "day" else self.night_threshold
        ref = max(self._last_tick_ts, self._active_session_since)
        age = now - ref

        if age > threshold:
            if not self._alert_sent:
                notify(
                    f"⚠️ TICK FEED STALE — no dquote tick for {age:.0f}s "
                    f"(session={session}, threshold={threshold:.0f}s). "
                    f"Trader alive but blind to price; exits won't fire. Check feed / restart."
                )
                self._alert_sent = True
        else:
            if self._alert_sent:
                notify(f"✅ Tick feed recovered (last tick {age:.0f}s ago).")
                self._alert_sent = False
            self._kill_fired = False  # feed healthy → re-arm kill for any future outage

        # kill-tier: escalate a sustained outage to a process exit so systemd restarts
        # and the OS reclaims leaked fds. Gated by the same active-session check above,
        # plus a longer threshold and a process-uptime grace (anti self-kill-loop).
        if on_kill is not None and uptime is not None and uptime > self.kill_grace:
            kill_threshold = self.kill_day_threshold if session == "day" else self.kill_night_threshold
            if age > kill_threshold and not self._kill_fired:
                on_kill(
                    f"TICK FEED STALE {age:.0f}s > kill {kill_threshold:.0f}s "
                    f"(session={session}, uptime={uptime:.0f}s) — escalating to process exit "
                    f"for systemd restart (fd reclaim)."
                )
                self._kill_fired = True
```

- [ ] **Step 4: Run to verify pass (and no regression)**

Run: `python3 -m unittest test_tick_watchdog -v`
Expected: PASS — all existing tests + new `KillTier` tests green.

- [ ] **Step 5: Commit**

```bash
git add tick_watchdog.py test_tick_watchdog.py
git commit -m "feat(tick-wd): add kill-tier escalation (pure, tested)"
```

---

### Task 2: wire kill-tier into strategy (Phase A — log-only would-fire)

**Files:**
- Modify: `strategy.py` (constructor ~line 228 area; `_tick_wd` construction ~line 274; `_poll_loop` call ~line 576)

- [ ] **Step 1: Add process-start stamp.** In `MTXStrategy.__init__`, near `self._fvg_boot_ts_ms = int(time.time() * 1000)` (line 228), add:

```python
        self._proc_start_ts: float = time.time()  # wall-clock boot, for tick-wd kill grace
```

- [ ] **Step 2: Add kill env knobs near the existing TICK_STALE_* constants** (after line 148):

```python
TICK_STALE_KILL_DAY_SEC   = int(os.getenv("TICK_STALE_KILL_DAY_SEC", "180"))
TICK_STALE_KILL_NIGHT_SEC = int(os.getenv("TICK_STALE_KILL_NIGHT_SEC", "600"))
TICK_STALE_KILL_GRACE_SEC = int(os.getenv("TICK_STALE_KILL_GRACE_SEC", "180"))
TICK_STALE_KILL          = os.getenv("TICK_STALE_KILL", "off").lower() == "on"  # Phase B arms os._exit
```

- [ ] **Step 3: Pass kill thresholds into the watchdog constructor** (line ~274). Replace:

```python
        self._tick_wd = TickStaleWatchdog(
            day_threshold=TICK_STALE_DAY_SEC,
            night_threshold=TICK_STALE_NIGHT_SEC,
            kill_day_threshold=TICK_STALE_KILL_DAY_SEC,
            kill_night_threshold=TICK_STALE_KILL_NIGHT_SEC,
            kill_grace=TICK_STALE_KILL_GRACE_SEC,
        )
```

- [ ] **Step 4: Add the kill callback method.** Add a method on `MTXStrategy` (near `_safe_health_notify`):

```python
    def _tick_wd_kill(self, msg: str) -> None:
        # Phase A (TICK_STALE_KILL off): observe only — log the would-fire, do NOT exit.
        # Phase B (on): alert then os._exit(1) so systemd restarts and the OS reclaims fds.
        if not TICK_STALE_KILL:
            logger.error(f"[tick-wd KILL would-fire] {msg}")
            return
        logger.error(f"[tick-wd KILL] {msg}")
        try:
            self._safe_health_notify(f"🔪 Trader self-restart: {msg}")
        except Exception:
            pass
        import os as _os
        _os._exit(1)
```

- [ ] **Step 5: Pass `uptime` + `on_kill` into the `check()` call** (line ~576). Replace the call:

```python
                self._tick_wd.check(
                    time.time(), self._current_session,
                    datetime.now(TZ_TW).weekday() >= 5,
                    lambda m: logger.warning(f"[tick-wd OBSERVE] {m}"),  # PHASE 2: -> self._safe_health_notify
                    uptime=time.time() - self._proc_start_ts,
                    on_kill=self._tick_wd_kill,
                )
```

- [ ] **Step 6: Syntax + import check (no live broker needed)**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add strategy.py
git commit -m "feat(strategy): wire tick-wd kill-tier (Phase A observe, TICK_STALE_KILL gates os._exit)"
```

---

### Task 3: systemd guard-rails (VPS ops — ask-first, no auto-apply)

**Files:**
- Modify (on VPS): `/etc/systemd/system/uni-trader.service`

> This task is applied on the VPS during deploy (Task 6), NOT in the repo. Documented here for completeness. It is config-only and reversible.

- [ ] **Step 1:** In the `[Service]` section add `LimitNOFILE=65536`.
- [ ] **Step 2:** In the `[Unit]` (or `[Service]` per systemd version) add `StartLimitIntervalSec=600` and `StartLimitBurst=6`.
- [ ] **Step 3:** `sudo systemctl daemon-reload` (does NOT restart the running process).
- [ ] **Step 4:** Verify: `systemctl show uni-trader -p LimitNOFILE,StartLimitIntervalUSec,StartLimitBurst`. Expected `LimitNOFILE=65536`, burst=6.

---

# PART 2 — phantom-pnl-on-rejection rollback

### Task 4: pure order-reject module

**Files:**
- Create: `order_reject.py`
- Test: `test_order_reject.py`

- [ ] **Step 1: Write the failing tests** — create `test_order_reject.py`:

```python
"""Tests for order_reject. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_order_reject -v
"""
import unittest

import order_reject as orj


def _unit(source="mtx", dir_="short", id_=111):
    return {"source": source, "id": id_, "dir": dir_, "entry": 46137, "stop": 46290}


class IsRejectStatus(unittest.TestCase):
    def test_fuf_codes_are_rejects(self):
        self.assertTrue(orj.is_reject_status("FUF1239:同ID客戶未沖銷部位及委託保證金超過使用額度"))
        self.assertTrue(orj.is_reject_status("FUF0092:無足夠留倉口數平倉"))

    def test_success_and_fill_are_not_rejects(self):
        self.assertFalse(orj.is_reject_status("委託成功"))
        self.assertFalse(orj.is_reject_status("完全成交"))
        self.assertFalse(orj.is_reject_status(""))
        self.assertFalse(orj.is_reject_status(None))


class RollbackRejectedEntry(unittest.TestCase):
    def test_rejected_entry_removes_unit_and_pops_pending(self):
        unit = _unit()
        units = {"mtx": [unit]}
        pending = [{"kind": "entry", "bs": "S", "unit": unit}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6")
        self.assertIs(removed, unit)
        self.assertEqual(units["mtx"], [])
        self.assertEqual(pending, [])

    def test_foreign_product_is_noop(self):
        unit = _unit()
        units = {"mtx": [unit]}
        pending = [{"kind": "entry", "bs": "S", "unit": unit}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFG6", "S", "MXFF6")
        self.assertIsNone(removed)
        self.assertEqual(units["mtx"], [unit])
        self.assertEqual(len(pending), 1)

    def test_bs_mismatch_leaves_queue_intact(self):
        unit = _unit(dir_="long")
        units = {"mtx": [unit]}
        pending = [{"kind": "entry", "bs": "B", "unit": unit}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6")
        self.assertIsNone(removed)
        self.assertEqual(units["mtx"], [unit])

    def test_exit_rejection_does_not_remove_unit(self):
        unit = _unit()
        units = {"mtx": [unit]}
        pending = [{"kind": "exit", "bs": "B"}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "B", "MXFF6")
        self.assertIsNone(removed)
        self.assertEqual(units["mtx"], [unit])
        self.assertEqual(len(pending), 1)

    def test_empty_pending_is_noop(self):
        self.assertIsNone(orj.rollback_rejected_entry([], {"mtx": []}, "MXFF6", "S", "MXFF6"))

    def test_fifo_pops_front_entry_only(self):
        u1, u2 = _unit(id_=1), _unit(id_=2)
        units = {"mtx": [u1, u2]}
        pending = [{"kind": "entry", "bs": "S", "unit": u1},
                   {"kind": "entry", "bs": "S", "unit": u2}]
        removed = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6")
        self.assertIs(removed, u1)
        self.assertEqual(units["mtx"], [u2])
        self.assertEqual(len(pending), 1)
        self.assertIs(pending[0]["unit"], u2)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_order_reject -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'order_reject'`.

- [ ] **Step 3: Create `order_reject.py`**

```python
"""Pure helpers for handling broker order rejections.

When the broker rejects an order (e.g. FUF1239 margin, FUF0092 no-position), the SDK
delivers a `reply` with that status but NO `match` event — so the strategy's optimistic
unit (appended at placement in _open_unit) never gets a fill and would otherwise linger
as a phantom, later recording phantom P&L on a phantom close. These helpers let the
strategy FIFO-roll-back that unit. Pure + fully unit-tested; the strategy holds the lock.
"""
from __future__ import annotations

from typing import Optional


def is_reject_status(status: Optional[str]) -> bool:
    """True if a broker reply status means 'rejected, no fill'. Reject codes start with
    'FUF' (FUF1239 margin-exceeded, FUF0092 no-position-to-close). '委託成功' (accepted)
    and '完全成交' (filled) are NOT rejections."""
    return bool(status) and status.strip().startswith("FUF")


def rollback_rejected_entry(pending_fills: list, units: dict,
                            productid: str, bs: str, our_product: str) -> Optional[dict]:
    """Undo an optimistically-recorded entry whose broker order was rejected.

    FIFO-matches the rejection to the FRONT pending fill (mirrors on_fill's discipline):
    only acts when it is an ENTRY for our product and matching bs. Mutates `pending_fills`
    (pops the entry) and `units` (removes the unit). Returns the removed unit, or None on
    a safe no-op (foreign product, bs/kind mismatch, empty queue). Caller holds the lock.
    """
    if productid != our_product:
        return None
    if not pending_fills or pending_fills[0]["bs"] != bs:
        return None
    if pending_fills[0].get("kind") != "entry":
        return None
    pend = pending_fills.pop(0)
    unit = pend["unit"]
    src_units = units.get(unit["source"], [])
    if unit in src_units:
        src_units.remove(unit)
    return unit
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest test_order_reject -v`
Expected: PASS — all tests green.

- [ ] **Step 5: Commit**

```bash
git add order_reject.py test_order_reject.py
git commit -m "feat(order-reject): pure FIFO rollback for broker-rejected entries (tested)"
```

---

### Task 5: wire rollback into strategy + trader

**Files:**
- Modify: `strategy.py` (add `on_order_rejected` method; import)
- Modify: `trader.py` (`_on_reply` ~line 108; import)

- [ ] **Step 1: Add the strategy wrapper.** In `strategy.py`, add the import near the other local-module imports (e.g. beside `from tick_watchdog import TickStaleWatchdog`):

```python
import order_reject
```

Add the method on `MTXStrategy` (near `on_fill`):

```python
    def on_order_rejected(self, productid: str, bs: str, orderstatus: str):
        """Called from trader._on_reply (broker thread) when a reply is a rejection.
        Roll back the optimistic unit so no phantom unit / phantom P&L lingers."""
        with self._lock:
            unit = order_reject.rollback_rejected_entry(
                self._pending_fills, self._units, productid, bs,
                self.trader.config.get("product"),
            )
        if unit:
            logger.warning(
                f"[order-rejected] source={unit['source']} dir={unit['dir']} "
                f"id={unit['id']} status={orderstatus} → unit rolled back (no fill, no P&L)"
            )
```

- [ ] **Step 2: Route rejections in `trader.py`.** Add the import near the top (beside the other local imports):

```python
from order_reject import is_reject_status
```

Replace `_on_reply` (line 108-111):

```python
    def _on_reply(self, reply):
        logger.info(f"Reply | {reply.productid} {reply.bs} status={reply.orderstatus} orderno={reply.orderno}")
        order_log.log_event("reply", productid=reply.productid, bs=reply.bs,
                            orderno=reply.orderno, orderstatus=reply.orderstatus)
        if self.strategy and is_reject_status(reply.orderstatus):
            try:
                self.strategy.on_order_rejected(reply.productid, reply.bs, reply.orderstatus)
            except Exception as e:
                logger.debug(f"on_order_rejected error (non-fatal): {e}")
```

- [ ] **Step 3: Syntax check both files**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); ast.parse(open('trader.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Full pure-test sweep (regression)**

Run: `python3 -m unittest test_tick_watchdog test_order_reject test_mtx_restore -v`
Expected: PASS — all green.

- [ ] **Step 5: Commit**

```bash
git add strategy.py trader.py
git commit -m "feat: roll back broker-rejected entries to kill phantom P&L"
```

---

# PART 3 — deploy (observe-first, ask-first)

### Task 6: deploy to VPS

> Production real-money change. Do NOT run unattended — confirm with Sean at each gate.

- [ ] **Step 1:** Confirm all pure tests green locally (Task 5 Step 4).
- [ ] **Step 2:** scp changed files to VPS (`feedback-vps-trader-deploy-scp`): `tick_watchdog.py`, `strategy.py`, `order_reject.py`, `trader.py`. sha256sum both ends to confirm no drift.
- [ ] **Step 3:** Apply systemd guard-rails (Task 3) on the VPS; `daemon-reload`.
- [ ] **Step 4:** Run pure tests ON the VPS copy: `python3 -m unittest test_tick_watchdog test_order_reject -v`.
- [ ] **Step 5:** Restart via SOP: `000_Agent/scripts/trader-precheck.sh && sudo systemctl restart uni-trader` (ask-first). Confirm clean boot (login, MXFF6 subscribe, flat, fd low).
- [ ] **Step 6:** Observe. Part 1 stays Phase A (`TICK_STALE_KILL` unset → would-fire logs only) through one weekend cycle; confirm zero false would-fire in maintenance windows. Part 2 is live immediately — on the next broker rejection, confirm `[order-rejected] … rolled back` and that internal day/month P&L matches real fills.
- [ ] **Step 7:** Phase B (later): set `TICK_STALE_KILL=on` in `.env`, restart, to arm `os._exit`.

---

## Self-Review

**Spec coverage:** Part 1 ① LimitNOFILE → Task 3/6. ② kill-tier → Task 1+2. ③ StartLimit → Task 3/6. ④ TDD injected-callback → Task 1 (on_kill param). ⑤ two-phase rollout → Task 2 (`TICK_STALE_KILL`) + Task 6. Part 2 detect → Task 5 (`is_reject_status` in `_on_reply`). rollback/FIFO/expected-auto-correct → Task 4 (`rollback_rejected_entry`; expected derives from `_units` via `_expected_net_position`, so removing the unit auto-corrects — no separate counter). edge: foreign product / bs / empty / FIFO / exit-rejection → Task 4 tests. ✔ all covered.

**Placeholder scan:** none — every code/test step has complete code; every run step has an exact command + expected output.

**Type consistency:** `is_reject_status` / `rollback_rejected_entry` signatures match between `order_reject.py` (Task 4), the strategy wrapper and trader import (Task 5). `check()` new kwargs `uptime` / `on_kill` match between `tick_watchdog.py` (Task 1) and the strategy call site (Task 2). `_proc_start_ts` defined (Task 2 Step 1) before use (Task 2 Step 5). Unit dict keys used in rollback (`source`, `dir`, `id`) match `_open_unit`'s unit shape; pending entry key `unit` matches `on_fill`'s `pend["unit"]`.
