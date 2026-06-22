# dquote auto-resubscribe — design

**Date:** 2026-06-23
**Status:** approved (brainstorm), pending implementation plan
**Related:** `2026-06-22-trader-fd-leak-watchdog-design.md`, tick-stale watchdog (`tick_watchdog.py`),
`project-killtier-monday-dawn-storm` (2026-06-22 root-cause note)

## Problem

The dquote tick feed has no resubscribe path. `trader._subscribe()` (trader.py:111-120) calls
`api.dquote.subscribe_trade_bid_offer(product)` once at boot; on failure it logs a warning and
returns — no retry (deliberate, to avoid a zombie process). dquote exposes **no**
connected/disconnected callback (only dtrade does), and `trader._on_connected` /
`strategy.on_reconnect` do **not** resubscribe dquote. So when the feed dies — e.g. a restart that
lands inside the broker quote-maintenance window, where subscribe fails with `Broken pipe` — the
bot runs blind until the tick-stale watchdog (`TICK_STALE_KILL`) restarts the whole process.

**2026-06-23 incident root cause:** the 06:58 fd-leak restart landed in the maintenance window;
dquote subscribe failed; dtrade reconnected at 07:27 but dquote did not follow; the bot ran ~1.5h
with a dead feed until tick-wd killed it at 08:48. The full restart is a heavy recovery for a
problem a single resubscribe call could fix.

**Key facts (verified 2026-06-23):**
- `api.dquote.subscribe_trade_bid_offer(product)` returns `(ok, err)` and is a plain re-callable
  Python method; `unsubscribe_trade_bid_offer(product)` exists too. Resubscribe is reachable in
  Python (unlike the C-ext fd leak).
- Feed liveness can only be inferred from tick staleness (`tick_watchdog`), not a status flag.
- Mid-session resubscribe is **unverified SDK behavior on a live account** (tick_watchdog.py:22-23),
  and there is **no paper env** — so rollout must be observe-first.
- The maintenance window can make a subscribe call block — every SDK call must be wrapped in
  `call_with_timeout` (sdk_timeout.py).

## Goal

Recover the dquote feed by resubscribing **before** the tick-wd kill restarts the process,
eliminating the dead-feed window. The tick-wd kill remains the backstop if resubscribe fails.

**Non-goals:** changing the boot `_subscribe` failure philosophy (still no raise); fixing the SDK;
touching order/position logic.

## Architecture

Responsibility split, mirroring the existing watchdog pattern.

- **`dquote_resub.py` — pure `DquoteResubPolicy`**: decides **trigger A** only.
  `should_attempt(now, tick_age, session_active, uptime) -> bool`, holding stale-threshold,
  cooldown, max-attempts-per-episode, grace, and episode reset. No clock, no SDK — fully
  unit-testable (time and tick_age passed in).
- **`trader.py` — `resubscribe_dquote(reason) -> bool`**: the SDK call lives here (trader owns
  `api.dquote`). Env-gated: observe → log `[dquote-resub would-fire] reason=…`, no SDK call;
  armed → `call_with_timeout(unsubscribe_trade_bid_offer)` then
  `call_with_timeout(subscribe_trade_bid_offer)` (~5s each), log result, return ok. Never raises.
- **Trigger B (dtrade reconnect)** — `trader._on_connected` (the existing dtrade reconnect
  handler) calls `resubscribe_dquote("dtrade-reconnect")`, guarded by a min-interval cooldown so a
  reconnect storm cannot spam it. Entirely within trader.py; directly targets the 07:27 case.
- **Trigger A (staleness)** — the strategy poll loop, after `_tick_wd.check`, calls
  `policy.should_attempt(...)`; if true it invokes an injected `resubscribe_cb`
  (= `trader.resubscribe_dquote`, passed into the strategy at construction, mirroring the existing
  notify/kill callback injection). Catches a silent dquote death with no dtrade cycle.

## Triggers, thresholds, guards

| Param | Default | Rationale |
|---|---|---|
| resub stale threshold | day 90s / night 300s (= existing alert thresholds) | below the kill thresholds (day 180 / night 600) so resubscribe is tried first; kill backstops |
| per-SDK-call timeout | 5s | maintenance-window broker can block; never wedge the poll loop / reconnect callback |
| max attempts / episode | 3 | try a few times then stop and let tick-wd kill backstop — no spin |
| cooldown between attempts | 60s | |
| uptime grace | 180s | boot `_subscribe` runs first; don't fire during boot |
| `DQUOTE_RESUB` env | default **off** = observe | Phase A validate triggers → Phase B arm |

## Data flow (self-healing loop)

feed dies → (B) dtrade reconnect OR (A) staleness past threshold → `resubscribe_dquote` →
unsubscribe + subscribe → broker resumes ticks → `on_tick` → `_tick_wd.record_tick` → staleness
resets → no kill. If resubscribe fails: retry up to max attempts; still dead → tick-wd kill
restarts (= today's behavior, no worse).

## Rollout (two-phase, observe-first)

- **Phase A (`DQUOTE_RESUB` off, observe):** A and B triggers log `[dquote-resub would-fire]
  reason=…` and make **no SDK call**. Validate: fires at the right time (feed stale / dtrade
  reconnect), and does **not** fire spuriously (healthy feed, cross-session boundaries, weekend
  maintenance windows).
- **Phase B (`DQUOTE_RESUB=on`):** actually resubscribe. Watch one real feed-death and confirm it
  is recovered without a tick-wd kill.

## Error handling / safety

- subscribe returns not-ok / times out / raises → log, count the attempt, never raise (preserve
  the no-zombie philosophy of `_subscribe`).
- Observe phase touches no SDK at all.
- Worst case armed: resubscribe does not recover the feed → tick-wd kill backstop (equivalent to
  the 2026-06-23 behavior).
- No change to order/position logic; this is purely a quote-feed subscription.

## Testing (injected, no broker)

`DquoteResubPolicy` pure-logic tests: stale < threshold → no attempt; stale ≥ threshold + session
active + uptime > grace → attempt; within cooldown → no repeat; after max-attempts → stop;
feed recovery (tick_age small again) → episode resets; session inactive → no attempt; uptime ≤
grace → no attempt. The `resubscribe_dquote` observe/arm branch follows the existing kill-method
pattern (not separately unit-tested, consistent with the three watchdog siblings).

## Deployment (SOP)

scp + sha256 drift check + VPS unit tests + `trader-precheck.sh && systemctl restart` (`&&`, never
`;`). Phase A observe ships first (ask Sean). After confirming triggers fire correctly, set
`DQUOTE_RESUB=on` (ask Sean again). Production real-money trader — ask before each deploy/restart.

## Open follow-ups (out of scope here)

- Arm `DQUOTE_RESUB=on` after one clean observe period with correct, non-spurious triggers.
- If the SDK ever exposes a dquote connected/disconnected callback, trigger B could move to it.
