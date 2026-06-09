# Entry-filter OOS re-verification — scheduled run 2026-06-10 (full 6/1–6/9 window)

**Verdict: NO-GO on both gates.** Pure offline backtest, READ-ONLY, no production / live-trader / env changes.

## Gates tested
`short×night` and `③×all` (the two empirically-correct survivors that prior runs redirected to), via `gate.py`.

Data: `400_Outputs/trader_archive/2026-06-10/trades.jsonl` (cumulative June, synced 05:30 today),
`source=='mtx'`, break rows excluded; worker joined for signal fields only. Metric `pnl_pts`
(signal-based, slippage-inclusive). Window: **OOS 6/1–6/9, n=75 real fills, 7 trading days**
(6/6–6/7 are weekend → 6/8, 6/9 present). CI = 10k-resample bootstrap of subset mean, 90%.
Gate passes iff CI-upper < 0 (subset reliably net-negative) AND LODO sign-stable AND same-sign as IS lean.

## Results (alt-cut matrix)

| cut | n | WR% | sum | mean | CI90 of mean | LODO flips | verdict |
|---|---|---|---|---|---|---|---|
| ③ × all | 6 | 33 | −41 | −6.8 | [−62.8, **+44.2**] | 2/4 | **NO-GO** (THIN, CI-upper>0, LODO-fragile) |
| ③ × night | 4 | 25 | +34 | +8.5 | [−23.2, +42.5] | 1/2 | NO-GO (THIN, positive) |
| ③ × day | 2 | 50 | −75 | −37.5 | [−148, +73] | 1/2 | NO-GO (THIN n=2) |
| short × all | 28 | 46 | −559 | −20.0 | [−49.8, **+10.1**] | 0/7 | NO-GO (CI-upper>0) |
| **short × night** | **14** | **43** | **−81** | **−5.8** | [−31.5, **+20.9**] | 1/5 | **NO-GO** (THIN, CI-upper>0, LODO-fragile) |
| short × day | 14 | 50 | −478 | −34.1 | [−87.4, **+19.9**] | 0/7 | NO-GO (THIN, CI-upper>0) |

## Per-gate read against the strict OOS bar

**`short×night` — NO-GO.** Fails 3 of 4 bar conditions:
1. CI-upper = **+20.9 > 0** (not reliably net-negative). FAIL.
2. n=14 < THIN_N=15 → underpowered; LODO 1/5 flips → single-day-driven. FAIL.
3. **⑥-contamination confirmed and total.** Composition: ③ +34 (n=4), ④ +39 (n=7), ⑥ **−154 (n=3)**.
   The entire −81 net is the 3 ⑥ (night-short) trades — and then some. **Strip ⑥ and short×night = +73 (n=11).**
   The gate is 100% mis-attributing ⑥'s loss to the dir×session cut; a blanket gate would block the
   profitable ③④ night-shorts. Identical pathology to 6/6, now sharper (then ⑥ = 56% of bleed; now ⑥ > 100%).
4. The real short-side bleed this window is **day** (−478, n=14), the *opposite* session from the gate's thesis.

**`③×all` — NO-GO.** Fails the bar at condition 1 and 2:
1. CI-upper = **+44.2 > 0**, mean −6.8 only (vs −19.0 in the 6/6 full window). FAIL.
2. n=6 (THIN), LODO 2/4 flips → not sign-stable. FAIL.
   Net collapsed toward flat; ③ has no reliable OOS bleed at the all-session level.

## Why NO-GO (summary)
- Neither candidate clears CI-upper < 0; both straddle zero with positive upper bounds.
- Both are THIN (③×all n=6, short×night n=14) → n=1-day discipline says not actionable.
- `short×night` net is **entirely ⑥-driven** (−154 from 3 trades); the dir×session dimension itself is
  net-positive (+73) once the known-NO-GO ⑥ signal is removed.
- The short bleed concentrates in **day** (−478), contradicting the night thesis the candidate was built on.

## Caveats
- `pnl_pts` is signal-based and under-reports ([[feedback-real-pnl-orders-not-trades-jsonl]]); real
  orders.jsonl FIFO would push subsets *more* negative — but the rejection is about **composition &
  stability** (CI straddles zero, ⑥-single-signal-driven, LODO-fragile, THIN), which a more-negative
  -but-noisier real series doesn't fix. Subset-level real FIFO remains infeasible on shared-queue orders.jsonl.
- Despite being the full scheduled window, OOS n is only 75 (vs ~169 in the 5/22–6/5 full window): June
  traded thin and deployed controls (HALF_SIZE ③④, ④×ATR-skip) already removed fills. `short×night`
  still sits at n=14 — the THIN problem the 6/10 date was meant to fix did **not** resolve.
- Phantom diagnostic: 67 worker-only closed rows dropped (82% ③/④ short) — clean-recon working as designed.

## Decision / next
- **Confirms 5/31 + 6/6 for the third time: dir×session has no reliable edge.** Closing the dir×session
  research line. The bleed is signal-specific (night ⑥), not a "all night shorts" dimension.
- The only live thread is an **isolated ⑥ (night-short) gate**, but it still fails its own counter-example
  bar from [[project-short-signal-regime-gate-nogo]]: needs ≥20 trades × ≥3 independent up-sessions +
  holdout CI-upper < 0. Current OOS ⑥-night n=3 → **not-yet, far from powered.**
- No production change justified. No skip-filter spec to draft.

Result files (this dir): `gate_altcut_matrix_oos.csv`, `gate_candidates_oos.csv`, `gate_realfill_oos.csv`.
