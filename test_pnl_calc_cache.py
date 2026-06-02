"""Tests for pnl_calc cache behaviour across the 08:45 trading-day boundary.

Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_pnl_calc_cache -v

Regression target — the phantom DAILY_MAX_LOSS re-lock observed 2026-06-02 08:45:
pnl_calc caches real_trading_day_pnl_pts for 30s. When the cache window straddles
the 08:45 trading-day boundary, the post-reset loss-lock check reads the STALE
pre-boundary value (yesterday's full-day P&L) and re-locks the fresh day, then
latches for the whole day. See [[project-shared-account-margin-contention]].
"""
import os
import tempfile
import unittest

import pnl_calc

YESTERDAY_START = "2026-06-01T08:45:00+08:00"
TODAY_START     = "2026-06-02T08:45:00+08:00"

# A single MXFF6 long round-trip that CLOSED yesterday for -407 pts and nothing
# today: B@46500 (open) then S@46093 (close) → 46093 - 46500 = -407.
_ORDERS = (
    '{"ts":"2026-06-01T17:49:00+08:00","event":"match","productid":"MXFF6","bs":"B","matchprice":46500.0,"matchqty":1}\n'
    '{"ts":"2026-06-01T17:50:00+08:00","event":"match","productid":"MXFF6","bs":"S","matchprice":46093.0,"matchqty":1}\n'
)


class CacheBoundary(unittest.TestCase):
    def setUp(self):
        self._orig_path = pnl_calc._ORDERS_PATH
        self._orig_start = pnl_calc._trading_day_start_iso
        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self._tmp.write(_ORDERS)
        self._tmp.close()
        pnl_calc._ORDERS_PATH = self._tmp.name
        pnl_calc._CACHE = {"ts": 0.0, "val": None}  # start cold

    def tearDown(self):
        pnl_calc._ORDERS_PATH = self._orig_path
        pnl_calc._trading_day_start_iso = self._orig_start
        pnl_calc._CACHE = {"ts": 0.0, "val": None}
        os.unlink(self._tmp.name)

    def _pin_day(self, iso):
        pnl_calc._trading_day_start_iso = lambda: iso

    def test_window_before_boundary_counts_yesterday(self):
        # Sanity: under yesterday's window, the round-trip is the day's P&L.
        self._pin_day(YESTERDAY_START)
        self.assertEqual(pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"], -407.0)

    def test_cache_invalidates_across_trading_day_boundary(self):
        # 1) Pre-boundary call caches yesterday's full-day P&L (-407).
        self._pin_day(YESTERDAY_START)
        self.assertEqual(pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"], -407.0)

        # 2) Boundary crosses to the new trading day WITHIN the 30s TTL (the bug
        #    window). The round-trip closed yesterday, so today's real day P&L is 0.
        #    Stale cache would wrongly return -407 and phantom-lock the fresh day.
        self._pin_day(TODAY_START)
        self.assertEqual(
            pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"],
            0,
            "cache must invalidate when the 08:45 trading-day window moves",
        )

    def test_same_day_cache_still_served(self):
        # Regression: within the same trading day the cache must still short-circuit
        # disk reads (don't recompute just because content changed mid-window).
        self._pin_day(TODAY_START)
        first = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        # Mutate the file underneath; same-day + within TTL → cached value stands.
        with open(self._tmp.name, "a", encoding="utf-8") as f:
            f.write('{"ts":"2026-06-02T09:00:00+08:00","event":"match","productid":"MXFF6","bs":"B","matchprice":1.0,"matchqty":1}\n')
        second = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        self.assertEqual(first, second, "same-day within-TTL call should be cache-served")


if __name__ == "__main__":
    unittest.main()
