# MTX Code-4 High-ATR Skip — Spec

- **Status**: DRAFT (2026-05-27), not deployed, awaiting Sean go/no-go
- **Driver memory**: `[[project-trader-slippage-analysis]]`
- **Related**: `[[project-mtx-loss-controls]]` (HALF_SIZE_CODES coexistence), `[[feedback-4-5month-oos]]` (sample sufficiency rule)

## 1. Problem

5/22–5/27 slippage deep-dive (6 trading days, 89 trades total, 15 ④ trades)
identified ④ 轉弱賣出 short × High-ATR (>58) as the single largest source
of real-cash leakage:

- ④ overall: 16 trades, entry slip mean +9.4 pts, sum +150 pts (Task A
  pairing yielded n=15 with broker fills present)
- ④ × High-ATR (>58): n=7 (of the 15 paired), entry slip mean **+18.7 pts**,
  contributing **87%** of ④'s total entry slip
- Counterfactual: skipping ④ when ATR > 58 (historical, n=6 after re-pair)
  improves 6-day broker PnL by **+348 pts** (≈ NTD +17,400)

Other codes are **not** suitable for the same gate:
- ⑧ ATR>58 skip: **−406** (high-ATR ⑧ are mostly winners)
- ③ ATR>58 skip: **−75** (③ × High-ATR is favorable, see Task B)

The fix must be **code-specific to ④**.

## 2. Hypothesis

- ④ trigger (`!aboveMA20 && hist<0 && 30<rsi<45`) fires while the market is
  already in momentum-down state. In High-ATR (high-volatility) regimes
  this amplifies fill slippage on market sell entry and increases the
  chance of momentum exhaustion / sharp reversal against the position.
- Skipping ④ in High-ATR environments restricts the signal to Low/Mid-ATR
  conditions where entry fills are clean (Task D: Low-ATR entry slip
  mean −0.2).
- Expected effect (per Task D + Task A backtest): **net P&L improvement
  of ~+58 pts per skipped High-ATR ④ trade** over the backtest sample.
  Sample variance is large; statistical confidence is weak (n=6 trigger
  events in 6 days).

## 3. Design

### 3.1 Env var (new)

- `MTX_SKIP_CODE_4_ATR_GT`: integer, optional, **default unset**
  - **Unset** (or empty / ≤0): no behavior change. Backwards-compat.
  - **Set to N**: when a ④ signal would open a unit, check the signal's
    `atr` field (provided by Worker `/api/history`). If `atr > N`,
    log + skip (do not call `_open_unit`).
- Recommended initial value: `58` (D-analysis threshold).
- Adjustable without redeploy (env-only).

### 3.2 Integration point

`strategy.py` — entry guard, **before** `_open_unit()`, at the same
logical position as existing `HALF_SIZE_CODES` skip-alternate logic.

Pseudocode (subject to actual code review):

```python
if signal["source"] == "mtx" and signal["sigCode"] == 4:
    skip_atr = int(os.getenv("MTX_SKIP_CODE_4_ATR_GT", "0") or "0")
    sig_atr = signal.get("atr", 0)
    if skip_atr > 0 and sig_atr > skip_atr:
        logger.info(
            f"MTX code-4 ATR-gated skip | atr={sig_atr} > threshold={skip_atr} "
            f"id={signal['id']} entry={signal['entry']}"
        )
        return  # do not open unit
```

### 3.3 Coexistence with `HALF_SIZE_CODES=3,4`

The existing HALF_SIZE_CODES skip-alternate logic ([[project-mtx-loss-controls]])
operates **independently**. Order of evaluation:

1. ATR-gate check (`MTX_SKIP_CODE_4_ATR_GT`) — full skip if triggered
2. HALF_SIZE_CODES — skip-alternate (≈50%) if not already full-skipped

A High-ATR ④ skipped by gate (1) never reaches gate (2). HALF_SIZE
counter is **not** incremented for ATR-gate skips (they are not signal
events, just suppressions).

Both env vars are independent; either can be disabled without touching
the other.

### 3.4 Logging

- INFO level: `MTX code-4 ATR-gated skip | atr=X > threshold=Y id=Z entry=P`
- **Telegram notification (2026-05-28 PM update)**: each skip fires
  `🚫 ATR Skip | ④ short atr=X > Y\nentry=P id=Z` via `_safe_health_notify`
  (Health Bot channel, separate from MTX_Monitor trade stream). Threaded
  send to avoid blocking poll loop. Rationale: Phase 2 observation period
  benefits from real-time visibility to catch unexpected fire patterns.
  May downgrade to session-summary after ≥6-week promotion review (~7/8).
- No state file change (skip = no unit opened = no `mtx_state.json` write).
- Skip events are derivable from journal grep for post-hoc analysis.

### 3.5 Data source for ATR

ATR comes from the Worker signal payload (`/api/signals` or signal_bus
consumption path). Worker computes ATR over the trailing 14 bars at
5m resolution. Trader already receives this field; no new fetch needed.

If `atr` is missing (`None` or absent) on a signal: **fail open**
(do not skip). This avoids accidentally killing all ④ signals on a
Worker bug.

## 4. Test plan

Unit tests (`tests/test_atr_gated_skip.py`, new file):

