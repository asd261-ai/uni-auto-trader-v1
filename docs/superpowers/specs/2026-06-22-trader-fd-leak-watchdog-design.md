# Trader fd-leak watchdog — design

**Date:** 2026-06-22
**Status:** approved (brainstorm), pending implementation plan
**Related:** `2026-06-01-trader-fd-leak-and-phantom-pnl-fixes-design.md`,
`2026-06-15-disconnect-storm-circuit-breaker-design.md`,
`2026-06-17-pollloop-liveness-watchdog-design.md`

## Problem

The broker SDK `unitrade` leaks one TCP socket per reconnect attempt: its
`TCPClient._excetion_for_error → sleep → _connect()` path opens a new
`socket.socket()` without closing the previous one. During the weekend
quote-maintenance disconnect storm (reconnect every ~5s, ~4 fd/min), open fds
climb steadily.

On 2026-06-22 this hit the **Python `select()` `FD_SETSIZE` 1024 hard limit**
(NOT the `ulimit`): the SDK's `_connect()` calls
`select.select([], [self.socket], [], ...)`, and once a socket is assigned a fd
number ≥ 1024 the call raises `filedescriptor out of range in select()`. At
05:32 the ftrade client disconnected and could not reconnect; the trader stayed
`active` but crippled (zombie). open_fds = 1026.

**Key constraint (verified 2026-06-22):** the SDK is shipped entirely as Cython
`.so` (ELF shared objects), zero `.py`. `TCPClient._connect` is a C-level
function pointer — **cannot be monkeypatched or subclassed from Python**. A
true source fix requires a vendored C-extension fork (recompile the `.so` with
`socket.close()` added), which is too heavy/brittle for a real-money broker
connection and breaks on every SDK update. Therefore the root-cause fix is **not
reachable at the trader's Python layer**.

**The gap:** the existing three watchdogs (tick-stale, poll-freeze,
disconnect-storm) all key on *symptoms*. None keys on the actually-leaked
resource — the fd count. `fd-weekend-watch` (external launchd monitor) reads fd
count but only *alerts* (every 4h) and never acts; on 6/22 it took a manual
human relay + restart to recover.

## Goal

Prevent the SDK fd-leak from reaching the `select()` 1024 wall by **restarting
the process before fd crosses a safe threshold** — `os._exit(1)` → systemd
`Restart=always` → OS reclaims all leaked fds on the fresh process. This is a
durable, pure-Python, process-level fix that requires no SDK access.

**Non-goals:** fixing the SDK leak at source (C-ext fork — deferred,
out of scope); changing the SDK's reconnect behavior.

## Architecture

A 4th watchdog, mirroring the existing three.

- **New module `fd_watchdog.py`** — `FdLeakWatchdog` class:
  - `check(now, fd_count, uptime, is_flat) -> verdict` — pure decision logic, no
    I/O, fully unit-testable.
  - kill via an **injected callback** (same seam as `tick_watchdog` /
    `pollloop_watchdog` / `disconnect_watchdog`), so tests assert on the verdict
    without touching the real process or broker.
- **Own daemon thread** (mirror `_pollloop_wd_loop`, `strategy.py:808`): every
  `FD_LEAK_CHECK_SEC` (default 30s) it reads
  `len(os.listdir('/proc/self/fd'))` and runs `check()`. Reading `/proc` is pure
  Python, never blocks, and is immune to SDK hangs — it must not live inside
  `_poll_loop` (which can itself freeze).
- **Kill action** `_fd_wd_kill` (mirror `_pollloop_wd_kill`, `strategy.py:2119`):
  log → notify → `os._exit(1)`.

### fd_count semantics

`select()` fails on the fd *value* (≥1024), not the count. fd numbers are
assigned lowest-available, so total open-fd count is a faithful proxy: as count
approaches 1024, newly opened sockets receive fd values near 1024. Counting
`/proc/self/fd` entries is therefore the correct, cheap signal.

## Two-tier gating

| Tier | Default threshold | Condition | Rollout |
|---|---|---|---|
| **Soft** | fd ≥ `FD_LEAK_SOFT` (800) | **AND flat** AND uptime > grace | **Armed on deploy** — `FD_LEAK_SOFT_KILL` defaults **on**. Restart-when-flat is benign (weekend maintenance window has no positions). |
| **Hard** | fd ≥ `FD_LEAK_HARD` (980) | regardless of position AND uptime > grace | **Observe-first** — `FD_LEAK_HARD_KILL` defaults **off** → logs `[fd-wd KILL would-fire]` only, until armed after one clean weekend. |

- **Grace:** `FD_LEAK_GRACE_SEC` (default 180s) — never fire within grace of
  boot (anti-suicide loop), same as existing watchdogs.
- **Margin check:** observed storm climb ≈ 4 fd/min; at a 30s check interval the
  between-check climb is ≈2 fd, so hard=980 leaves ample room below 1024. All
  thresholds are env-tunable.
- **Flat detection:** reuse the trader's existing position state — `mtx_units ==
  []` AND FVG flat AND no `_pending_fills` — the same source precheck Gate 3 and
  the reconciliation loop already use. Do not introduce a parallel notion of
  "flat".

## Decision logic (check)

```
if uptime <= grace:            -> none
if fd >= hard:                 -> HARD  (kill if HARD_KILL armed, else would-fire log)
if fd >= soft and is_flat:     -> SOFT  (kill if SOFT_KILL armed, else would-fire log)
else:                          -> none
```

Hard tier is evaluated before soft so an in-position process above the hard
threshold still triggers the hard path (restart-anyway beats hitting the wall
blind while holding a position).

## Notification

On both would-fire and real kill, push the **Health bot** (Telegram health chat
+ Discord #mission-control), matching the existing watchdogs:
`fd-wd: fd=NNN tier=soft/hard flat=Y/N → killed | would-fire`.

## Error handling / safety

- `/proc/self/fd` read failure → log once, skip the tick, never kill on missing
  data.
- Within grace → never fire (boot anti-suicide).
- Worst case of a soft false-positive = one harmless restart while flat.
- Hard tier in observe mode performs no action — log only.

## Testing (injected, no broker)

Inject an fd-count provider, a kill callback, and a flat flag; assert on
verdicts:

- fd < soft → none
- fd ≥ soft + flat + uptime > grace → SOFT
- fd ≥ soft but **in position** → none (soft requires flat)
- fd ≥ hard regardless of position → HARD verdict
- `HARD_KILL=off` → HARD verdict logs would-fire, kill callback **not** invoked
- `SOFT_KILL=off` → SOFT verdict logs would-fire, kill callback **not** invoked
- uptime ≤ grace → none in all cases (anti-suicide)
- `/proc` read failure → safe skip, no kill

## Deployment (SOP)

scp (sha256 drift check vs VPS) → `trader-precheck.sh && systemctl restart`
(`&&`, never `;`) → soft tier armed, hard tier left in observe. Production
real-money trader: **ask Sean before the deploy/restart.** Rollback = unset the
relevant `*_KILL` env + restart.

## Open follow-ups (out of scope here)

- Arm `FD_LEAK_HARD_KILL` after one clean weekend of zero false would-fire.
- Vendored C-ext fork as the only true root cause — separate backlog, only if
  the SDK vendor provides `.pyx` source.
