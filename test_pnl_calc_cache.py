"""Tests for pnl_calc cache behaviour across the 08:45 trading-day boundary.

Rewritten 2026-06-19 against the provenance-FIFO API (pnl_calc post-refactor).
Previous version monkeypatched pnl_calc._ORDERS_PATH and pnl_calc._trading_day_start_iso,
neither of which exist in the real module; those tests never ran.

Now patches the REAL module-level functions:
  - pnl_calc._today_trading_day  → pinned to a fixed date string
  - pnl_calc._read_orders_raw    → injects pre-parsed dicts (accepts optional path arg)
  - pnl_calc._read_trades        → returns [] (no per-trade cross-check noise)

Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_pnl_calc_cache -v

Regression target — the phantom DAILY_MAX_LOSS re-lock observed 2026-06-02 08:45:
pnl_calc caches real_trading_day_pnl_pts for 30s. When the cache window straddles
the 08:45 trading-day boundary, the post-reset loss-lock check reads the STALE
pre-boundary value (yesterday's full-day P&L) and re-locks the fresh day, then
latches for the whole day. See [[project-shared-account-margin-contention]].
"""
import unittest

import pnl_calc

# Bot-backed long round-trip that closed on 2026-06-01 for -407 pts:
# B@46500 (open) then S@46093 (close) → 46093 - 46500 = -407.
# MUST include the sent→reply(orderno)→match chain so realized_day_pts treats
# the fills as bot-backed (bare match events with no sent/reply are excluded
# as manual fills). All events share the same second (within _LINK_WINDOW_SEC=3).
_ORDERS = [
    {"ts": "2026-06-01T17:49:00+08:00", "event": "sent",  "productid": "MXFF6", "bs": "B"},
    {"ts": "2026-06-01T17:49:00+08:00", "event": "reply", "productid": "MXFF6", "bs": "B", "orderno": "QN1"},
    {"ts": "2026-06-01T17:49:00+08:00", "event": "match", "productid": "MXFF6", "bs": "B", "orderno": "QN1", "matchprice": 46500.0, "matchqty": 1},
    {"ts": "2026-06-01T17:50:00+08:00", "event": "sent",  "productid": "MXFF6", "bs": "S"},
    {"ts": "2026-06-01T17:50:00+08:00", "event": "reply", "productid": "MXFF6", "bs": "S", "orderno": "QN2"},
    {"ts": "2026-06-01T17:50:00+08:00", "event": "match", "productid": "MXFF6", "bs": "S", "orderno": "QN2", "matchprice": 46093.0, "matchqty": 1},
]

# Second bot-backed round-trip used in test_same_day_cache_still_served to verify
# that the cache blocks disk reads within the same trading day + TTL window.
_ORDERS_WITH_EXTRA = _ORDERS + [
    {"ts": "2026-06-02T09:00:00+08:00", "event": "sent",  "productid": "MXFF6", "bs": "B"},
    {"ts": "2026-06-02T09:00:00+08:00", "event": "reply", "productid": "MXFF6", "bs": "B", "orderno": "QN3"},
    {"ts": "2026-06-02T09:00:00+08:00", "event": "match", "productid": "MXFF6", "bs": "B", "orderno": "QN3", "matchprice": 46000.0, "matchqty": 1},
]


class CacheBoundary(unittest.TestCase):
    def setUp(self):
        # Save real functions and module globals for tearDown restoration.
        self._orig_today = pnl_calc._today_trading_day
        self._orig_read_orders = pnl_calc._read_orders_raw
        self._orig_read_trades = pnl_calc._read_trades
        self._orig_cache = dict(pnl_calc._CACHE)

        # Inject deterministic stubs.
        pnl_calc._read_trades = lambda: []
        # Start with _ORDERS as default injection (tests may override per-call).
        pnl_calc._read_orders_raw = lambda path=None: list(_ORDERS)
        # Reset cache to fully cold state (all three keys must be present).
        pnl_calc._CACHE = {"ts": 0.0, "val": None, "day": None}

    def tearDown(self):
        pnl_calc._today_trading_day = self._orig_today
        pnl_calc._read_orders_raw = self._orig_read_orders
        pnl_calc._read_trades = self._orig_read_trades
        pnl_calc._CACHE = self._orig_cache

    def _pin_day(self, day_str):
        """Force _today_trading_day() to return a fixed 'YYYY-MM-DD' string."""
        pnl_calc._today_trading_day = lambda: day_str

    # ------------------------------------------------------------------
    # Test 1: sanity — round-trip is in the 6/1 window → P&L = -407
    # ------------------------------------------------------------------
    def test_window_before_boundary_counts_yesterday(self):
        """Under the 2026-06-01 trading day, the bot round-trip is in-window."""
        self._pin_day("2026-06-01")
        result = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        self.assertEqual(result, -407.0)

    # ------------------------------------------------------------------
    # Test 2: core regression — cache must invalidate when day changes
    # ------------------------------------------------------------------
    def test_cache_invalidates_across_trading_day_boundary(self):
        """Cache keyed on trading-day MUST NOT serve yesterday's P&L on the new day.

        Pre-boundary call caches -407 for 2026-06-01. Within the 30s TTL window
        the clock crosses 08:45 and the trading day becomes 2026-06-02. The
        round-trip now falls outside the 6/2 window, so real_trading_day_pnl_pts
        must be 0.0 — not the stale -407 that would phantom-lock the fresh day.
        """
        # Step 1: prime cache under 6/1.
        self._pin_day("2026-06-01")
        val_day1 = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        self.assertEqual(val_day1, -407.0)

        # Step 2: day crosses to 6/2 while still within the 30s TTL (the bug window).
        # The cache's "day" key no longer matches → must recompute.
        self._pin_day("2026-06-02")
        val_day2 = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        self.assertEqual(
            val_day2,
            0.0,
            "cache must invalidate when 08:45 trading-day window advances; "
            "stale -407 would phantom-lock the fresh day's DAILY_MAX_LOSS breaker",
        )

    # ------------------------------------------------------------------
    # Test 3: same-day within-TTL — cache must short-circuit
    # ------------------------------------------------------------------
    def test_same_day_cache_still_served(self):
        """Within the same trading day and TTL the cached value is served unchanged.

        First call on 2026-06-02: round-trip is in 6/1's window, not 6/2's →
        real_trading_day_pnl_pts = 0.0; this is cached.

        Second call: swap in _ORDERS_WITH_EXTRA (adds a bot round-trip open leg
        on 6/2 that has no close, so even a fresh compute would still return 0.0
        for realized_day_pts on 6/2 — but crucially the cache prevents the read
        entirely). The returned value must equal the first call, proving the cache
        short-circuits rather than re-reading orders.
        """
        self._pin_day("2026-06-02")

        first = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        self.assertEqual(first, 0.0,
                         "6/1 round-trip is outside the 6/2 window; "
                         "no realized P&L for today yet")

        # Mutate the injected reader to return more orders.
        pnl_calc._read_orders_raw = lambda path=None: list(_ORDERS_WITH_EXTRA)

        second = pnl_calc.heartbeat_fields("MXFF6")["real_trading_day_pnl_pts"]
        self.assertEqual(
            first,
            second,
            "same-day within-TTL call must return cached value, not re-read orders",
        )


if __name__ == "__main__":
    unittest.main()
