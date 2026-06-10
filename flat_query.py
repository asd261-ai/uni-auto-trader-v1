"""Pure helpers behind flat.py --query / RESID= (Discord /flat verification).

Principle (same as feed_schema): UNKNOWN must be distinguishable from flat.
A broker-not-ok response, an exception, or schema drift must never read as 0 —
/flat derives a real-money reversing order from this number, and verifies the
flatten from it.
"""
from __future__ import annotations

from feed_schema import SCHEMA_FAIL, parse_broker_position

# Printed verbatim as NET=UNKNOWN / RESID=UNKNOWN — the bot aborts loud on it.
UNKNOWN = "UNKNOWN"


def signed_net(positions, product: str):
    """Signed broker net for product: buy=+qty, sell=-qty, 0=flat.

    Returns UNKNOWN when any matching object fails schema validation —
    never 0, which would read as "flat" and skip a needed flatten.
    """
    for p in positions or []:
        r = parse_broker_position(p, product)
        if r is SCHEMA_FAIL:
            return UNKNOWN
        if r is not None:
            return r["qty"] if r["bs"] == "B" else -r["qty"]
    return 0


def query_net(api, actno: str, product: str):
    """Read the signed broker net via daccount.get_position.

    Returns int (0=flat) or UNKNOWN. Unlike trader._query_broker_position
    (which folds broker-not-ok into None for the recon loop), a not-ok
    response here is UNKNOWN: /flat must abort, not assume flat.
    """
    try:
        resp = api.daccount.get_position(actno)
    except Exception:
        return UNKNOWN
    if not resp or not getattr(resp, "ok", False):
        return UNKNOWN
    return signed_net(getattr(resp, "data", None) or [], product)
