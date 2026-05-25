# Spec: MTX restore-divergence fix — no phantom units on restart

**Date:** 2026-05-25
**Status:** Approved (design), proceeding to implementation plan
**Area:** `strategy.py` MTX startup restore (`start()`), `_open_unit`, `_close_unit`
**Repo:** `asd261-ai/uni-auto-trader-v1`

## Context

On 2026-05-25 a false `DAILY_MAX_LOSS` lock (Bug#1, already fixed by scoping the lock P&L to the
bot's own contract) caused the bot to correctly **refuse** a real MTX short entry (no broker order,
no fill). But the **Worker** still marked that signal `status="open"` in its signal-history KV. On the
09:06 restart, `start()`'s MTX restore trusted Worker `status=="open"` as the sole authority and
restored the signal as a **phantom unit** (entry 43419) that never had a real broker fill. It then
"phantom-closed" for +276, polluting `trades.jsonl`.

**Root cause:** `start()` restore treats Worker `status=="open"` as proof the bot holds the position.
But the bot can **refuse** (DAILY_MAX_LOSS lock) or **skip** (`HALF_SIZE_CODES` alternate-skip) a
signal the Worker still considers open. So *Worker-open ≠ bot-holds-it*. This recurs on every restart
where a Worker-open signal wasn't actually filled by the bot — and `HALF_SIZE_CODES` skipping is
routine, so the phantom is a **recurring** class, not a one-off.

FVG does NOT have this bug: it restores from a local `fvg_state.json` (what the bot actually holds),
not from the Worker. MTX is the outlier ("MTX is restored from Worker KV at startup" — the flawed
assumption).

## Goal / Non-goals

- **Goal:** MTX restore reflects what the **bot actually holds**, not what the Worker thinks.
  No phantom units. **Never lose a real position** (conservative bias: a missed restore is recoverable
  via recon; a phantom or a lost real position is not).
- **Non-goal:** Changing the "book-on-send / don't wait for fill" order architecture (the rarer
  send-failed-but-booked phantom is a separate issue).
- **Non-goal:** Session-close P&L report timing (separate spec, `2026-05-25-session-close-pnl-timing`).
- **Non-goal:** FVG (already correct via `fvg_state.json`).

## Decision (from brainstorming)

**Approach B — persist the bot's actually-opened MTX units locally and gate restore by it.** Give MTX
the same local-state pattern FVG already has, and make the local file authoritative for *existence*,
with the Worker only supplying current exit *levels*.

Rejected: (A) query broker net at restore — the broker position query lags right after login (the
recurring 15:00 recon transient), so it could read 0 and drop a real position (the dangerous
direction). (C) match against `orders.jsonl` fills — fills don't carry the Worker trade id, so id
attribution is heuristic and fragile.

## Design

### Components

**1. `mtx_state.json`** (NEW local file, mirrors `fvg_state.json`)
- Path: `Path(__file__).parent / "mtx_state.json"`.
- Holds the list of MTX units the bot actually opened (the same unit dicts already used in
  `self._units["mtx"]`).
- Atomic write (write `.tmp` then `replace`), exactly like `_save_fvg_state`.
- Helpers `_save_mtx_state()` / `_load_mtx_state()` mirroring `_save_fvg_state()` / `_load_fvg_state()`.

**2. Persist on open** — in `_open_unit`, after the unit is appended to `self._units[source]`
(strategy.py:1313) and alongside the existing FVG persist (`if source == "fvg": self._save_fvg_state()`,
~1323): when `source == "mtx"` and `place_order` is true (a real entry the bot actually sent), call
`self._save_mtx_state()`.
- Refused entries return at the lock gate (line 1287) **before** the append → never persisted. ✓
- HALF_SIZE-skipped signals are absorbed **before** `_open_unit` is called → never persisted. ✓

**3. Persist on close** — in `_close_unit`, after `self._units[source].remove(unit)` (line 1418) and
alongside the existing FVG persist (~1422): when `source == "mtx"`, call `self._save_mtx_state()`.

