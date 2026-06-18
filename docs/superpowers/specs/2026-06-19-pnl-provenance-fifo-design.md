# pnl_calc realized-P&L via order-provenance FIFO

**Date:** 2026-06-19
**Status:** Design approved (Sean), pending implementation plan
**Touches:** `pnl_calc.py` (live trader — feeds heartbeat `realPnl` + DAILY_MAX_LOSS circuit breaker)

## Problem

`pnl_calc.summarize_real_pnl()` (deployed 2026-06-19 03:27, commit `660f89f`) computes the
trading-day realized P&L by summing each trade's own `pnl_pts_real` from `trades.jsonl`,
excluding rows where the field is `None`. This fixed the earlier whole-history orders-FIFO
bugs (stale-leg mis-pairing → the +2176/+322 phantom; base-`MXF` filter cross-pairing the
shared account) but introduced a new gap:

- When `on_fill` fails to stamp `exit_fill` within its timeout, `pnl_pts_real` is `None` and
  the trade is **silently dropped** from the day total. On 2026-06-18 this under-reported by
  3 trades: real total **+170.0 pts** but `summarize_real_pnl` returned **+167.0**.

The circuit breaker (`DAILY_MAX_LOSS_PTS`) reads this value, so an under-report is a
correctness risk for loss-lock behavior, not just display.

### Why the orders-FIFO can now be made authoritative

The 2026-06-18 canonical inversion assumed the orders-FIFO was irredeemably contaminated by
"shared same-month manual trades." Sean clarified (2026-06-19): **the bot only ever trades
its own contract (currently MXFG6); Sean only trades manually on a *different* month
(currently MXFH6).** Both roll to the next month after each settlement.

Validated against live `orders.jsonl` for trading-day 2026-06-18 (08:45 window):

| | count | product |
|---|---|---|
| `sent` events | 40 | MXFG6 |
| distinct bot ordernos (sent-backed) | 40 | MXFG6 |
| total match ordernos | 48 | — |
| **bot match fills** (sent-backed) | **38** | **MXFG6** → 19 round-trips → **+170.0 pts** |
| **orphan match fills** (no sent = manual) | **10** | **MXFH6** (Sean's manual) |

So once non-bot fills are removed, a per-product FIFO over the trading-day window is both
**uncontaminated** and **complete** (no `None` gaps) — strictly better than the
`pnl_pts_real` sum.

## Approach

Replace the day realized-P&L source in `pnl_calc` with an **order-provenance FIFO** over
`orders.jsonl`, keeping `summarize_real_pnl` as a parallel cross-check.

### Data flow

1. **Provenance filter** — parse `orders.jsonl`; reconstruct bot order identity by linking
   `sent → reply(orderno) → match`. A `sent` event carries no `orderno`; the `orderno`
   comes from the first `reply` within ~3 s that shares `productid` + `bs`. The set of such
   ordernos = **bot ordernos**. Any `match` whose `orderno` is *not* in that set is a
   manual / non-bot fill (it has no originating `sent`) and is **excluded**.
2. **Trading-day window filter** — keep only bot `match` fills whose timestamp is within the
   current 08:45-TW trading-day window (`[08:45 D, 08:45 D+1)`). This is the existing
   boundary; it floors out stale legs from prior days (immunises against the +2176
   regression).
3. **Per-product FIFO** — group the surviving bot fills by **exact** `productid` and FIFO
   each product independently (reuse the existing `_fifo`), then sum realized points across
   products. Per-product grouping means a settlement-day window spanning the old and new
   contract pairs each within its own contract — never cross-pairs MXFG6 against MXFH6.
4. **Output** — `real_trading_day_pnl_pts` = Σ closed round-trip points. This feeds the
   heartbeat payload and the circuit breaker. Open (unrealized) positions are **not** counted
   in realized; open state continues to come from `mtx_state.json` via `_real_open()`.

### Safety

- **Authoritative value:** the provenance FIFO is the circuit-breaker input.
- **Parallel cross-check:** keep computing `summarize_real_pnl` (`pnl_pts_real` sum). If the
  two diverge by more than a tolerance that accounts for the known `null`-fill count, log a
  divergence **warning** — it does not block or alter the breaker. (A persistent divergence
  with zero nulls would signal a provenance-linkage or data problem worth investigating.)
- **Fail-open:** if `orders.jsonl` is missing/unparseable or provenance cannot be
  reconstructed, the day P&L returns `None`. The breaker treats `None` as "no data" and does
  **not** lock — same as today's exception path. The code never fabricates a number.
- **Additive:** order-placement and signal-count paths are untouched. Restart-safe:
  `orders.jsonl` is persistent.

### Caching

Unchanged: 30 s TTL keyed on the 08:45 trading-day window, so a stale pre-boundary value is
never served right after the daily reset.

## Components

- `_bot_ordernos(rows) -> set[str]` — pure; reconstruct sent-backed ordernos from a list of
  parsed order rows.
- `_bot_fills_in_window(rows, window) -> dict[productid, list[(ts, bs, price)]]` — pure;
  provenance + window filter, grouped by exact product.
- `realized_day_pts(rows, window) -> float | None` — pure; per-product FIFO sum. `None` on
  unreconstructable input.
- `_compute()` wires the file read + window + the cross-check/divergence log; existing
  `heartbeat_fields()` cache wrapper unchanged.

Pure functions take parsed rows so they stay I/O-free and unit-testable, matching the
existing `summarize_real_pnl` / `reconcile` style.

## Testing (TDD)

Extend `test_pnl_calc.py`:

1. Bot-only round-trips → correct per-product FIFO total.
2. Stream containing manual fills (match ordernos with **no** `sent`) → excluded; total
   unaffected.
3. Settlement-day window with bot fills on **two** products → each FIFO'd independently, no
   cross-pairing.
4. A trade with `pnl_pts_real == None` but real `match` fills present → FIFO captures it
   (the +170 vs +167 case).
5. Unparseable / empty `orders.jsonl` → `None` (fail-open).
6. Stale unmatched leg dated before the window start → excluded by the window floor (the
   +2176 regression guard).
7. Divergence cross-check: FIFO vs `pnl_pts_real` sum within tolerance → no warning; beyond
   tolerance with zero nulls → warning emitted.

## Out of scope

- No change to `reconcile_real_fill.py` beyond what falls out naturally (it can keep its
  base-`MXF` whole-history FIFO as the contamination detector, or be revisited later).
- No change to order placement, signal logging, or `trades.jsonl` schema.
- Deployment is **observe-first + ask-first** (live circuit-breaker input; no paper env).
