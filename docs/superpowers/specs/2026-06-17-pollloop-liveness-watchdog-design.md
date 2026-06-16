# Poll-loop liveness watchdog + broker-read timeout (Phase 1)

**Date:** 2026-06-17
**Status:** Design approved — pending implementation plan
**Scope:** Phase 1 of 2. Phase 2 (broker-write timeout + phantom-fill reconciliation) is a separate spec.

## 1. Problem

On 2026-06-16 ~18:30–19:26 TW the trader's `_poll_loop` (strategy.py:641) **silently froze for 57 minutes**: the process stayed alive (`systemctl is-active=active`, `NRestarts=0`, PID unchanged) but the loop stopped iterating. No exception, no SEGV, no `Errno24`, no disconnect log; it self-recovered at 19:26. During the freeze the trader placed no new orders, but the broker-side stop-loss stayed live and the held long was stopped out (−42 pts).

Two findings drove this design:

1. **Root cause (read-confirmed):** the loop makes synchronous **broker SDK calls with no timeout** — `trader.py:176 daccount.get_position()` (recon, ~1/min) and `trader.py:215 daccount.get_margin()` (margin headroom, ~1/min), plus the order writes `dtrade.order/replace_order`. `self.api = Unitrade()` is a C-native SDK; a hung internal socket blocks the whole loop indefinitely without raising. The 14:50 `on_tickdatabeforebidoffe` callback storm is the likely precursor (SDK in a degraded state). `_fetch_history` already has `timeout=10` and `heartbeat.send` runs in a daemon thread, so neither is the culprit.