**4. Restore = local ∩ Worker reconciliation** — rewrite the MTX restore block in `start()`
(strategy.py ~325-332). Source of truth for *existence* = `mtx_state.json`. For each locally-persisted
MTX unit (subject to the existing `cutoff_ms` boot floor), reconcile against the Worker history entry
for that `id`:

| Local has unit? | Worker status for that id | Action |
|---|---|---|
| yes | `open` | **Restore** the unit (`place_order=False`), refresh `stop`/`target`/trail from the Worker entry (current levels). |
| yes | terminal (`profit`/`loss`/`trail`/`reversed`/`session_end`) | The position exited while the bot was down (real exit). **Record the missed exit once** via the normal close/record path — do NOT restore it as open (avoids the [[mtx-restore-trail-bug]] double-count). Then drop it from `mtx_state.json`. |
| yes | not found in Worker history | Conservative: **restore** as open (don't lose a real position); let the running `_sync_worker_state` reconcile on the next poll. |
| no | `open` | **SKIP** — this is the phantom we are killing (Worker-open the bot never filled). ✓ |

The previous behavior (iterate Worker `status=="open"` and restore all) is removed; Worker is consulted
per-local-unit, not iterated as the authority.

### Data flow

`on_fill`/`_open_unit` (real entry) → append to `_units["mtx"]` + `_save_mtx_state()` →
`mtx_state.json`. On restart: `start()` loads `mtx_state.json` → for each unit, look up Worker history
by id → restore/record-exit/skip per the table → `_units["mtx"]` rebuilt to match reality.
`_close_unit` → remove + `_save_mtx_state()`.

### Error handling

- Missing/corrupt `mtx_state.json` → treat as empty (boot flat). Conservative: better to restore
  nothing and let recon flag a real position than to trust the Worker and resurrect a phantom.
- `MTX_SKIP_RESTORE=1` escape hatch preserved (forces flat boot; still advances `last_seen_id`).
- `cutoff_ms` session boot-floor still applies to the local units (don't restore stale prior-session
  units).
- A Worker-history fetch failure must not crash restore: if Worker is unreachable, restore local-open
  units as-is (levels not refreshed) and let `_sync_worker_state` reconcile later — never lose a real
  position over a transient Worker fetch error.

## Testing (TDD)

The restore reconciliation is the high-value unit to test. Factor the per-unit decision into a pure
function, e.g. `reconcile_restore(local_units, worker_history, cutoff_ms) -> (to_restore, to_record_exit, skipped)`,
testable without network or disk:
- **Phantom skip:** Worker-open id NOT in local → in `skipped`, not `to_restore`. (the core fix)
- **Normal restore:** local id + Worker `open` → in `to_restore` with refreshed levels.
- **Missed exit:** local id + Worker terminal status → in `to_record_exit`, not `to_restore` (no
  double-count).
- **Worker-missing:** local id + not in Worker history → in `to_restore` (conservative).
- **Boot floor:** local unit older than `cutoff_ms` → excluded.
- `_save_mtx_state`/`_load_mtx_state` round-trip (write→read equality), atomic-write, missing-file→empty.

Find/confirm the repo's test runner (e.g. `pytest` or stdlib `unittest`; check existing
`test_tick_watchdog.py`) and follow it.

## Verification (end-to-end, observe-first — no paper env)

1. Unit suite green (reconcile + state round-trip).
2. Simulate the incident locally: a `mtx_state.json` WITHOUT a given id + a Worker history WITH that id
   `status="open"` → restore must SKIP it (no phantom).
3. After deploy (scp + sha256 + restart, weekday, flat — observe-first per [[no-paper-env-validate-trader-live]]):
   on a restart with a known half-size-skipped Worker-open signal present, confirm the startup log shows
   it SKIPPED (not restored) and `_units["mtx"]` matches the broker.

## Files touched

- `strategy.py` (modify: `_save_mtx_state`/`_load_mtx_state`, persist in `_open_unit`/`_close_unit`,
  rewrite MTX restore in `start()`; factor `reconcile_restore` pure fn)
- test file (new or extend existing): `reconcile_restore` + state round-trip tests
- `mtx_state.json` is runtime state (gitignore it; do NOT commit live state)
