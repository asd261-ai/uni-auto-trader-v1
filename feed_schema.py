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
