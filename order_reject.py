"""Pure helpers for handling broker order rejections.

When the broker rejects an order (margin FUF1239, no-position FUF0092, time TTO0001,
market-ROD HHO0038, …) the SDK delivers a `reply` with that status but NO `match` (fill)
event. The strategy's optimistic unit (appended at placement in _open_unit) then lingers
as a phantom and later books phantom P&L. These helpers let the strategy roll back that
unit. Pure + fully unit-tested; the strategy holds the lock.

orderno-keyed matching is INFEASIBLE here: orderno is broker-assigned and first appears in
the async reply (never at placement), with no client correlation token. So we identify the
rejected unit conservatively — by side + unfilled + uniqueness — and BAIL on any ambiguity,
leaving genuine drift to the broker reconciliation safety net.
"""
from __future__ import annotations

from typing import Optional

# Broker reject-code families (verified against production reply archives). Success
# statuses (委託成功/完全成交/刪單成功/改價成功) are Chinese and never match these.
_REJECT_PREFIXES = ("FUF", "TTO", "HHO")


def is_reject_status(status: Optional[str]) -> bool:
    """True if a broker reply status means 'rejected, no fill'."""
    return bool(status) and status.strip().startswith(_REJECT_PREFIXES)


def rollback_rejected_entry(pending_fills: list, units: dict,
                            productid: str, bs: str, our_product: str) -> Optional[dict]:
    """Undo an optimistically-recorded ENTRY whose broker order was rejected.

    Conservative, ambiguity-averse matching (caller holds the strategy lock):
      - ignore foreign contracts (manual trades in other products);
      - if a same-side EXIT is pending, the reject may be for that close (e.g. FUF0092
        no-position, or a same-side reversal close) → bail;
      - candidates = unfilled (entry_fill is None) ENTRY orders on this side; a reject
        produces no fill, so the rejected order is one of these;
      - act only when EXACTLY ONE candidate exists, else bail (e.g. two unfilled same-side
        entries in flight) and leave it to recon.
    Mutates pending_fills + units; returns the removed unit, or None on a safe no-op.
    """
    if productid != our_product:
        return None
    if any(p.get("kind") == "exit" and p.get("bs") == bs for p in pending_fills):
        return None
    candidates = [
        p for p in pending_fills
        if p.get("kind") == "entry" and p.get("bs") == bs
        and p.get("unit", {}).get("entry_fill") is None
    ]
    if len(candidates) != 1:
        return None
    pend = candidates[0]
    unit = pend["unit"]
    pending_fills.remove(pend)
    src_units = units.get(unit["source"], [])
    if unit in src_units:
        src_units.remove(unit)
    return unit
