"""Pure helper: sanity-bound Worker-supplied exit prices.

No SDK/strategy imports -> unit-testable on system python3:
    python3 -m unittest test_exit_sanity

Why this exists (defense-in-depth, 2026-07-09 dirty-fill root-cause): on
2026-06-30 the Worker's fill_anchor.js null-guard bug polluted `target` into
tiny slip-deltas (4/-8/15) and strategy's profit-exit path wrote them into
trades.jsonl unchecked, fabricating |entry|-sized phantom P&L on 30 rows. The
Worker bug is fixed (commit 23dc740 / eceb0db6); this is the trader-side second
line so ANY future Worker-side price pollution is capped at ≈0-pnl noise plus a
warning, never a poisoned ledger.
"""


def sane_exit_price(candidate, entry, max_pts):
    """Return (price, ok) for a Worker-supplied exit price.

    ok=False when candidate sits implausibly far (> max_pts) from our own entry
    -> caller uses the returned fallback (entry, ≈0 pnl) and warns. None
    candidate or missing entry passes through untouched (existing null-handling
    paths own those cases; inventing a fallback there would mask real gaps).
    """
    if candidate is None or entry is None:
        return candidate, True
    if abs(candidate - entry) > max_pts:
        return entry, False
    return candidate, True
