# Cross-source opposite-direction collision guard + exit-rejection pend cleanup

- **Date:** 2026-06-29
- **Author:** Bob (for Sean)
- **Status:** Approved design → ready for implementation plan
- **Trader:** uni-auto-trader-v1 (real money, account 0239174, product MXFG6). No paper env → observe-first.

## 1. Problem

On 2026-06-29 the trader booked 4 trades with `pnl_pts_real=null` (exit-fill timeouts) and a
permanent ~−221 pt hole in per-trade monthly P&L. Root-caused by trading-ops (read-only):

1. **12:30:28** MTX ④ opened a **short** in MXFG6 (`S`).
2. **12:31:04** FVG opened a **long** in the **same** MXFG6 (`B`).
3. MXFG6 is a **net-position account** → short + long cancel to broker-net 0.
4. When the bot tried to close either leg, the broker rejected with
   `FUF0092:無足夠留倉口數平倉` ("insufficient position to close") and **0 fills**.
5. No `match` event → `on_fill` never consumes the exit pend → after `EXIT_FILL_TIMEOUT_MS`
   (60s) `_flush_due_exit_records` writes the row with `exit_fill=null`, `pnl_pts_real=None`.
6. **Queue-poison cascade:** `on_order_rejected` → `rollback_rejected_entry`
   (`order_reject.py:44-45`) *bails* whenever a same-`bs` exit is pending, so the rejected
   exit pend is **never removed** from `_pending_fills`. The stale exit pends
   (9G295 S, 9G552 B) sit at the FIFO front and mis-consume the next real fills
   (0H650 entry, 0H914 exit) → trades #6–#9 all lose their fills → 4 null rows.

### Authority / impact (verified, not changed by this work)
- `orders-FIFO realized = −464 pts` is **authoritative**; `conservative_day_pnl = min(−464, −243) = −464`,
  so the DAILY_MAX_LOSS breaker fired on the correct number. ✅
- Per-trade monthly (`real_month_pnl_pts`) carries a permanent ~−221 pt hole from the 4 nulls.
- **Latent risk (the real reason to fix):** while netted, the bot tracked two phantom units
  the broker did not hold → strategy logic operating on positions that do not exist.

## 2. Root cause is structural, not manual

Today's FUF0092 was **NOT** Sean's manual interference — the manual MXFH6 orders (a different
contract) never touched MXFG6. The collision was **MTX and FVG strategies holding
opposite-direction positions in the same contract**. This is the
[[project-shared-account-margin-contention]] failure manifesting *between the two bot
strategies*, forced by the net-position account type.

## 3. Scope decision (approved)

**Fix B = B1: block opposite-direction cross-source entries only.** Same-direction
cross-source (e.g. MTX short + FVG short = broker 2 lots short) is left untouched — it nets
cleanly and is not the bug. A net-position account *physically cannot* hold opposite legs in
one contract, so "allow but account correctly" is impossible; blocking is the only option.
First-in holds the contract; the conflicting opposite signal is skip-absorbed.

## 4. Design

### Fix B — cross-source opposite-direction guard

**New pure helper** in `entry_guard.py` (existing home of `entry_past_target`):

```
cross_source_opposite(units: dict, source: str, direction: str) -> bool
```
- Returns True iff **another** source key in `units` holds ≥1 open unit whose `dir` is the
  **opposite** of `direction` (long↔short).
- Pure, no side effects, fail-open on malformed input (returns False).
- Considers any unit currently in `self._units[other]` (filled or pending-fill) — intent is
  enough, because the broker nets at execution.

**Guard wiring** in `strategy.py:_open_unit`, after the past-target guard, before
`_execute_order`, matching the existing skip-and-return idiom:
```
if place_order and CROSS_SOURCE_OPP_MODE != "off" and cross_source_opposite(self._units, source, direction):
    # observe: log WOULD-BLOCK, still place. on: log + skip-absorb (no order, no unit), return.
```
- **Env `CROSS_SOURCE_OPP_MODE`** ∈ `off | observe | on`. Ships **`observe`** (Day-1: log
  "WOULD BLOCK", still place the order so behaviour is unchanged and we can confirm it fires
  only on genuine opposite collisions and never on legitimate trades). Flip to `on` after
  ≥1 clean observe day → actual skip-absorb.
- `on` path: `logger.warning("... cross-source opposite collision — skip ...")`, no order,
  no unit registered, return. No Telegram per-skip (consistent with other guards).

### Fix A — exit-rejection pend cleanup (stop queue-poison)

**New pure helper** in `order_reject.py` (alongside `rollback_rejected_entry`, MATCH + has
`test_order_reject.py`):

