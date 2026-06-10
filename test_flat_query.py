"""Tests for flat_query (flat.py --query / RESID= helpers).

Pure stdlib unittest (runs on system python3, no deps).
Run:  python3 -m unittest test_flat_query -v

WHY these tests matter: /flat (Discord gated control) derives a real-money
reversing order from NET= and verifies from RESID=. A schema drift or a
broker-not-ok response misread as "flat" (0) would either skip a needed
flatten or report a false success — both must map to UNKNOWN, never 0.
"""
from __future__ import annotations

import unittest

from flat_query import UNKNOWN, signed_net, query_net


class FakePos:
    def __init__(self, productid, buy=0, sell=0):
        self.productid = productid
        self.current_buy_open_position = buy
        self.current_sell_open_position = sell


class DriftedPos:
    """Lacks the open-position fields — schema drift, not flat."""
    def __init__(self, productid):
        self.productid = productid


class SignedNet(unittest.TestCase):
    def test_long_is_positive(self):
        self.assertEqual(signed_net([FakePos("MXFG6", buy=2)], "MXFG6"), 2)

    def test_short_is_negative(self):
        self.assertEqual(signed_net([FakePos("MXFG6", sell=1)], "MXFG6"), -1)

    def test_flat_product_entry_is_zero(self):
        self.assertEqual(signed_net([FakePos("MXFG6")], "MXFG6"), 0)

    def test_empty_positions_is_zero(self):
        self.assertEqual(signed_net([], "MXFG6"), 0)
        self.assertEqual(signed_net(None, "MXFG6"), 0)

    def test_other_products_ignored(self):
        self.assertEqual(signed_net([FakePos("TXFG6", buy=5)], "MXFG6"), 0)

    def test_schema_drift_is_unknown_not_flat(self):
        self.assertEqual(signed_net([DriftedPos("MXFG6")], "MXFG6"), UNKNOWN)

    def test_insane_qty_is_unknown(self):
        self.assertEqual(signed_net([FakePos("MXFG6", buy=999)], "MXFG6"), UNKNOWN)


class FakeResp:
    def __init__(self, ok, data=None, error=""):
        self.ok = ok
        self.data = data
        self.error = error


class FakeAccount:
    def __init__(self, resp=None, raises=False):
        self._resp = resp
        self._raises = raises

    def get_position(self, actno, groupid="", trader=""):
        if self._raises:
            raise RuntimeError("socket dead")
        return self._resp


class FakeApi:
    def __init__(self, account):
        self.daccount = account


class QueryNet(unittest.TestCase):
    def test_ok_response_returns_signed_net(self):
        api = FakeApi(FakeAccount(FakeResp(True, [FakePos("MXFG6", sell=2)])))
        self.assertEqual(query_net(api, "0239174", "MXFG6"), -2)

    def test_ok_response_no_positions_is_flat_zero(self):
        api = FakeApi(FakeAccount(FakeResp(True, [])))
        self.assertEqual(query_net(api, "0239174", "MXFG6"), 0)

    def test_broker_not_ok_is_unknown_never_flat(self):
        api = FakeApi(FakeAccount(FakeResp(False, error="busy")))
        self.assertEqual(query_net(api, "0239174", "MXFG6"), UNKNOWN)

    def test_none_response_is_unknown(self):
        api = FakeApi(FakeAccount(None))
        self.assertEqual(query_net(api, "0239174", "MXFG6"), UNKNOWN)

    def test_exception_is_unknown(self):
        api = FakeApi(FakeAccount(raises=True))
        self.assertEqual(query_net(api, "0239174", "MXFG6"), UNKNOWN)

    def test_schema_drift_propagates_unknown(self):
        api = FakeApi(FakeAccount(FakeResp(True, [DriftedPos("MXFG6")])))
        self.assertEqual(query_net(api, "0239174", "MXFG6"), UNKNOWN)


if __name__ == "__main__":
    unittest.main()
