# main.py try/except — Zombie Prevention Spec

- **Status**: DRAFT (2026-05-28), not deployed
- **Driver memory**: `[[feedback-trader-weekend-restart-zombie]]` + `[[project-pnl-calc-contract-mixing]]` (Bug#3 sibling fix)
- **Related**: `[[feedback-trader-service-precheck-sop]]` (precheck Gate 2 prevents Sean/Claude from restarting INTO zombie; this spec prevents zombie itself)

## 1. Problem

`main.py` calls `trader.start()` and `strategy.start()` at module level (line 48-49)
without exception handling. The trader SDK has known failure modes:

- `trader.py:49` `RuntimeError("Login failed: ...")` — broker disconnect / wrong cert
- `trader.py:53` `RuntimeError("No accounts found after login")`
- `trader.py:67` `RuntimeError("Contract resolve failed ...")` — broker API hiccup
- `trader.py:70/77` `RuntimeError("No contracts ...")` / `("Front contract has no prod_id ...")`

When any of these raise:
1. Python main thread dies (uncaught exception propagates out of module-level code)
2. **SDK C-extension threads keep running, holding the process PID alive** ← zombie
3. systemd sees `state=active running` (PID still exists) — does NOT trigger `Restart=always`
4. Trader becomes a zombie: process alive, but no login, no heartbeat, no strategy poll, no trading
5. `systemctl stop` is required + `pkill -9` to fully clean (per [[feedback-trader-weekend-restart-zombie]])

**Incidents that exhibited this**:
- 2026-05-23 Saturday: deployed schema-gate + restart during weekend broker outage → zombie (root incident, memory created)
- 2026-05-27 05:47: my controlled restart in dawn-break broker outage → zombie (today's slippage day driver)

**Why this is critical**:
- broker dawn-break disconnects (05:32) appear to be **routine** (5/27 + 5/28 same pattern)
- Any deploy or maintenance restart during outage = automatic zombie risk
- Without this fix, trader system is fragile during the most common broker downtime window

## 2. Hypothesis

Wrapping `trader.start()` + `strategy.start()` in try/except + calling `os._exit(1)` on
failure will:

- Force immediate process termination (skipping Python cleanup, killing C-ext threads)
- systemd sees exit code != 0 → triggers `Restart=always` after `RestartSec=15`
- If broker recovers between retries, trader self-heals
- If broker stays down past `StartLimitBurst` (default 5 in 10s), systemd gives up cleanly
  → trader stays in `failed` state, NOT zombie. Sean can `systemctl reset-failed && start`
  when broker recovers, OR add `StartLimitIntervalSec=0` for indefinite retry (deferred)

## 3. Design

### 3.1 Code change (main.py)

**Current (line 47-49)**:
```python
else:
    trader.start()
    strategy.start()
```

**Proposed**:
```python
else:
    # Wrap startup in try/except: any failure here (Login failed, contract
    # resolve failed, etc.) MUST cause the process to exit, not propagate as
    # uncaught exception. The unitrade SDK C-extension keeps the process PID
    # alive after Python main dies, creating a zombie that systemd cannot
    # detect or restart. os._exit(1) terminates immediately (C-ext threads
    # die with the process), so systemd sees a clean failure and Restart=
    # always kicks in. See [[feedback-trader-weekend-restart-zombie]] for the
    # 2026-05-23 incident that motivated this fix, and 2026-05-27 05:47 for
    # the reproduction during dawn-break broker outage.
    try:
        trader.start()
        strategy.start()
    except Exception as e:
        logging.exception(f"trader/strategy startup failed — exiting for systemd restart: {e}")
        import os as _os
        _os._exit(1)
```

### 3.2 Why `os._exit(1)`, not `sys.exit(1)`

`sys.exit()` raises `SystemExit`, which **can be caught by other except blocks**
and which **runs cleanup handlers** (atexit, __del__, etc.). Both behaviors are
problematic here:

- Cleanup handlers could call back into SDK C-ext, potentially deadlocking
- SDK C-ext threads might intercept SystemExit propagation in their own handlers

`os._exit(N)` is a direct `_exit(2)` syscall — immediate, no cleanup, no chance
for C-ext to survive. Exactly what we need for the zombie escape.

### 3.3 DRY_RUN path unchanged

The DRY_RUN branch (`strategy.start()` only) is dev/test mode. Failure there is
already visible to the developer running it. No production zombie risk. Leave
unchanged to keep diff minimal.

### 3.4 Logging

`logging.exception(...)` captures traceback automatically. This appears in
journal as a multi-line entry with the full exception chain — preserves
diagnostic info before exit.

## 4. Test plan

### 4.1 Unit-testability — limited

`main.py` is a module-level entry point that imports `trader` (which connects
to broker on import). Hard to unit-test in isolation without mocking entire
SDK. Defer dedicated unit test.

### 4.2 Import sanity

```bash
cd /home/ubuntu/uni-auto-trader-v1
python3 -c "import main"  # in DRY_RUN mode
```

Should not raise. (The module-level code runs `strategy.start()` in DRY_RUN,
which spawns a thread but returns; import completes.)

### 4.3 Manual exception injection (local-only sanity)

Stand-alone proof:

```python
# in repl
import os, logging
logging.basicConfig(level=logging.INFO)

try:
    raise RuntimeError("Login failed: test")
except Exception as e:
    logging.exception(f"trader/strategy startup failed — exiting for systemd restart: {e}")
    os._exit(1)
```

Verify: traceback logged + process exits with code 1. Run in subshell to test.

### 4.4 Live verification (passive)

After deploy, normal boot (broker alive) = identical behavior to before. Only
failure path is changed. So:

- Smoke test (post-deploy): `systemctl status uni-trader.service` shows
  `active running`, journal shows normal startup (`Logged in`, `Subscribed`,
  `MTXStrategy started`)
- Failure path: passively observe on next broker outage. Expected:
  - Trader exits with code 1 (instead of zombie)
  - systemd auto-restarts after `RestartSec=15`
  - If broker still down, exits again, retries, eventually stops after
    `StartLimitBurst` if outage prolonged

5/29 05:32 expected dawn-break outage is the first natural test point.

## 5. Deployment plan

Standard break-window deploy per [[feedback-trader-service-precheck-sop]]:

1. **Pre-flight**: `bash 000_Agent/scripts/trader-precheck.sh` — exit 0 required.
   With today's Gate 2 redesign (journal-heartbeat), the broker probe will
   PASS correctly (no false negative).
2. **Backup**: `cp main.py main.py.bak.$(date +%Y%m%d-%H%M)`
3. **scp** updated `main.py` + sha256 dual-end verify
4. **systemctl restart**
5. **Boot verify**: normal startup logs (login OK, MTX restored N, no traceback)

Deploy window: **2026-05-28 13:45–15:00 TW** (day session close → night open
break). Mtx flat expected (close out + ~15 min wind-down).

## 6. Verification post-deploy

Immediate (within minutes):
- `systemctl is-active uni-trader.service` = active
- `journalctl -u uni-trader.service --since "30 sec ago"` shows clean boot:
  `Logged in`, `Subscribed`, `MTXStrategy started`, no traceback, no `os._exit`
  log line (would only appear on failure path)
- mtx_state.json present and parseable

Longer-term (passive):
- 5/29 05:32 (expected next broker outage) — observe whether trader exits +
  restarts cleanly, or stays zombie (regression)

## 7. Rollback

Zero-downtime via git revert:

```bash
git revert <commit-sha>
# scp main.py back to VPS
# systemctl restart uni-trader.service
```

Or surgical revert via backup:
```bash
ssh uni-trader 'cp /home/ubuntu/uni-auto-trader-v1/main.py.bak.YYYYMMDD-HHMM \
                   /home/ubuntu/uni-auto-trader-v1/main.py && \
                sudo systemctl restart uni-trader.service'
```

## 8. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| `os._exit(1)` skips logger flush — last log line may be lost | Low | `logging.exception(...)` writes synchronously before os._exit; full traceback usually flushed |
| systemd `StartLimitBurst=5/10s` may give up too fast during sustained outage (3h+) | Medium | Accept — failed state is better than zombie. Sean can `reset-failed && start` post-outage. Optional follow-up: tune `StartLimitIntervalSec=0` |
| C-ext threads might block before `_exit(2)` syscall lands | Low | `_exit(2)` is OS-level, no Python/C-ext can intercept |
| Already-running trader (no recent failure) sees no behavior change at deploy | Low (this is the point) | Verify via smoke test |
| `strategy.start()` also wrapped — could mask strategy bugs as "zombie prevention" | Medium | logging.exception preserves full traceback; bugs still visible in journal |

## 9. Acceptance criteria

Pre-deploy:
- spec reviewed
- import sanity passes
- code change is surgical (3 lines added + 1 try block + 1 except block)

Post-deploy (immediate):
- normal boot path works identically to before
- service `active running`, mtx_state restored, monthly restored, MTX strategy started
- no spurious `os._exit` log line on healthy boot

Post-deploy (validated, after 1-2 broker outages):
- On next broker outage: trader exits cleanly with code 1 (NOT zombie)
- systemd auto-restarts; if broker recovers → trader self-heals
- No more manual `pkill -9` needed during broker outages

## 10. Decisions for Sean

- [ ] **GO / NO-GO on deploy in 13:45-15:00 break window today?**
- [ ] Optional follow-up: tune systemd `StartLimitIntervalSec=0` for indefinite
      retry during long outages (separate change to `/etc/systemd/system/uni-trader.service`)

## 11. References

- `main.py` lines 47-49 (current code)
- `trader.py` lines 22-28 (start method), 49/53/67/70/77 (RuntimeError raises)
- [[feedback-trader-weekend-restart-zombie]] — 5/23 root incident + 5/27 reproduction
- [[feedback-trader-service-precheck-sop]] — precheck.sh Gate 2 journal-heartbeat redesign (sibling fix that prevents restart INTO zombie)
- [[project-pnl-calc-contract-mixing]] — Bug#3 restore-fix (different layer, prevents zombie ON restore; this spec prevents zombie ON startup)
