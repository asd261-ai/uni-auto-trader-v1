"""Boundary validation for broker SDK responses.

Principle: fail LOUD on schema drift; never silently default a missing field to
0. The 2026-05-16/05-22 recon false-alerts came from reading fields that did
not exist on the SDK object and silently getting 0 — which reads as "flat" and
fires a mismatch on every held position. A drifted shape must be distinguishable
from a genuinely flat account.
"""

# Sentinel: the SDK object did not match the expected shape. Callers must NOT
# interpret this as a flat position — they must skip the cycle and alert.
SCHEMA_FAIL = object()

_REQUIRED_POS_FIELDS = ("current_sell_open_position", "current_buy_open_position")
_MAX_SANE_QTY = 50  # net position never legitimately exceeds this


def parse_broker_position(p, product: str):
    """Map one broker DPosition object to {productid, bs, qty} | None | SCHEMA_FAIL.

    None     = this object is not our product, or our product is flat.
    SCHEMA_FAIL = the object lacks the expected open-position fields, or returns
                  out-of-range quantities — treat as drift, not as flat.
    """
    if getattr(p, "productid", "") != product:
        return None
    for f in _REQUIRED_POS_FIELDS:
        if not hasattr(p, f):
            return SCHEMA_FAIL
    sell = int(getattr(p, "current_sell_open_position", 0) or 0)
    buy = int(getattr(p, "current_buy_open_position", 0) or 0)
    if not (0 <= sell <= _MAX_SANE_QTY and 0 <= buy <= _MAX_SANE_QTY):
        return SCHEMA_FAIL
    if sell > 0:
        return {"productid": product, "bs": "S", "qty": sell}
    if buy > 0:
        return {"productid": product, "bs": "B", "qty": buy}
    return None


def parse_fill(bs, matchprice, matchqty):
    """Validate a broker Match's fields before they reach the P&L log / fill FIFO.
    Returns (price: float, qty: int) | None. None = malformed → caller rejects.

    Field-level only (type + sane range + valid side). Product filtering stays
    with the caller / on_fill — this just stops garbage (0/negative/NaN/inf price,
    bad side, non-numeric qty) from contaminating order_log P&L or the fill FIFO.
    """
    if bs not in ("B", "S"):
        return None
    try:
        price = float(matchprice)
        qty = int(matchqty)
    except (TypeError, ValueError):
        return None
    # price band catches 0, negative, NaN (NaN>0 is False) and inf (inf<bound False)
    if not (0 < price < 1_000_000):
        return None
    if not (0 < qty <= _MAX_SANE_QTY):
        return None
    return price, qty


def clean_feed(raw, max_items: int = 1000):
    """Validate an HTTP feed payload (MTX /api/history and FVG /api/signals share
    a trade-like dict shape). Returns (items, ok).

    ok=False  = payload was not even a list (e.g. an error object) — caller logs.
    items     = only dict entries carrying an int `id`, capped at max_items.

    Enforces just the list + int-id invariant the downstream cursor/sync logic
    actually depends on (`_last_seen_id`, age math on `id`). Deliberately does
    NOT enforce a status enum: MTX and FVG use different status sets, so that
    check belongs to each consumer, not this shared boundary.
    """
    if not isinstance(raw, list):
        return [], False
    items = [
        x for x in raw[:max_items]
        if isinstance(x, dict) and isinstance(x.get("id"), int)
    ]
    return items, True
