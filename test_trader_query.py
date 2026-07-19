"""trader._query_broker_position failure semantics (2026-07-19 audit): a
broker-not-ok response or generic exception returned None, which the recon
caller reads as broker_net=0 — a false '倉位消失' alert during any mid-session
API outage. No-read must map to SCHEMA_FAIL (skip cycle), never to flat.

Stubs the unitrade SDK so trader.py imports on machines without it."""
import sys
import types
import unittest

if "unitrade" not in sys.modules:
    _u = types.ModuleType("unitrade")
    _uu = types.ModuleType("unitrade.unitrade")
    class _SDK:  # placeholders; tests never construct a live session
        pass
    _uu.Unitrade = _SDK
    _uu.DOrderObject = _SDK
    _uu.DReplaceObject = _SDK
    _u.unitrade = _uu
    sys.modules["unitrade"] = _u
    sys.modules["unitrade.unitrade"] = _uu

import trader as trader_mod
from feed_schema import SCHEMA_FAIL


class _Resp:
    def __init__(self, ok, data=None, error="boom"):
        self.ok = ok; self.data = data; self.error = error


class _Pos:
    def __init__(self, productid, buy=0, sell=0):
        self.productid = productid
        self.current_buy_open_position = buy
        self.current_sell_open_position = sell


def _mk_trader(resp=None, raise_exc=None):
    t = trader_mod.AutoTrader.__new__(trader_mod.AutoTrader)
    t.actno = "0239174"
    t.config = {"product": "MXFH6"}
    class _Acct:
        def get_position(self, actno):
            if raise_exc: raise raise_exc
            return resp
    t.api = types.SimpleNamespace(daccount=_Acct())
    return t


class QueryBrokerPositionFailureSemantics(unittest.TestCase):
    def test_not_ok_is_schema_fail_not_flat(self):
        t = _mk_trader(_Resp(ok=False))
        self.assertIs(t._query_broker_position(), SCHEMA_FAIL)

    def test_generic_exception_is_schema_fail_not_flat(self):
        t = _mk_trader(raise_exc=RuntimeError("socket reset"))
        self.assertIs(t._query_broker_position(), SCHEMA_FAIL)

    def test_empty_positions_is_genuinely_flat_none(self):
        t = _mk_trader(_Resp(ok=True, data=[]))
        self.assertIsNone(t._query_broker_position())

    def test_real_position_parses(self):
        t = _mk_trader(_Resp(ok=True, data=[_Pos("MXFH6", buy=1)]))
        self.assertEqual(t._query_broker_position(), {"productid": "MXFH6", "bs": "B", "qty": 1})


if __name__ == "__main__":
    unittest.main()
