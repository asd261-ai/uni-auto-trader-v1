# Spec: Trader labels a trailing-stop-in-profit exit as "trail", not "loss"

**Date:** 2026-05-26
**Status:** Approved (design), proceeding to implementation plan
**Area:** `strategy.py` — `_check_exit_unit` (local tick-level stop-hit)
**Repo:** `asd261-ai/uni-auto-trader-v1` (LIVE real-money MTX trader)

## Context

`_check_exit_unit(self, unit, price)` (strategy.py ~1416–1429) is the trader's local tick-level exit check. On
a stop hit it **hardcodes `reason="loss"`**:

```python
if unit["dir"] == "long":
    if unit["stop"] and price <= unit["stop"]:
        ... self._close_unit(unit, "loss", exit_price=price)   # line ~1419
elif unit["dir"] == "short":
    if unit["stop"] and price >= unit["stop"]:
        ... self._close_unit(unit, "loss", exit_price=price)   # line ~1426
```

A unit's `stop` is **trailed in place** (strategy.py ~1302: `unit["stop"] = new_stop`) — there is no stored
original stop. So when a stop has been trailed onto the **profit side of entry** and is then hit, the exit is a
**trailing-take-profit (移動停利)**, yet it is logged and recorded as `loss` (停損).

**Concrete case (2026-05-26 09:06):** short, entry 44219, stop trailed 44349 → 44314 → **44151** (below entry =
profit side); price 44152 ≥ 44151 → "Stop hit" → exit 44152, **pnl +67**, but `reason=loss`. It should be
`trail`.

The Worker-driven exit path already distinguishes correctly — `_sync_worker_state` closes with `reason="trail"`
when the Worker reports `status='trail'` (strategy.py ~1266–1271), matching the Worker's own
`isTrailing ? 'trail' : 'loss'` (worker/index.js ~1225). Only the trader's **local** stop-hit path is wrong.

**Impact:** label/reporting only — `pnl_pts` is computed from `exit_price` and is correct (+67). But the wrong
label corrupts: the Telegram exit message (`"停損出場"` instead of `"移動停利"`), the `reason` field in
`trades.jsonl`, and any win/loss attribution that keys on `reason` rather than pnl sign. Real-fill P&L
(orders.jsonl FIFO) and the DAILY_MAX_LOSS lock are **unaffected** (they read real fills, not `reason`).

## Goal / Non-goals

- **Goal:** on a local stop hit, label the exit `reason="trail"` when the stop has been trailed onto entry's
  profit side, else `reason="loss"`. Semantics match the Worker's `isTrailing`.
- **Non-goals:** changing exit price, pnl, order placement, or any P&L/locking logic; the Worker-driven path
  (already correct); FVG exits (Worker/producer-driven). No behavior change beyond the label.

## Decision (from brainstorming)

Determine trail-vs-loss from **the stop's position relative to entry at exit time** (equivalent to the exit pnl
sign, since at a stop hit `exit_price ≈ stop`):
- long: stop **>** entry → `trail`; stop **≤** entry → `loss`
- short: stop **<** entry → `trail`; stop **≥** entry → `loss`

`stop == entry` (breakeven) → `loss` (not strictly profit). No new unit state needed (original stop is not
retained, and is not required for this rule).

## Design

### Pure module (testable, mirrors `mtx_restore.py` / `session_timing.py`)
New file `exit_reason.py` (pure, stdlib-only, no SDK deps):

```python
def stop_hit_reason(direction: str, stop: float, entry: float) -> str:
    """Label a stop-hit exit. A hit on a stop that has been trailed onto the
    profit side of entry is a trailing-take-profit ('trail'); otherwise it is a
    real stop-loss ('loss'). Breakeven (stop == entry) counts as 'loss'.
    Mirrors the Worker's isTrailing semantics (worker/index.js)."""
    if direction == "long":
        return "trail" if stop > entry else "loss"
    return "trail" if stop < entry else "loss"
```

### Wiring (strategy.py)
- Import near the other pure-module imports: `from exit_reason import stop_hit_reason`.
- In `_check_exit_unit`, replace the two hardcoded `"loss"` stop-hit closes:

```python
# long stop hit (~1417-1419):
        if unit["stop"] and price <= unit["stop"]:
            logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
            self._close_unit(unit, stop_hit_reason("long", unit["stop"], unit["entry"]), exit_price=price)

# short stop hit (~1424-1426):
        if unit["stop"] and price >= unit["stop"]:
            logger.info(f"Stop hit | source={source} id={unit['id']} price={price} stop={unit['stop']}")
            self._close_unit(unit, stop_hit_reason("short", unit["stop"], unit["entry"]), exit_price=price)
```

The Target-hit branches (`profit`) and the Worker-driven path are unchanged. `_close_unit` already renders
`reason="trail"` correctly: `EXIT_EMOJI["trail"]="🔒"` (line 125) and `reason_zh["trail"]="移動停利"`
(line ~1495).

## Error handling / edge cases

- `unit["stop"]` is truthy by the time the branch runs (guarded by `if unit["stop"] and ...`), and `entry` is
  always set at open → `stop_hit_reason` receives valid numbers.
- Breakeven `stop == entry` → `loss` (documented).
- Trailing not yet activated (stop still on the protective side of entry) → correctly `loss`.
- No change to FVG (`source=='fvg'`) exits — they don't take the MTX trailing path; the rule is still correct
  if reached (trail only when stop is on the profit side), but FVG stops are producer-driven.

## Testing (TDD)

`exit_reason.py` is pure → `test_exit_reason.py` (stdlib unittest). Run: `python3 -m unittest test_exit_reason -v`.

Cases:
- long, stop > entry → `"trail"` (trailed into profit, e.g. entry 44000 stop 44050).
- long, stop < entry → `"loss"` (original protective stop, e.g. entry 44000 stop 43950).
- long, stop == entry → `"loss"` (breakeven).
- short, stop < entry → `"trail"` (the 09:06 case: entry 44219 stop 44151).
- short, stop > entry → `"loss"` (original protective stop, e.g. entry 44219 stop 44349).
- short, stop == entry → `"loss"` (breakeven).

Integration (the `_check_exit_unit` wiring) verified by code review — the change is a pure substitution of the
`reason` argument; exit_price/pnl/order paths are untouched.

## Verification (end-to-end)

1. `python3 -m unittest test_exit_reason -v` → green.
2. `python3 -m py_compile strategy.py exit_reason.py` → OK.
3. Deploy observe-first (ask-first; real money, no paper env): scp `strategy.py` + `exit_reason.py` → sha256
   verify both ends → restart in a flat/break window → confirm boot clean + modules import.
4. First trailing-stop-hit exit after deploy logs `reason=trail` and the Telegram shows `🔒 移動停利`
   (not `❌ 停損出場`); an original-stop loss still logs `reason=loss`.

## Files touched

- `exit_reason.py` (new, pure) + `test_exit_reason.py` (new)
- `strategy.py` (modify: import; two `reason` substitutions in `_check_exit_unit`)
