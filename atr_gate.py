"""ATR-gated entry skip for MTX code-4 (④ 轉弱賣出 short).

Pure function — no SDK / network / strategy imports, so unit-testable on
system python3 (`python3 -m unittest test_atr_gate`).

Spec: docs/superpowers/specs/2026-05-27-mtx-skip-code4-high-atr.md

Rule: when MTX_SKIP_CODE_4_ATR_GT > 0 and a ④ signal's ATR exceeds it
**AND the current session is 'night'**, trader silent-absorbs the
entry (no order, no unit). Day-session ④×High-ATR continues to trade
(2026-05-28 refinement: 13-event counterfactual showed day-side
ATR>58 trades net +78 pts of edge if not skipped). Other codes
unaffected. Missing ATR / non-night session → fail-open (no skip).
"""


def should_skip_code4_atr(sig_code, sig_atr, threshold, session):
    """Return True iff this signal should be ATR-gated-skipped.

    Args:
        sig_code:  signal code (int) from Worker; 4 = 轉弱賣出 short
        sig_atr:   ATR value from Worker (numeric) or None
        threshold: skip-above threshold; ≤0 or non-int = gate disabled
        session:   current trading session ("day" | "night" | "break" |
                   None); only "night" triggers the skip (per
                   2026-05-28 night-only refinement)

    Returns:
        bool — True only when ALL of:
          - threshold is a positive integer
          - sig_code == 4
          - session == "night"
          - sig_atr is numeric AND sig_atr > threshold
    """
    if not isinstance(threshold, int) or threshold <= 0:
        return False  # gate disabled (backwards-compat)
    if sig_code != 4:
        return False  # code-specific to ④
    if session != "night":
        return False  # night-only refinement (2026-05-28)
    if not isinstance(sig_atr, (int, float)) or isinstance(sig_atr, bool):
        return False  # fail-open on missing / invalid atr
    return sig_atr > threshold
