"""Per-signal-code demote skip for MTX signals (trader-side).

Pure function — no SDK / network / strategy imports, so unit-testable on
system python3 (`python3 -m unittest test_demote_gate`).

Spec: docs/superpowers/specs/2026-06-13-mtx-code2-demote-design.md

Rule: when a Worker MTX signal's code is in the configured demote set
(env MTX_DEMOTE_CODES, e.g. "2"), the trader silent-absorbs the entry
(no order, no unit) across ALL sessions and BOTH directions. The Worker
keeps firing it into signal_history as a paper record. Demote = the
signal's real-money expectancy is negative; remove the risk, keep the
data. First demoted code: ② 突破進場 (chronic soft bleed, real-fill
mean -22.9/trade, 90% CI [-41, -4.8]). See memory
project-per-signal-live-paper-review.

Fail-open: an invalid / missing sig_code never demotes.
"""


def should_demote(sig_code, demote_codes):
    """Return True iff this signal's code is in the demote set.

    Args:
        sig_code:     signal code from Worker (int, or numeric str like "2");
                      None / non-numeric / bool / float → fail-open (no demote)
        demote_codes: a set/frozenset of int codes to demote (empty = disabled)

    Returns:
        bool — True only when sig_code coerces to an int that is a member of
        demote_codes. bool is explicitly excluded (True==1 would otherwise
        match a {1} set).
    """
    if not demote_codes:
        return False  # disabled (env unset / empty)
    if isinstance(sig_code, bool):
        return False  # bool is an int subclass — never treat as a code
    if isinstance(sig_code, int):
        return sig_code in demote_codes
    if isinstance(sig_code, str):
        s = sig_code.strip()
        if s.lstrip("-").isdigit():
            return int(s) in demote_codes
        return False  # non-numeric string → fail-open
    return False  # float / None / anything else → fail-open
