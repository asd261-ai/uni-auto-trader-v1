"""Tests for reconcile_real_fill — the Monday observe-first reconciliation math.

Intent: prove the reconciliation correctly FLAGS the two failure modes it exists to catch —
(1) a missing real fill (pnl_pts_real=None) is RED, (2) real vs orders.jsonl FIFO drift is
caught — and that the under-report gap (signal vs real) is surfaced, not hidden.
"""
import unittest

from reconcile_real_fill import day_window, reconcile


def _trade(source="mtx", pnl_pts=0.0, pnl_pts_real=0.0, id="x", reason="trail"):
    return {"source": source, "id": id, "dir": "long", "pnl_pts": pnl_pts,
            "pnl_pts_real": pnl_pts_real, "reason": reason}


class DayWindowTests(unittest.TestCase):
    def test_window_is_0845_to_next_0845(self):
        start, end = day_window("2026-06-08")
        self.assertTrue(start.startswith("2026-06-08T08:45"))
        self.assertTrue(end.startswith("2026-06-09T08:45"))

    def test_night_after_midnight_falls_inside_same_trading_day(self):
        # A night round-trip closing 2026-06-09 01:00 belongs to trading_day 2026-06-08.
        start, end = day_window("2026-06-08")
        after_midnight = "2026-06-09T01:00:00+08:00"
        self.assertTrue(start <= after_midnight < end)

    def test_next_morning_open_is_excluded(self):
        start, end = day_window("2026-06-08")
        next_open = "2026-06-09T09:00:00+08:00"  # belongs to 6/9, must be OUT
        self.assertFalse(start <= next_open < end)


class ReconcileTests(unittest.TestCase):
    def test_clean_match_is_ok(self):
        trades = [_trade(pnl_pts=10, pnl_pts_real=8),
                  _trade(pnl_pts=-5, pnl_pts_real=-6)]
        rep = reconcile(trades, fifo_realized_pts=2.0, fifo_roundtrips=2)
        self.assertEqual(rep["verdict"], "OK")
        self.assertEqual(rep["sum_real"], 2.0)
        self.assertEqual(rep["real_vs_fifo"], 0.0)
        self.assertEqual(rep["n_null"], 0)

    def test_missing_real_fill_is_red(self):
        # The core observe-first failure: exit_fill never came back → pnl_pts_real None.
        trades = [_trade(pnl_pts=10, pnl_pts_real=None),
                  _trade(pnl_pts=-5, pnl_pts_real=-6)]
        rep = reconcile(trades, fifo_realized_pts=-6.0, fifo_roundtrips=1)
        self.assertEqual(rep["verdict"], "RED")
        self.assertEqual(rep["n_null"], 1)
        self.assertEqual(len(rep["null_ids"]), 1)

    def test_real_vs_fifo_drift_is_warn(self):
        # All fills present but real sum diverges from FIFO ground truth beyond tolerance.
        trades = [_trade(pnl_pts=10, pnl_pts_real=8)]
        rep = reconcile(trades, fifo_realized_pts=20.0, fifo_roundtrips=1)
        self.assertEqual(rep["verdict"], "WARN")
        self.assertEqual(rep["real_vs_fifo"], -12.0)

    def test_count_mismatch_flags_manual_intrusion(self):
        # 1 bot trade, but FIFO sees 2 round-trips (a manual same-contract trade) → WARN.
        trades = [_trade(pnl_pts=10, pnl_pts_real=8)]
        rep = reconcile(trades, fifo_realized_pts=8.0, fifo_roundtrips=2)
        self.assertTrue(rep["count_mismatch"])
        self.assertEqual(rep["verdict"], "WARN")

    def test_signal_vs_real_gap_surfaced(self):
        # The whole point: signal under-reports vs real. 6/5 was −91 signal vs −325 real.
        trades = [_trade(pnl_pts=-91, pnl_pts_real=-325)]
        rep = reconcile(trades, fifo_realized_pts=-325.0, fifo_roundtrips=1)
        self.assertEqual(rep["signal_vs_real"], 234.0)
        self.assertEqual(rep["verdict"], "OK")  # real matches FIFO; the gap is signal's fault

    def test_by_source_breakdown(self):
        trades = [_trade(source="mtx", pnl_pts=10, pnl_pts_real=8),
                  _trade(source="fvg", pnl_pts=-3, pnl_pts_real=None)]
        rep = reconcile(trades, fifo_realized_pts=8.0, fifo_roundtrips=1)
        self.assertEqual(rep["by_source"]["mtx"]["n"], 1)
        self.assertEqual(rep["by_source"]["fvg"]["n_null"], 1)
        self.assertEqual(rep["by_source"]["fvg"]["sum_real"], 0.0)

    def test_fvg_paper_null_does_not_flip_verdict(self):
        # FVG runs paper (never in orders.jsonl) → its null pnl_pts_real is EXPECTED, must
        # NOT make the day RED. Only the mtx row is reconciled against the FIFO.
        trades = [_trade(source="mtx", pnl_pts=10, pnl_pts_real=8),
                  _trade(source="fvg", pnl_pts=-3, pnl_pts_real=None)]
        rep = reconcile(trades, fifo_realized_pts=8.0, fifo_roundtrips=1)
        self.assertEqual(rep["verdict"], "OK")
        self.assertEqual(rep["n_null"], 0)        # mtx-scoped: the fvg null doesn't count
        self.assertEqual(rep["other_n"], 1)
        self.assertEqual(rep["other_null"], 1)
        self.assertEqual(rep["sum_real"], 8.0)    # fvg row excluded from the FIFO comparison

    def test_mtx_null_still_red_with_fvg_present(self):
        # A real mtx missing-fill must still go RED even when FVG paper rows are present.
        trades = [_trade(source="mtx", pnl_pts=10, pnl_pts_real=None),
                  _trade(source="fvg", pnl_pts=-3, pnl_pts_real=None)]
        rep = reconcile(trades, fifo_realized_pts=0.0, fifo_roundtrips=0)
        self.assertEqual(rep["verdict"], "RED")
        self.assertEqual(rep["n_null"], 1)

    def test_empty_day_is_ok(self):
        rep = reconcile([], fifo_realized_pts=0.0, fifo_roundtrips=0)
        self.assertEqual(rep["verdict"], "OK")
        self.assertEqual(rep["n_trades"], 0)


if __name__ == "__main__":
    unittest.main()
