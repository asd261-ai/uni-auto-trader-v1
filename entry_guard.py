"""Entry guard: skip a market entry when the live market is already at/through the trade's
target — the reward is gone before we enter, so a market order there can only break even or
lose. Structural RR≤0 check, NOT a fitted statistical edge: entering past your own target is
never +EV for that trade's plan.

Surfaced 2026-06-08 (FVG gap-open pathology): at 09:56 a long filled 43121 (slip +214) already
past its 43017 target → instant 'target hit' close, +6 real vs +214 signal-fiction. Across the
5/22–6/5 archive this class was 6/33 FVG trades (18%), Σsignal +304 vs Σreal −148. MTX: 0/164
(clean — its fills don't land past target), so the guard is a no-op for MTX.

Pure + defensive: any missing/non-numeric input returns False (never block on bad data).
"""
from typing import Optional


def entry_past_target(direction: str, price: Optional[float], target: Optional[float]) -> bool:
    """True ⇒ SKIP the entry: the live `price` is already at/through `target` for `direction`.

    long  → price >= target (already at/above the take-profit → reward gone)
    short → price <= target (already at/below the take-profit → reward gone)

    Defensive: missing/non-numeric price or target returns False (never block on bad data —
    a guard must fail open so a feed glitch can't silently halt all entries).
    """
    if price is None or target is None:
        return False
    try:
        p = float(price)
        t = float(target)
    except (TypeError, ValueError):
        return False
    if direction == "long":
        return p >= t
    if direction == "short":
        return p <= t
    return False
