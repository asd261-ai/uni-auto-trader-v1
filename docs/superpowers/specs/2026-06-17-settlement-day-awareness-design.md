# Settlement-Day Awareness — Design Spec

**Date:** 2026-06-17
**Author:** Bob (with Sean)
**Status:** Design approved; spec under review → writing-plans next.

## Goal

Make MTX **settlement days** non-disruptive for the auto-trader. On the monthly settlement day the front contract cash-settles at 13:30 (day session); the night session (15:00) trades the next month. Two failure modes observed on the 2026-06-17 June settlement must be fixed:

- **#15** — `TickStaleWatchdog` (B / `TICK_STALE_KILL`) **false-kills** in the 13:30–13:45 window: the contract settled at 13:30 so no ticks arrive, but `_get_session` still returns `"day"` (180s kill threshold) → `os._exit` → systemd restart → repeat.
- **#16** — a position **held into the 13:30 settlement** becomes a **local-state phantom**: settlement is not a fill, so `reconcile_restore` can't see it close; the bot keeps the (settled, non-existent) unit and may act on it.

**Success criteria:** on a settlement day the bot (a) does NOT kill-loop, (b) does NOT act on or carry a settled position as a phantom, (c) resumes normal trading on the new contract for the night session. Verified by unit tests + a clean 7/15 July settlement.

## Non-goals (YAGNI)

- **No settlement P&L booking.** The settled position's P&L stays with the broker (authoritative). The bot's internal P&L is already muddied on the shared account; booking the TAIFEX special settlement price is out of scope.
- **No calendar-driven auto-flatten of positions.** Per the 2026-06-17 "don't over-trust auto-detection" lesson, consequential position changes stay tied to the operator's deliberate contract rollover (see Component 3), not a calendar guess.
- **No full holiday calendar.** Pure 3rd-Wednesday detection + an optional manual override date for the rare holiday-shifted settlement. General holiday handling is a separate concern.
- **#17 (contract rollover) is already mitigated** by the `UNITRADE_PRODUCT` override env (deployed 2026-06-17). Not re-solved here.

## Background

- The active contract code rolls one letter per settlement: `MXFF6`(Jun, settled 6/17) → `MXFG6`(Jul, now) → `MXFH6`(Aug) → … See memory `reference_mtx_contract_rollover.md`.
- The operator sets `UNITRADE_PRODUCT` to the correct contract each settlement and restarts in the afternoon break (a `/schedule` reminder fires 2026-07-15 13:50). That rollover restart is the authoritative "settlement happened" signal Component 3 keys off.
- Relevant code: `strategy.py:205 _get_session(dt)` (weekday-aware → `day`/`night`/`break`); `tick_watchdog.py` (`check()` line 95 returns early when `session not in ("day","night")`); `strategy.py:178-181` watchdog thresholds; `strategy.py:430-455` startup restore; `mtx_restore.py` `load_mtx_state`/`save_mtx_state`/`reconcile_restore`; `trader.py:_resolve_product` (sets `config["product"]`).

## Architecture — direction B ("safe automation + operator-driven position")

Three components. The calendar drives only **safe, internal** behaviors (suppress a kill, stop trading). The **consequential** position change is driven by the operator's rollover.

### Component 1 — `settlement_calendar.py` (new pure module)

