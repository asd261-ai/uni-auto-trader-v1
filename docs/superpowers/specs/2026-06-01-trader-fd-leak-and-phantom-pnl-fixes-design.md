# Trader fd-leak self-heal + phantom-pnl-on-rejection fixes — Design

**Date:** 2026-06-01
**Status:** Design approved (Sean), pending spec review → writing-plans
**Scope:** uni-auto-trader-v1 (real-money, account 0239174, no paper env → observe-first)

Two independent but co-shipped fixes, both surfaced by the 2026-06-01 incident where the
trader went fd-blind ~9.5h and (separately) recorded phantom P&L on broker-rejected orders.

---

## Part 1 — fd-leak runtime self-heal

### Problem
The broker SDK (`unitrade`) `TCPClient._connect()` opens a new socket per reconnect attempt
but does not close the prior socket on failure (reconnect loop recurses via
`_excetion_for_error → sleep → _connect()`). When the broker is unreachable — notably the
weekend quote-maintenance window (Sat 07:00 → Mon 07:20, see
`reference-broker-maintenance-hours`) — dquote/dtrade/daccount TCP clients spin-reconnect and
leak fds until the process hits the soft limit (1024). All new sockets then fail with
`[Errno 24] Too many open files`: the feed dies, exits can't fire, but systemd still shows
`active`. On 2026-06-01 the process (up since 5/31 19:10) exhausted fds by ~00:00 and stayed
blind until a manual restart at 09:46.

The existing `os._exit(1)` safety (`main.py:60-66`) only covers **startup** failure. There is
no mechanism to recover from a **runtime** feed-death after a successful start.

