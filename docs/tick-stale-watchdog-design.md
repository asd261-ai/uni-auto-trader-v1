# Tick-stale watchdog — design

> Status: **IMPLEMENTED (Phase 1, observe-only) — not yet deployed to VPS.** Code lives in
> `tick_watchdog.py` (`TickStaleWatchdog`, 13 unit tests) + `strategy.py` hooks; alerts go to the
> log as `[tick-wd OBSERVE]`, NOT Telegram. PR #1 (`tick-stale-watchdog` → `main`). Deploy via
> scp + sha256 verify + restart on a weekday; Phase 2 swaps the log-lambda for `_safe_health_notify`.
> Author: Bob, 2026-05-23 (status updated 2026-05-25). Surface map from code-researcher.

## Problem

The trader gets live price exclusively from the broker `dquote` callback
(`trader._on_tick`, trader.py:103 → `strategy.on_tick`, strategy.py:418). Two failure modes
are **currently undetected**:

1. **Never subscribed.** `_subscribe()` (trader.py:90) is called once at startup and its
   failure is *non-fatal* (trader.py:91-98) — the trader can run with no tick feed at all.
2. **Silently stopped.** There is **no** `dquote` reconnect/resubscribe path (only the
   `dtrade` order channel reconnects). If the quote feed goes quiet mid-session, nothing
   notices and nothing recovers.

Either way the trader stays "alive but blind": it can't fire exit checks
(`_check_exit_unit`) because `on_tick` never runs, yet heartbeat + recon keep looking
healthy. **This is the gap.**

### Why existing protections don't cover it
- **Trader→Worker heartbeat watchdog** detects the whole process dying — not "alive but no ticks."
- **Broker recon (60s)** reads *positions* via `get_position`, not ticks — recon can be green while the tick feed is dead.
- **dtrade reconnect** handles the *order* channel, not the *quote* channel.

So this watchdog is complementary, not redundant.

## Design (alert-only v1)

Four pieces, all inside `strategy.py`, mirroring the existing recon machinery.

### (a) Stamp last-tick time — at the TOP of `on_tick`, before the flat early-return
`strategy.on_tick` (strategy.py:418) currently returns early when flat:
```python
def on_tick(self, price: float):
    with self._lock:
        all_units = self._flatten_units()
        if not all_units:
            return            # <-- ticks still arrive when flat!
        ...
```
**Critical:** stamp BEFORE the early-return, else the watchdog false-alarms whenever the
book is flat. Use a lockless atomic float (GIL-atomic assignment; no lock needed):
```python
def on_tick(self, price: float):
    self._last_tick_ts = time.time()        # PROPOSED: first line, outside the flat check
    with self._lock:
        all_units = self._flatten_units()
        if not all_units:
            return
        ...
```
Init `self._last_tick_ts = 0.0` in `__init__` (beside the recon latches ~strategy.py:225-230).

### (b) Periodic check — beside recon in `_poll_loop`
In `_poll_loop` (strategy.py:455), next to `_check_broker_reconciliation()` (strategy.py:498),
add `self._check_tick_staleness()`, self-throttled to ~30s the same way recon throttles to
60s (`_recon_last_check` pattern, strategy.py:225/760). 30s cadence so we detect within
~30s of crossing the threshold.

### (c) Session/break/weekend gate + transition grace
Reuse recon's gate verbatim (strategy.py:754-757):
```python
if self._current_session not in ("day", "night"):
    return
if datetime.now(TZ_TW).weekday() >= 5:
    return
```
`self._current_session` is kept fresh by `_check_session_change()` (strategy.py:462).
**Add a transition grace:** when the session flips INTO day/night, stamp
`self._active_session_since = time.time()` (hook inside `_check_session_change`). Staleness
is then measured against the *later* of last tick and session-start, so the first ticks of a
session have time to arrive:
```python
ref = max(self._last_tick_ts, self._active_session_since)
age = time.time() - ref
```

### (d) Threshold (session-aware) + deduped alert + recovery
Day session (08:45-13:45) is liquid → short threshold. Night (15:00-05:00) is thin,
especially deep night → longer threshold. Both env-tunable:
```
TICK_STALE_DAY_SEC   = 90    # liquid day session; >90s with no tick is abnormal
TICK_STALE_NIGHT_SEC = 300   # thin night session; allow longer legit gaps
TICK_CHECK_INTERVAL_SEC = 30
```
Dedup with a one-shot bool latch `self._tick_stale_alert_sent` (init in `__init__`), exactly
like `_recon_alert_sent` (set on alert strategy.py:820, cleared with a "✅ recovered" notify
strategy.py:800-808). Alert via `self._safe_health_notify(...)` → Health bot (strategy.py:1516).