2. **The existing tick-stale watchdog (`TICK_STALE_KILL`, armed 2026-06-15) cannot catch this.** `tick_watchdog.check()` runs **inside** `_poll_loop` (strategy.py:699), *after* the SDK calls. When the loop freezes at an SDK call, `check()` is never reached → the watchdog can neither evaluate nor fire. Worse, the watchdog measures **feed** staleness (`_last_tick_ts`, updated by the broker thread's `on_tick`), not **loop** liveness — if the broker tick thread keeps delivering ticks during the freeze, `last_tick_age` even looks healthy. A watchdog that lives inside the loop it guards is structurally blind to a freeze of that loop. The only layer that caught the 6/16 freeze was the **external** heartbeat-staleness monitor (mission-control / Twilio), because the heartbeat (sent at the *end* of the loop) stopped.

See `MEMORY: project-trader-pollloop-freeze`, `project-killtier-monday-dawn-storm`.

## 2. Goals / non-goals

**Goals (Phase 1):**
- **G1 — Detect and auto-recover a poll-loop freeze** from *any* cause, via a watchdog that does **not** live inside the poll loop. This closes the architectural gap that `TICK_STALE_KILL` cannot.
- **G2 — Stop the known proximate cause** (broker *read* hangs) from freezing the loop at all, by bounding `get_position` / `get_margin` with a timeout.

**Non-goals (deferred to Phase 2):**
- Bounding broker **write** calls (`buy`/`sell`/`replace_order`) with a timeout. A timed-out write may still fill at the broker → phantom position; it needs dedicated reconciliation. Out of scope here.
- Diagnosing *why* the Unitrade SDK hangs internally (SDK is a black box).

## 3. Architecture

Three components. The first two implement G1; the third implements G2.

```
┌─ poll loop (strategy.py:_poll_loop, daemon thread) ───────────────────┐
│  ... session checks / fetch_history(timeout) / sync / check_new_signal │
│  recon  → get_position  ─┐                                             │
│  margin → get_margin    ─┴─ wrapped in call_with_timeout (G2)          │
│  tick_wd.check() ; heartbeat.send()                                    │
│  self._pollloop_wd.record_poll_complete(monotonic())   ← stamp (G1)    │
│  sleep(POLL_INTERVAL=3s)                                               │
└────────────────────────────────────────────────────────────────────────┘
        ↑ reads _last_complete_ts (GIL-atomic float)
┌─ pollloop-wd thread (strategy.py, INDEPENDENT daemon thread) ─────────┐
│  while running:                                                        │
│    pollloop_wd.check(monotonic(), uptime, on_kill=_pollloop_wd_kill)   │
│    sleep(CHECK_SEC=30)                                                 │
│  → age = now - _last_complete_ts                                       │
│  → age > FREEZE_SEC and uptime > GRACE:                                │
│      observe (KILL=off) → log "[pollloop-wd KILL would-fire] …"        │
│      armed  (KILL=on)   → _safe_health_notify + os._exit(1) → systemd  │
└────────────────────────────────────────────────────────────────────────┘
```

The watchdog thread does **only** float-compare + log/notify — it never touches the broker SDK or network, so it can never be frozen by the same hang it is detecting.

## 4. Component 1 — `pollloop_watchdog.py` (new, pure)

Mirrors `tick_watchdog.py` / `disconnect_watchdog.py`: pure, side-effect-free, no internal lock, all time passed in by the caller, fully unit-testable. Side effects (os._exit, Telegram) live in strategy.py.

```python
class PollLoopLivenessWatchdog:
    def __init__(self, *, freeze_threshold=120.0, check_interval=30.0, kill_grace=180.0): ...
    def record_poll_complete(self, now: float) -> None:   # poll thread; GIL-atomic float write
        self._last_complete_ts = now
        self._kill_fired = False                          # loop alive again → re-arm
    def last_complete_age(self, now: float) -> float | None: ...   # for heartbeat/observability
    def check(self, now: float, uptime: float, on_kill: Callable[[str], None]) -> None:
        # 1. throttle to check_interval
        # 2. uptime <= kill_grace → return (anti boot-loop)
        # 3. _last_complete_ts == 0 → return (no iteration completed yet)
        # 4. age = now - _last_complete_ts
        # 5. age > freeze_threshold and not _kill_fired → on_kill(msg); _kill_fired = True
```

**No `session` / `is_weekend` parameter** — see Decision D1.
**`now` is `time.monotonic()`**, not wall clock — see Decision D2.

## 5. Component 2 — strategy.py wiring

Five change points, each mirroring the existing `TICK_STALE_KILL` wiring:

1. **Env block** (beside strategy.py:177-180):
   ```python
   POLLLOOP_FREEZE_SEC       = int(os.getenv("POLLLOOP_FREEZE_SEC", "120"))
   POLLLOOP_FREEZE_GRACE_SEC = int(os.getenv("POLLLOOP_FREEZE_GRACE_SEC", "180"))
   POLLLOOP_FREEZE_CHECK_SEC = int(os.getenv("POLLLOOP_FREEZE_CHECK_SEC", "30"))
   POLLLOOP_FREEZE_KILL      = os.getenv("POLLLOOP_FREEZE_KILL", "off").lower() == "on"
   ```
2. **`__init__`** (beside the `tick_watchdog` construction ~:328): build `self._pollloop_wd = PollLoopLivenessWatchdog(...)`; record a monotonic boot anchor `self._proc_start_monotonic = time.monotonic()`.
3. **`_poll_loop` end** (before `time.sleep(POLL_INTERVAL)`, :739): `self._pollloop_wd.record_poll_complete(time.monotonic())`.
4. **New thread target `_pollloop_wd_loop()`** + spawn in `start()` (beside the `_poll_loop` thread spawn ~:453):
   ```python
   def _pollloop_wd_loop(self):
       while self._running:
           try:
               self._pollloop_wd.check(
                   time.monotonic(),
                   uptime=time.monotonic() - self._proc_start_monotonic,
                   on_kill=self._pollloop_wd_kill,
               )
           except Exception as e:
               logger.debug(f"pollloop-wd error (silent): {e}")
           time.sleep(POLLLOOP_FREEZE_CHECK_SEC)
   # in start():
   threading.Thread(target=self._pollloop_wd_loop, daemon=True).start()
   ```
5. **New `_pollloop_wd_kill(msg)`** — byte-for-byte mirror of `_tick_wd_kill`:
   ```python
   def _pollloop_wd_kill(self, msg):
       if not POLLLOOP_FREEZE_KILL:
           logger.error(f"[pollloop-wd KILL would-fire] {msg}")
           return
       logger.error(f"[pollloop-wd KILL] {msg}")
       try: self._safe_health_notify(f"🔪 Trader self-restart (poll-loop freeze): {msg}")
       except Exception: pass
       import os as _os; _os._exit(1)
   ```

Optionally add `"poll_loop_age_sec": self._pollloop_wd.last_complete_age(time.monotonic())` to the heartbeat payload for external observability.

## 6. Component 3 — `sdk_timeout.py` + trader.py read wrapping (G2)

```python
class SDKCallTimeout(Exception): ...

def call_with_timeout(fn, *args, timeout, **kwargs):
    """Run fn in a fresh daemon thread; return its result, or raise SDKCallTimeout
    if it exceeds `timeout`. A timed-out call's thread is abandoned (daemon)."""
```

- **Fresh daemon thread per call**, not a shared pool: a shared single-worker pool would be blocked by a stuck call for all subsequent calls; a fresh thread leaks one stuck thread but does not block the next cycle. A truly wedged SDK is then caught by the liveness watchdog / restart.
- **trader.py** wraps the SDK call inside `_query_broker_position` (:176) and `_query_broker_margin_excess` (:215):
  ```python
  resp = call_with_timeout(self.api.daccount.get_position, self.actno,
                           timeout=SDK_READ_TIMEOUT_SEC)   # default 5s, env-overridable
  ```
  On `SDKCallTimeout`, each method returns the sentinel its caller already treats as "skip this cycle": `_query_broker_position` returns **`SCHEMA_FAIL`** (NOT `None` — recon treats `broker_pos is None` as "broker flat", so a timeout→`None` could fire a false `DAILY_RECON_ALERT`; `SCHEMA_FAIL` hits the existing `strategy.py:1102` skip-without-alert branch), and `_query_broker_margin_excess` returns **`None`** (its caller `_check_margin_headroom` skips on `None`). Both are ~1/min throttled safety nets, so skipping a cycle is benign. A consecutive-`SCHEMA_FAIL` counter (schema-drift OR timeout) raises a health alert after `SDK_READ_TIMEOUT_ALERT_N` in a row.
- **Consecutive-timeout counter:** after `K` consecutive timeouts (default 3), `_safe_health_notify("broker reads timing out Nx — SDK may be wedged")` for visibility *before* the freeze backstop. Counter resets on any successful read.
- **Writes are NOT wrapped** in Phase 1 (Phase 2).

## 7. Key design decisions

**D1 — No session-gate (deliberate divergence from `tick_watchdog` / `disconnect_watchdog`).**
Those gate on active-session because their signal (feed silence / disconnects) is *legitimately present* during breaks. The poll loop, by contrast, completes an iteration every ~`POLL_INTERVAL` (3s) in **all** conditions — including weekends and breaks, where it still runs session checks + recon + margin + heartbeat (the heartbeat send sits outside the `is_weekend` guard). So `_last_complete_ts` advances every ~3s always, and any 120s+ gap is a genuine freeze regardless of session. Session-gating liveness would open a blind spot for weekend/break freezes. Therefore liveness uses **only** uptime-grace, no session-gate. This does not reintroduce the Monday-dawn-storm class: (a) a weekend broker-read hang is now bounded by the G2 read-timeout so it never freezes the loop, and (b) a genuine freeze that is killed restarts into a light loop that stamps normally and does not re-kill.

**D2 — Monotonic clock.** `record_poll_complete`, `check`, and the uptime anchor all use `time.monotonic()`, immune to NTP steps / DST folds / VM resume. This is cleaner than `disconnect_watchdog`'s wall-clock clamp and removes any false-kill-on-clock-jump risk. (The heartbeat payload keeps using wall-clock `ts` independently.)

**D3 — Single threshold (consequence of D1).** With no session-gate and an identical day/night loop cadence, the day/night threshold split from `tick_watchdog` is meaningless here. One `POLLLOOP_FREEZE_SEC=120` covers both. 120s ≈ 40× the normal ~3s iteration — ample margin against a slow-but-not-frozen iteration, while recovering far faster than the external monitor's 600s heartbeat-stale threshold.

**D4 — os._exit may fire during a hung broker *write* (known Phase-1 limitation).** Phase 1 does not wrap writes, so a write-call hang can still freeze the loop and the watchdog will `os._exit` mid-write. The order may already have filled at the broker → on restart, the existing `_check_broker_reconciliation` (Plan D) detects `broker_net != expected` and alerts (mismatch > 3 min). Nothing breaks silently; a phantom is flagged for manual reconcile. Phase 2 (write-timeout + phantom-fill) closes this cleanly.

## 8. Config / env (all overridable, observe-safe defaults)

| env | default | meaning |
|---|---|---|
| `POLLLOOP_FREEZE_KILL` | `off` | `off` = observe (log would-fire only); `on` = arm os._exit |
| `POLLLOOP_FREEZE_SEC` | `120` | loop-iteration staleness that counts as frozen |
| `POLLLOOP_FREEZE_GRACE_SEC` | `180` | min process uptime before a kill is eligible |
| `POLLLOOP_FREEZE_CHECK_SEC` | `30` | watchdog-thread evaluation period |
| `SDK_READ_TIMEOUT_SEC` | `5` | per-call timeout for `get_position` / `get_margin` |
| `SDK_READ_TIMEOUT_ALERT_N` | `3` | consecutive read timeouts before a health alert |

## 9. Edge cases

- **First iteration not yet complete** (`_last_complete_ts == 0`): no age computed, no kill (also covered by uptime-grace).
- **Watchdog thread resilience**: each iteration is wrapped in try/except (debug-logged); the thread never dies. Ultimate backstop remains the external heartbeat monitor.
- **`_kill_fired` latch**: in observe mode, on_kill logs once per freeze episode (not every 30s); `record_poll_complete` resets it on recovery. In armed mode the process exits, so no double-fire.
- **read-timeout ↔ liveness interaction**: a bounded read-timeout keeps the loop alive (skip-on-timeout, stamp continues), so the liveness watchdog fires only for *other* whole-loop wedges. Complementary, not redundant.
- **Stale `_current_session` during a freeze**: irrelevant — the watchdog no longer reads session (D1).

## 10. Testing

1. **Unit (pure, committed, no sleeps):**
   - `test_pollloop_watchdog.py`: no kill within grace; no kill when `_last_complete_ts==0`; kill when age > threshold (on_kill called once); no kill when age ≤ threshold; throttle (evaluates only every check_interval); `_kill_fired` latch (one call per episode); `record_poll_complete` resets latch; monotonic — no negative-age kill.
   - `test_sdk_timeout.py`: returns value on completion; raises `SDKCallTimeout` past timeout; a stuck thread does not block a subsequent call; a fn that raises propagates (not mis-reported as timeout).
2. **Wiring validation (observe-first live — no paper env):** deploy with `POLLLOOP_FREEZE_KILL=off`; over several days spanning a session open/close and an overnight, confirm **zero** false `[pollloop-wd KILL would-fire]` (normal operation stamps continuously → age stays < 120s). Same bar that validated `TICK_STALE_KILL` / disconnect-storm.
3. **Arm gating:** the observe branch of `_pollloop_wd_kill` (env off → log, no exit) is unit-testable; the arm branch (os._exit) is validated live at the arm step.

## 11. Rollout (observe → arm, ask-first)

1. Unit tests green; merge to main.
2. Deploy to VPS in **observe** (`POLLLOOP_FREEZE_KILL` unset/off) + read-timeout active. Verify clean boot, zero false would-fire across sessions + overnight (drift-check + sha256,休息窗 `precheck.sh && restart`).
3. After observe confirms no false-fire → **arm** (`POLLLOOP_FREEZE_KILL=on`), ask-first, via the休息窗 restart SOP.
4. **Rollback:** delete the env line + restart. The pure module + thread are inert when `KILL=off`.

## 12. Phase 2 (separate spec, out of scope here)

Broker **write** timeout (`buy`/`sell`/`replace_order`) + phantom-fill reconciliation: on a write timeout, query the broker's actual position and decide 補單 / 撤單 / 接受, leveraging the existing `reconcile_real_fill.py` / Plan D recon rather than building new. Higher risk (touches live order execution) → its own spec, plan, and observe-first validation.
