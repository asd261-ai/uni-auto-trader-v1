"""Pure helper: classify a stop-hit exit as trailing-take-profit vs stop-loss.

A unit's stop is trailed in place (strategy.py overwrites unit["stop"] as price
moves favorably), so the original stop is not retained. At a stop hit, the stop's
side relative to entry tells us which kind of exit it is -- equivalent to the exit
pnl sign because exit_price ~= stop at a stop hit. Mirrors the Worker's isTrailing
semantics (worker/index.js).
"""


def stop_hit_reason(direction: str, stop: float, entry: float) -> str:
    """Return "trail" if the stop has been trailed onto entry's profit side,
    else "loss". Breakeven (stop == entry) counts as "loss". A missing/zero
    entry degrades to "loss" (old behavior) rather than raising."""
    if not entry:
        return "loss"
    if direction == "long":
        return "trail" if stop > entry else "loss"
    return "trail" if stop < entry else "loss"
