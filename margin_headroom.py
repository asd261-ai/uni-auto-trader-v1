"""Shared-account margin-headroom predicate.

Account 0239174 is shared by the uni-trader bot and Sean's manual trades.
When Sean's manual positions consume the account's available margin, the bot's
MXF orders get rejected by the broker with FUF1239 ("未沖銷部位及委託保證金超過
使用額度"). This pure predicate decides whether the broker's available
order-excess margin (DMargin.twdordexcess) has dropped below the headroom the
bot needs to keep operating, so the caller can alert Sean to top up / trim.

Pure + side-effect free → unit-testable. Fail-SAFE by design: a bad / missing
read never reports "low" (we never want a false starvation alert).
"""
from __future__ import annotations

from typing import Optional


def headroom_low(ordexcess: Optional[float], min_twd: Optional[float]) -> bool:
    """Return True iff available order-excess margin is below the required headroom.

    Args:
        ordexcess: broker's available order-excess margin in NT$
                   (DMargin.twdordexcess), or None if unavailable.
        min_twd:   headroom floor in NT$ (env MARGIN_HEADROOM_MIN_TWD).

    Fail-safe rules (never alert on ambiguity):
      - min_twd is None or <= 0   → feature disabled        → False
      - ordexcess is None         → bad/no broker read      → False
      - ordexcess not numeric     → schema drift            → False
      - otherwise                 → ordexcess < min_twd
    """
    if min_twd is None:
        return False
    try:
        floor = float(min_twd)
    except (TypeError, ValueError):
        return False
    if floor <= 0:
        return False
    if ordexcess is None:
        return False
    try:
        return float(ordexcess) < floor
    except (TypeError, ValueError):
        return False