```
rollback_rejected_exit(pending_fills, productid, bs, our_product) -> Optional[dict]
```
- Ignore foreign contracts (`productid != our_product` → None).
- Identify the rejected exit **structurally + conservatively** (mirroring the entry version):
  candidate = pending_fills entries with `kind == "exit"` and matching `bs`; act only when
  **exactly one** such candidate exists, else **bail** (leave to broker reconciliation).
- Remove that exit pend from `pending_fills`; **return the pend** (which carries its `pe`)
  so the caller can finalize it. No flush inside the pure helper (keep it side-effect-free
  on the deferred-records list, which lives in strategy state).

**Caller change** in `strategy.py:on_order_rejected` (holds the lock):
1. Try `rollback_rejected_entry` (existing). If it rolled back a unit → done (log as today).
2. Else try `rollback_rejected_exit`. If it returns a pend with a `pe` still in
   `_pending_exit_records`: remove the pe, `finalize_exit(pe["record"], None, dir_)`,
   `_record_trade(**rec)` (write `exit_fill=null` **now**, not after 60s),
   `_save_pending_exit_records()`, and `logger.warning("... exit rejected → pend cleared, null booked ...")`.
3. If neither matched → no-op (today's behaviour).

Net effect: a rejected exit is removed from the FIFO **immediately**, so subsequent real
fills are attributed to the correct units — the cascade that nulled trades #7–#9 cannot form.

- Fix A is a strict-correctness change (worst case = same null row as today, just written
  sooner and without poisoning the queue). Ships **`on`** behind no new env (it only acts on
  genuine reject replies), gated by full unit tests.

## 5. Testing (TDD)

- **`test_entry_guard.py`** (new or extend): opposite cross-source → True; same-direction
  cross-source → False; no other-source position → False; same-source only → False;
  malformed/empty units → False (fail-open).
- **`test_order_reject.py`** (extend, exists): exit-reject with exactly one matching exit pend
  → pend returned + removed; ambiguous (two same-bs exits) → bail/None; entry-reject path
  unchanged (regression); foreign product → None; no matching pend → None.
- **`strategy.py` integration** (logic-level, no broker): simulate the 6/29 sequence
  (MTX short open → FVG long open) → with `CROSS_SOURCE_OPP_MODE=on` the FVG long is
  skip-absorbed; with `observe` it places but logs WOULD-BLOCK. Simulate an exit-reject →
  exit pend cleared, next entry fill attributed correctly (no cascade).
- `py_compile` clean; all existing trader tests still green.

## 6. Deployment

- **Sync first:** local `strategy.py` is DRIFTED behind VPS (VPS has VPS-only patches per
  [[feedback-vps-trader-deploy-scp]]). Before editing: scp VPS→local to base edits on reality.
  `order_reject.py` / `entry_guard.py` / `test_order_reject.py` already MATCH.
- **Deploy:** scp local→VPS + `sha256sum` verify each file. Restart via
  `trader-precheck.sh && systemctl restart uni-trader` — **Sean runs it himself with `!`**
  ([[feedback-trader-service-precheck-sop]], [[feedback-irreversible-ask-first]]).
- **Observe-first:** ship `CROSS_SOURCE_OPP_MODE=observe`; review ≥1 trading day of
  WOULD-BLOCK logs; then ask Sean to flip to `on`.
- Real-money deploy is **ask-first**: show the diff, get explicit GO before any scp.

## 7. Success criteria

1. `cross_source_opposite` + `rollback_rejected_exit` pure, fully unit-tested, py_compile clean.
2. Replaying the 6/29 sequence: under `on`, the FVG long that collided is skip-absorbed (no
   FUF0092, no null rows); under `observe`, logged as WOULD-BLOCK.
3. An exit rejection clears its pend immediately — no stale pend, no cascade null rows on the
   following fills.
4. No regression in entry-rejection rollback or any existing test.
5. orders-FIFO remains the authoritative daily P&L; the breaker keeps reading the correct number.

## 8. Out of scope (named, not done)

- **Backfilling the 4 existing null rows** — FUF0092 orders never matched, so there is no fill
  to recover; the −221 monthly hole is permanent. Not addressed.
- **Replace-triggered cooldown / daily ④ entry-count cap** — the over-trading lever for 6/29;
  orthogonal, separate analysis (loss-cooldown already evaluated NOT-YET).
- **Contract carve-out** (MTX=MXFG6, FVG=MXFH6) — rejected in favour of B1 (carve-out hits
  MXFH6 thin pre-rollover liquidity + changes routing).
- **Fixed strategy priority** (e.g. MTX always wins) — using first-in-holds instead.