### Goals
- Survive the weekend reconnect-storm leak (don't exhaust fds before maintenance ends).
- Auto-recover if the trader does get stuck blind, with no manual intervention.
- Add a backstop so a genuine broker outage can't cause an infinite restart storm.

### Non-goals
- Fixing the SDK leak at source (would require a vendored fork of a C-extension). Deferred.
- Preventing the leak during maintenance (stopping SDK clients). Strategy chosen is
  **survive + self-heal**, not source-prevention.

### Design

**① systemd `LimitNOFILE=65536`** (band-aid, config-only)
Raise the fd soft limit from 1024 to 65536 in `uni-trader.service`. A full-weekend leak
(~17k estimated) then has headroom. `daemon-reload` to apply. No code change.

**② tick-watchdog kill-tier** (runtime self-heal, TDD)
Extend `tick_watchdog.py` (`TickStaleWatchdog`) with an escalation tier above the existing
alert tier. The watchdog already detects feed-stale via `last_tick_age` and already only
evaluates during active (day/night) sessions — break/maintenance windows are inherently
skipped (validated 5/31 pre-check: "break session 不評"), which is the natural
maintenance-window gate.

New behavior: when the feed has been continuously stale beyond a **kill threshold** (more
conservative than the alert threshold — default day 180s / night 600s, env
`TICK_STALE_KILL_SEC`) **AND** the process uptime exceeds a **grace period** (default 180s,
env `TICK_STALE_KILL_GRACE_SEC`), invoke an escalation callback. In production that callback
is `os._exit(1)` → systemd `Restart=always` restarts the process → OS reclaims all leaked fds
→ reconnect succeeds (broker up) → recovered. This extends the existing
"startup-failure → os._exit" philosophy (`main.py`) to runtime feed-death.

Two guards against a self-kill loop:
- **active-session gate** — already built in; avoids escalating during legitimate
  maintenance/break windows when stale is expected.
- **uptime grace** — a freshly-restarted process gets `grace` seconds to reconnect before it
  is eligible to self-kill again.

Testability: the kill action is an **injected callback** (same pattern as the existing notify
callback at `strategy.py:576`), so unit tests assert the callback fires/doesn't-fire under
controlled (age, uptime, session) inputs without ever calling `os._exit`.

**③ systemd `StartLimitIntervalSec=600` + `StartLimitBurst=6`** (restart-loop backstop)
Current unit has only `Restart=always` / `RestartSec=15` — a genuine broker outage during an
active session would loop os._exit → restart → login-fail → os._exit every 15s indefinitely.
With the StartLimit, systemd gives up after 6 restarts in 10 min (enters `failed`), stopping
the storm. The stale heartbeat then drives a watchdog alert (depends on the separate
`isWeekendBreak()` boundary fix — out of scope here, tracked in `project-trader-watchdog`).

### Rollout (observe-first; no paper env)
- **Phase A:** kill logic live but callback only **logs** `[tick-wd KILL would-fire] ...`
  (no real exit). Config ① ③ ship now. Run a full weekend cycle; confirm zero false would-fire
  in maintenance windows and a correct would-fire only on real stale.
- **Phase B:** flip env `TICK_STALE_KILL=on` to wire the real `os._exit(1)`.

---

## Part 2 — phantom-pnl-on-rejection rollback

### Problem
`_open_unit()` (`strategy.py:1436`) appends the unit to `self._units` **at order-placement
time** (1474), independent of whether the order fills. The existing `_pending_fills` +
`on_fill` mechanism only back-fills the real `entry_fill` price; it does not gate the unit's
existence on a fill. `_on_reply` (`trader.py:108`) logs the broker reply — including
rejections like `FUF1239:...保證金超過...` — but **never feeds it back to the strategy**.

Result: when an order is rejected, no `on_fill` ever arrives, the phantom unit lingers
(`entry_fill=None`), and it is later "closed" (also rejected) with phantom P&L recorded. On
2026-06-01 (Sean's manual MXFG6 short consumed account margin → bot's MXFF6 entries rejected),
internal `trading_day_pnl_pts` / `month_pnl_pts` read **-576.5** vs real **-153** (1 fill).
This pollutes the monthly tally and risks falsely tripping the DAILY_MAX_LOSS lock.

### Goals
- A broker-rejected entry must **not** create a tracked unit and must **not** record P&L.
- Roll back the optimistic expected-position bump so recon stays clean.

### Non-goals
- Fixing the shared-account margin contention itself (a separate account/ops decision —
  `project-shared-account-margin-contention`).
- The full fill-confirmation lifecycle (approach B). Chosen approach is **A: reply-driven
  rollback**, reusing the existing `_pending_fills` FIFO.

### Design (approach A — reply-driven rollback)

1. **Detect rejection** in `trader.py:_on_reply`: classify `reply.orderstatus`. A status
   indicating broker rejection (e.g. begins with `FUF` / matches the known reject-code set)
   routes to `self.strategy.on_order_rejected(productid, bs, orderstatus)`. Accepted/working
   (`委託成功`) and filled (`完全成交`) statuses are unchanged (fills already flow via
   `_on_match → on_fill`).

2. **Roll back** in a new `strategy.on_order_rejected(productid, bs, orderstatus)`:
   - FIFO-match the rejection to the oldest **pending entry** for that `(product, bs)` in
     `_pending_fills` (same FIFO discipline the fill-anchor uses), to identify the unit.
   - Remove that unit from `self._units` and drop its `_pending_fills` entry.
   - Roll back the expected-position bump so `recon_expected_net` returns to pre-order value.
   - Record **no** P&L. Log `[order-rejected] source=… dir=… status=… → unit rolled back`.
   - Notify (health channel) once, optionally, so a margin-block is visible without spamming.

3. **Entry-rejection cascades:** because the phantom unit never exists, the later phantom
   close (and its `FUF0092:無足夠留倉口數平倉` + phantom P&L) cannot occur. So handling the
   **entry** rejection removes the bulk of today's phantom P&L without separately handling
   exit rejections.

### Edge cases
- **Reply/registration race:** `_on_reply` may fire on the dtrade callback thread while
  `_open_unit` holds `self._lock`. `on_order_rejected` must take `self._lock`; if the matching
  pending entry isn't found yet (reply beat registration — unlikely given placement precedes
  append within the same lock, but possible across threads), log and no-op safely rather than
  mutate wrong state.
- **Reject-code set:** confirm the set of FUF/error statuses that mean "rejected, no fill"
  vs. informational. Start with `FUF*` prefix; verify against observed codes
  (`FUF1239`, `FUF0092`) and the SDK status vocabulary before locking the classifier.
- **Partial fills:** out of scope (units are size 1); a reject is all-or-nothing here.

### Testing (TDD)
Pure-logic unit tests on the strategy with a fake trader/order layer:
- entry placed → reject reply → unit removed, no `_record_trade`, expected rolled back.
- entry placed → fill → reject of a *different* order does not touch the filled unit.
- reject with no matching pending entry → safe no-op.
- two pending entries, one rejected → FIFO pops the correct one.

---

## Shared deploy mechanics
- Deploy via scp, not git pull (`feedback-vps-trader-deploy-scp`); sha256 verify both ends.
- Any restart uses `trader-precheck.sh && restart` (`feedback-trader-service-precheck-sop`).
- Real-money, no paper env → observe-first; Part 1 ② is two-phase, Part 2 ships armed
  (rollback is strictly safer than the current record-phantom behavior) but is verified live
  on the next rejection.
- Production changes are ask-first per CLAUDE.md.

## Risks
- Part 1 ②: a too-aggressive kill threshold could self-kill on a benign slow patch — mitigated
  by conservative thresholds + grace + Phase A observe.
- Part 2: misclassifying a non-reject status as a reject would wrongly drop a real unit —
  mitigated by a conservative reject-code set verified against observed statuses + tests.