A dependency-free, fully unit-tested module (matches the repo's `atr_gate.py` / `demote_gate.py` / `tick_watchdog.py` pattern — all time/inputs passed in, no hidden clock).

```python
# settlement_calendar.py
from datetime import date, datetime, time
from typing import Optional

SETTLEMENT_START = time(13, 30)   # day session settles
SETTLEMENT_END   = time(15, 0)    # night session opens

def third_wednesday(year: int, month: int) -> date:
    """The 3rd Wednesday of the month (nominal TAIFEX settlement day)."""
    first = date(year, month, 1)
    first_wed_offset = (2 - first.weekday()) % 7   # weekday(): Mon=0 .. Wed=2
    return date(year, month, 1 + first_wed_offset + 14)

def is_settlement_window(now: datetime, override_date: Optional[date] = None) -> bool:
    """True iff `now` (TW-local) is on the settlement day AND time in [13:30, 15:00).
    Settlement day = override_date if given (holiday shift), else 3rd Wednesday of now's month."""
    settle_day = override_date or third_wednesday(now.year, now.month)
    return now.date() == settle_day and SETTLEMENT_START <= now.time() < SETTLEMENT_END
```

`override_date` is passed **in** (pure); the caller reads the env. No env coupling inside the function → trivially testable.

### Component 2 — `_get_session` returns `"break"` during the settlement window (fixes #15)

`strategy.py`:
- At module load (beside the existing `TICK_STALE_KILL_*` env block ~`:178-181`), parse a new optional env:
  `MTX_SETTLEMENT_OVERRIDE_DATE` (YYYY-MM-DD) → module-level `_SETTLEMENT_OVERRIDE_DATE: Optional[date]` (None if unset/unparseable, with a log warning on parse failure).
- In `_get_session(dt)` (`:205`), **before** the normal day/night/break classification:
  ```python
  if is_settlement_window(dt, _SETTLEMENT_OVERRIDE_DATE):
      return "break"
  ```
  Signature unchanged (many callers), so the override is read from the module global.

**Why this single change fixes #15 and the 13:30–13:45 management gap:**
- `tick_watchdog.check()` line 95 returns early for any non-active session → **`"break"` ⇒ no alert, no kill.** The false-kill stops.
- The trading/management gate (`:501 active = _get_session(...) != "break"`) → **`"break"` ⇒ bot stops opening/managing** → no phantom exit order against the settled position in 13:30–13:45.
- At 15:00 the window closes; `_get_session` returns `"night"` again. Entering an active session re-anchors the watchdog grace clock (`tick_watchdog.py:85-86`), so the night session starts clean.

**Window boundary rationale:** the window is 13:30–15:00 because the operator rolls `UNITRADE_PRODUCT` to the new contract **in the break** (before 15:00). After 15:00 the bot is on the correct contract and the watchdog must be active again (normal night). Known limitation: if the operator forgets to roll by 15:00, night-session protection ends and a kill-loop on the old contract is again possible — that case is covered by the rollover reminder, not by extending the window (extending would wrongly suppress the watchdog on the new contract all night).

### Component 3 — drop old-contract units at the rollover restart (fixes #16)

Persist the active product alongside units, and drop units when the product changed (= the operator rolled = the old units settled).

`mtx_restore.py`:
- `save_mtx_state(path, units, product)` writes `{"product": product, "mtx_units": [...]}` (currently `{"mtx_units": [...]}`).
- `load_mtx_state(path)` returns `(units, stored_product)` (stored_product = None for legacy files without the key).
- New pure helper (testable): `def rolled_over(stored_product, current_product) -> bool: return bool(stored_product) and bool(current_product) and stored_product != current_product`.

`strategy.py` startup restore (`:430-455`):
- After the product is resolved (available as the trader's `config["product"]`; pass it into the restore path), load `(local_units, stored_product)`.
- If `rolled_over(stored_product, current_product)` → **drop all restored units**, log `"Settlement rollover: dropped N unit(s) from old contract {stored} (now {current}) — settled, not restored"`, start flat. Skip `reconcile_restore`.
- Else (same product, or legacy `stored_product is None`) → restore normally via `reconcile_restore` as today (conservative: a missing product key never drops anything).
- Always `save_mtx_state(..., product=current_product)` so the product is recorded going forward.

**Why this is safe (no calendar / no shared-account risk):** the only time `product` changes is when the operator deliberately edits `UNITRADE_PRODUCT` and restarts. That is the authoritative settlement signal. Dropping units is correct because every MTX unit is on the bot's single `config["product"]`, which just became the *previous* (settled) contract. Legacy state (first deploy, no stored product) drops nothing.

## `_get_session` consumer audit (blast radius)

| Consumer | Effect of `"break"` at 13:30 on settlement day | Verdict |
|---|---|---|
| `:462 self._current_session = _get_session(now)` | current-session tracking → feeds watchdog | ✓ intended |
| `:501 active = _get_session(...) != "break"` | trading/management gate → bot stops trading from 13:30 | ✓ intended (contract is settled; nothing to trade) |
| `tick_watchdog.check(session=...)` | line 95 early-return → no kill/alert | ✓ intended (#15 fix) |
| `:780 _check_session_change` → session-summary / open-notify | day→break transition fires the close summary ~13:30 instead of ~13:45 | ✓ correct (trading day ended at settlement) |
| pollloop liveness watchdog (Phase 1) | session-independent; loop still iterates in break | ✓ unaffected |

The plan must grep every `_get_session(` call site to confirm no other consumer assumes `"day"` until 13:45. The audited set above is expected to be complete.

## Testing (stdlib unittest, no third-party deps — runs on VPS system python3)

- `test_settlement_calendar.py`: `third_wednesday` for several months (incl. month starting on Wed/Thu); `is_settlement_window` True only on the settlement day within [13:30,15:00); False 13:29 / 15:00 / other weekdays / non-settlement days; override_date path.
- `test_get_session_settlement.py` (or extend an existing strategy-session test if one exists): `_get_session` returns `"break"` inside the window (with the override global set) and the normal value outside.
- `test_mtx_restore.py` (extend): `rolled_over` truth table (same/changed/empty/legacy-None); `save_mtx_state`+`load_mtx_state` round-trip with product; restore drops units on product change and restores normally on same product / legacy.

## Edge cases & risks

- **Holiday-shifted settlement:** pure 3rd-Wed misses it. Mitigation: `MTX_SETTLEMENT_OVERRIDE_DATE`. Worst case if unset on a shifted day: Component 2 false-negative (watchdog/stop-managing not suppressed → possible #15 recurrence that day) — same severity as today, not worse. Component 3 is unaffected (keyed off product change, not calendar).
- **False-positive settlement window** (e.g., override mis-set, or 3rd-Wed that isn't really settlement): bot treats 13:30–15:00 as break → skips ~1.5h of trading + watchdog idle. No money risk ("少做"), self-limited to that window.
- **Operator forgets to roll by 15:00:** see Component 2 window-boundary note; covered by the rollover reminder.
- **Shared account:** Component 3 keys off the bot's own `config["product"]` change, not account net — immune to Sean's manual trades. Component 2 is account-agnostic.
- **Observe-first deploy:** ship to VPS in a clean break before 7/15, watch one settlement (7/15) confirm clean, before relying on it.

## Files touched

- **New:** `settlement_calendar.py`, `test_settlement_calendar.py`.
- **Modified:** `strategy.py` (env parse + `_get_session` settlement check + restore wiring), `mtx_restore.py` (`save_mtx_state`/`load_mtx_state`/`rolled_over` + restore-drop), plus restore/state tests.
- No `trader.py` change (the `_resolve_product` override is already deployed).

## Rollout

Deploy in a clean afternoon/dawn break before the **2026-07-15** July settlement (sha256 drift-check → scp → `precheck && restart`, observe). The 7/15 settlement is the live acceptance test. No new env required for default behavior (`MTX_SETTLEMENT_OVERRIDE_DATE` optional).
