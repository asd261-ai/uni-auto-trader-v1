"""Pure helpers for handling broker order rejections.

When the broker rejects an order (margin FUF1239/PSC0019, no-position FUF0092,
time TTO0001, market-ROD HHO0038, …) the SDK delivers a `reply` with that status but NO `match` (fill)
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
# PSC added 2026-07-18: margin-insufficiency rejects (PSC0019 保證金不足) observed
# live on 2026-07-17 night — missing from this list, so rollback never fired and
# four rejected entries lingered as phantom trades.
_REJECT_PREFIXES = ("FUF", "TTO", "HHO", "PSC")


def is_reject_status(status: Optional[str]) -> bool:
    """True if a broker reply status means 'rejected, no fill'."""
    return bool(status) and status.strip().startswith(_REJECT_PREFIXES)


# Margin-insufficiency reject codes (shared-account starvation). Exact codes, not
# families: FUF0092 etc. are rejects but say nothing about margin.
_MARGIN_REJECT_CODES = ("FUF1239", "PSC0019")


def is_margin_reject(status: Optional[str]) -> bool:
    """True if a reject status specifically means insufficient margin.

    Drives an immediate Health-bot alert: the reject reply is the broker itself
    saying the account is starved, so it must not depend on the polling margin
    query succeeding (2026-07-17 night: query failed all night → watcher silent
    while four PSC0019 rejects sat in the log)."""
    return bool(status) and status.strip().startswith(_MARGIN_REJECT_CODES)


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


def rollback_rejected_exit(pending_fills: list, productid: str, bs: str,
                           our_product: str) -> Optional[dict]:
    """Undo a rejected EXIT (close) order whose broker order was rejected (e.g. FUF0092
    no-position) so its pend stops poisoning the FIFO. The caller finalizes the pend's
    deferred record to exit_fill=null immediately instead of waiting for the 60s timeout.

    Conservative, ambiguity-averse (caller holds the strategy lock):
      - ignore foreign contracts;
      - if a same-side UNFILLED entry also pends, the reject may be for that entry → bail
        (the entry-rollback path owns that case);
      - candidates = pending EXIT orders on this side; act only when EXACTLY ONE, else bail
        and leave drift to broker reconciliation.
    Mutates pending_fills; returns the removed exit pend (carrying its 'pe'), or None.
    """
    if productid != our_product:
        return None
    if any(p.get("kind") == "entry" and p.get("bs") == bs
           and p.get("unit", {}).get("entry_fill") is None for p in pending_fills):
        return None
    candidates = [p for p in pending_fills
                  if p.get("kind") == "exit" and p.get("bs") == bs]
    if len(candidates) != 1:
        return None
    pend = candidates[0]
    pending_fills.remove(pend)
    return pend