```python
def _check_tick_staleness(self):
    now = time.time()
    if now - self._tick_check_last < TICK_CHECK_INTERVAL_SEC:
        return
    self._tick_check_last = now
    if self._current_session not in ("day", "night"):
        return
    if datetime.now(TZ_TW).weekday() >= 5:
        return
    threshold = TICK_STALE_DAY_SEC if self._current_session == "day" else TICK_STALE_NIGHT_SEC
    ref = max(self._last_tick_ts, self._active_session_since)
    age = now - ref
    if age > threshold:
        if not self._tick_stale_alert_sent:
            self._safe_health_notify(
                f"⚠️ TICK FEED STALE — no dquote tick for {age:.0f}s "
                f"(session={self._current_session}, threshold={threshold}s). "
                f"Trader is alive but blind to price; exits won't fire. Check feed / restart."
            )
            self._tick_stale_alert_sent = True
    else:
        if self._tick_stale_alert_sent:
            self._safe_health_notify(f"✅ Tick feed recovered (age {age:.0f}s).")
            self._tick_stale_alert_sent = False
```
Keep **all** latch logic in this poll-thread method; `on_tick` only writes the timestamp →
no cross-thread latch race.

### (e) (optional) Heartbeat visibility
Add one key to the heartbeat dict (strategy.py:524) for Worker-side defense-in-depth:
```python
"last_tick_age_sec": (time.time() - self._last_tick_ts) if self._last_tick_ts else None,
```
Trader-side is one line; the Worker (mtx-monitor, separate repo) would need to read/alert on
it to add value — out of scope here.

## Explicitly NOT doing in v1: auto-resubscribe
There is no existing `dquote` resubscribe path, and whether `unsubscribe`+
`subscribe_trade_bid_offer` mid-session is safe is an **unverified SDK behavior** on a LIVE
real-money trader. v1 is **alert-only** — tell Sean, let him decide to restart. Auto-recovery
is a possible v2 *only after* confirming SDK resubscribe behavior on the VPS.

## Config (new env, all optional with safe defaults)
| Env | Default | Meaning |
|---|---|---|
| `TICK_STALE_DAY_SEC` | 90 | staleness threshold during day session |
| `TICK_STALE_NIGHT_SEC` | 300 | staleness threshold during night session |
| `TICK_CHECK_INTERVAL_SEC` | 30 | how often the watchdog evaluates |

## Test plan (before any live deploy)
1. **Unit tests** (no SDK, no broker): drive a fake clock + fake `_current_session`; assert
   (a) no alert when fresh, (b) alert after threshold, (c) no alert during break/weekend,
   (d) grace after session transition, (e) recovery clears the latch, (f) flat-book ticks
   still refresh the stamp (the early-return gotcha).
2. **No paper environment:** `test167` is decommissioned (2026-05), so there is no paper
   login to dry-run on. Validation happens live on `viploginm` but **observe-only first**
   (see phased rollout) — safe because v1 is alert-only, try/except-wrapped, and gated.
3. **Thresholds:** sanity-check `TICK_STALE_NIGHT_SEC` against a real thin-night tick-gap
   sample before trusting it (avoid night false-alarms) — this is exactly what Phase 1
   observes.

## Rollout — two phases (test167 gone; respects the live-trader rule)
The only difference between phases is the `check()` notify callback.

**Phase 1 — observe-only (log, not Telegram):**
- `check()` runs the full logic but routes alerts to `logger.warning("[tick-wd OBSERVE] …")`,
  NOT `_safe_health_notify`. Also exposes `last_tick_age_sec` in the heartbeat.
- Deploy to viploginm (scp `strategy.py` + `tick_watchdog.py` + sha256 verify + restart,
  **not on a weekend**), on Sean's explicit go.
- Watch the trader log for ≥1 full day+night session: confirm (a) `last_tick_age` stays low
  during sessions, (b) NO spurious `[tick-wd OBSERVE] ⚠️ … STALE` during normal operation
  (validates thresholds/gating), (c) it stays quiet during breaks/weekend.

**Phase 2 — enable Telegram alerts:**
- One-line change: swap the notify lambda for `self._safe_health_notify`.
- Re-deploy (same scp + verify + restart, non-weekend) on Sean's go.

Both phases are alert-only (no auto-resubscribe) and try/except-wrapped, so worst case is a
log/Telegram false-or-missing alert — never a trading malfunction.

## Integration points (exact)
- stamp: `strategy.py:418` (top of `on_tick`); init `_last_tick_ts` ~`strategy.py:225-230`
- check call: `strategy.py:498` (in `_poll_loop`, beside recon)
- session ref: `_check_session_change` `strategy.py:462` (add `_active_session_since` stamp)
- gate: copy `strategy.py:754-757`
- alert: `_safe_health_notify` `strategy.py:1516` (Health bot)
- heartbeat field (optional): `strategy.py:524`
