"""Pure helpers for handling broker order rejections.

When the broker rejects an order (e.g. FUF1239 margin, FUF0092 no-position), the SDK
delivers a `reply` with that status but NO `match` event — so the strategy's optimistic
unit (appended at placement in _open_unit) never gets a fill and would otherwise linger
as a phantom, later recording phantom P&L on a phantom close. These helpers let the
strategy FIFO-roll-back that unit. Pure + fully unit-tested; the strategy holds the lock.
"""
from __future__ import annotations

from typing import Optional


def is_reject_status(status: Optional[str]) -> bool:
    """True if a broker reply status means 'rejected, no fill'. Reject codes start with
    'FUF' (FUF1239 margin-exceeded, FUF0092 no-position-to-close). '委託成功' (accepted)
    and '完全成交' (filled) are NOT rejections."""
    return bool(status) and status.strip().startswith("FUF")


def rollback_rejected_entry(pending_fills: list, units: dict,
                            productid: str, bs: str, our_product: str) -> Optional[dict]:
    """Undo an optimistically-recorded entry whose broker order was rejected.

    FIFO-matches the rejection to the FRONT pending fill (mirrors on_fill's discipline):
    only acts when it is an ENTRY for our product and matching bs. Mutates `pending_fills`
    (pops the entry) and `units` (removes the unit). Returns the removed unit, or None on
    a safe no-op (foreign product, bs/kind mismatch, empty queue). Caller holds the lock.
    """
    if productid != our_product:
        return None
    if not pending_fills or pending_fills[0]["bs"] != bs:
        return None
    if pending_fills[0].get("kind") != "entry":
        return None
    pend = pending_fills.pop(0)
    unit = pend["unit"]
    src_units = units.get(unit["source"], [])
    if unit in src_units:
        src_units.remove(unit)
    return unit
