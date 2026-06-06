# Entry-filter revisit — run EARLY 2026-06-06 (originally scheduled 6/10)

**Verdict: NO-GO on both gates.** Pure offline backtest, no production / live-trader changes.

## Gates tested
`short×night` and `③×all` (the two survivors of the 5/31 interim dry-run), via `gate.py`.

Data: `400_Outputs/trader_archive/2026-06-06/trades.jsonl`, `source=='mtx'`, break rows excluded.
Metric `pnl_pts` (signal-based, slippage-inclusive). Windows: FULL 5/22–6/5 (n=169, 11 td) and
OOS-only 6/1–6/5 (n=54, 5 td). CI = 10k-resample bootstrap of subset mean, 90%, **+ 50-seed
stability** to confirm the CI upper bound isn't seed luck. Gate passes iff CI-upper < 0 (subset
reliably net-negative) AND LODO sign-stable.

## Results

| window | gate | n | WR% | sum | mean | CI90-upper (seed range) | seeds CI-up<0 | LODO flips | verdict |
|---|---|---|---|---|---|---|---|---|---|
| FULL 5/22–6/5 | short×night | 38 | 45 | −574 | −15.1 | ≈0 (−0.76~+0.87) | **21/50** | 0/11 | **NO-GO** |
| FULL 5/22–6/5 | ③×all | 22 | 36 | −418 | −19.0 | +3.6~+7.4 (>0) | 0/50 | 0/8 | **NO-GO** |
| OOS 6/1–6/5 | short×night | 14 THIN | 43 | −81 | −5.8 | +19~+23 (>0) | 0/50 | 1/5 | NO-GO (THIN) |
| OOS 6/1–6/5 | ③×all | 5 THIN | 20 | −114 | −22.8 | +26~+30 (>0) | 0/50 | 1/5 | NO-GO (THIN) |

## Why NO-GO
- **`short×night` CI-upper sits ON zero**: only 21/50 seeds put it < 0. "CI-upper < 0" must be
  robust to the seed, not a single-seed −0.2. It isn't.
- **Loss is driven by 5 ⑥ trades**: of the −574 full-window loss, ⑥ = −320 (n=5), ④ = −117 (n=19),
  ③ = −137 (n=14). 56% of the bleed is 5 ⑥ signals — and ⑥ is already a known NO-GO/not-yet
  signal ([[project-short-signal-regime-gate-nogo]]). Strip ⑥ and ③④ turn POSITIVE in OOS
  (③ +34, ④ +39). A blanket dir×session gate would block those profitable ③④ too.
- **`③×all` CI-upper is reliably POSITIVE** (+3.6~+7.4) → skipping ③ throws away profit.

## Caveats
- `pnl_pts` is signal-based and under-reports ([[feedback-real-pnl-orders-not-trades-jsonl]]); real
  orders.jsonl FIFO would push subsets *more* negative — but the rejection here is about
  **stability/composition** (CI straddles zero, single-signal-driven, OOS flips), which a
  more-negative-but-noisier real series doesn't fix. Subset-level real FIFO isn't feasible on the
  current orders.jsonl (shared MXF queue, no trade-id/label); day/total real conclusions unaffected.
- OOS window is only 5 td (both gates THIN). The original 6/10 date exists precisely to reach
  6/1–6/9 OOS; running early can't fix the thin sample.

## Decision / next
- Confirms + strengthens the 5/31 interim: **dir×session has no reliable edge**. The bleed is
  signal-specific (night ⑥, plus ③④ on specific days), not a "all night shorts" dimension.
- Redirect the research line from dir×session to **single-signal**: an isolated ⑥ (night-short)
  gate, but it needs ≥20 trades × ≥3 independent up-sessions + holdout CI-upper < 0 (the ⑥
  counter-example bar from [[project-short-signal-regime-gate-nogo]]). Currently n=5 → not-yet.
- 6/10: re-run both gates on the full 6/1–6/9 OOS. GO requires CI-upper < 0 across ≥20 seeds AND
  zero LODO flips AND same-sign OOS.

Result files (this dir): `gate_altcut_matrix_full_5_22_to_6_5.csv`,
`gate_altcut_matrix_oos_6_1_to_6_5.csv`, `gate_robustness_2026-06-06.csv` (seed-stability main
table), `gate_realfill_full_5_22_to_6_5.csv`, `gate_realfill_oos_6_1_to_6_5.csv`.