| Test | Input | Expected |
|------|-------|----------|
| Env unset | sigCode=4, atr=100, env not set | open unit (no skip) |
| Env=58, atr=60 | sigCode=4, atr=60 | **skip**, log INFO |
| Env=58, atr=58 | sigCode=4, atr=58 | open unit (boundary `>` not `≥`) |
| Env=58, atr=50 | sigCode=4, atr=50 | open unit |
| Wrong code | sigCode=3, atr=100, env=58 | open unit (gate only affects ④) |
| Wrong code | sigCode=8, atr=100, env=58 | open unit |
| Missing atr | sigCode=4, atr=None, env=58 | open unit (fail open) |
| Bad env | env="abc" | open unit (parse fallback to 0 = disabled) |

Integration:
- Smoke test on live trader after deploy: confirm first ④ signal logs
  correctly given current env setting.

## 5. Deployment plan

Standard break-window deployment (per [[feedback-trader-service-precheck-sop]]):

1. Pre-flight: `bash 000_Agent/scripts/trader-precheck.sh` — exit 0 required.
2. Set env: edit `/home/ubuntu/uni-auto-trader-v1/.env` to add
   `MTX_SKIP_CODE_4_ATR_GT=58` (or leave unset for staged rollout —
   deploy code first, env later).
3. scp updated `strategy.py` + sha256 verify both ends.
4. `sudo systemctl restart uni-trader.service`.
5. Boot verify: login OK, MTX restored N, no traceback, env loaded.

Suggested staging:
- **Phase 1** (1 week): deploy code, leave env unset. Verify no behavior
  change (regression test).
- **Phase 2** (subsequent): set env=58. Observe for ≥2 weeks (≥30 ④
  signals expected) before re-evaluating threshold.

## 6. Verification (post-deploy)

- First ④ signal with atr > threshold: confirm INFO log line, no order,
  no unit in mtx_state.
- First ④ signal with atr ≤ threshold: confirm normal open path
  (Entry fill log, order sent, unit added).
- Non-④ signals: no change.
- Daily skip count from `journalctl -u uni-trader.service --since today
  | grep "ATR-gated skip" | wc -l`.

## 7. Rollback

Two paths, both zero downtime:
1. `unset MTX_SKIP_CODE_4_ATR_GT` in .env + restart → behavior reverts to
   pre-change instantly.
2. Set `MTX_SKIP_CODE_4_ATR_GT=999` to effectively disable (any ATR will
   pass the threshold check).

Code itself is small and surgical; full code revert (if needed) is a
clean `git revert` of the implementing commit.

## 8. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Sample insufficient** (n=6 trigger events in backtest) | High | Phase 1 = no-op deploy first; Phase 2 observation ≥30 signals |
| **Curve-fit to 5/22-5/27 regime** | High | Single 6-day window; market regime may shift. Re-evaluate threshold at ~6/22 with 2x sample. |
| **Missed winners** | Medium | Backtest: 2/6 skipped ④ were winners (+39, +36 = +75 pts forgone). Net lift still +348 |
| **ATR scale shift** | Medium | ATR>58 reflects current vol level. If TX moves to higher/lower vol regime, threshold needs adjustment. Env-var design handles this without redeploy |
| **Worker atr field missing or buggy** | Low | Fail-open semantics: missing atr → no skip → fall through to normal entry |
| **Interaction with HALF_SIZE_CODES** | Low | Independent gates, deterministic order: ATR-gate first, HALF_SIZE second. Documented behavior |

## 9. Acceptance criteria

Pre-deploy (Phase 1 — code only, env unset):
- All unit tests pass
- Trader boots cleanly post-restart, no regression on any code path
- 1 week of no-op operation confirms backwards-compat

Pre-deploy (Phase 2 — env set):
- First ATR-gated ④ skip event observed in journal
- All other code paths confirmed unaffected
- 2+ weeks observation with ≥30 ④ signals fired
- Re-run `000_Agent/scripts/slippage-analyze.sh` shows ④ × High-ATR
  bucket is empty (suppressed) and overall ④ net P&L improved

Promotion to "validated" (≥6 weeks live, per [[feedback-4-5month-oos]]):
- ≥30 ④ signals total observed
- Skip count consistent with Worker ATR distribution
- Real-cash P&L improvement vs pre-deploy baseline (compute via FIFO
  difference of orders.jsonl periods)

## 10. Open questions / Decisions for Sean

- [ ] **GO / NO-GO on this spec?**
- [ ] If GO: deploy Phase 1 (no-op) immediately or wait?
- [ ] If Phase 2: env threshold = 58 (recommended) or different value?
- [ ] Coexist with HALF_SIZE_CODES=3,4 as designed, or remove HALF_SIZE
      for ④ since ATR-gate is more selective?

## 11. References

- Slippage analysis: `400_Outputs/slippage-reports/2026-05-27/report.txt`
- D analysis (sigCode × ATR cross): `/tmp/slip-analysis/analyze_d.py`
- A counterfactual (wait-retest falsified): `/tmp/slip-analysis/analyze_a.py`
- ATR-skip backtest: `/tmp/slip-analysis/analyze_atr_skip.py`
- HALF_SIZE_CODES precedent: `[[project-mtx-loss-controls]]`
- Worker signal source: `taiwan-mini-futures/worker/index.js:772-774`
  (④ trigger) + line 840-855 (`calcLevels`)
- Trader entry path: `uni-auto-trader-v1/strategy.py` (search HALF_SIZE_CODES
  for insertion point)
