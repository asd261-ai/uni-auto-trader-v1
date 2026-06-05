"""Pure unittest for real_fill_pnl. No broker-SDK / no I/O.
Run:  python3 -m unittest test_real_fill_pnl -v
WHY: real-fill P&L must NEVER fall back to signal values — a missing fill must
read as null, not a fabricated number, or real-money attribution silently lies.
"""
import unittest
import real_fill_pnl as rfp


class ComputePnlPtsReal(unittest.TestCase):
    def test_long_uses_exit_minus_entry(self):
        self.assertEqual(rfp.compute_pnl_pts_real("long", 46100, 46160), 60)

    def test_short_uses_entry_minus_exit(self):
        self.assertEqual(rfp.compute_pnl_pts_real("short", 46470, 46450), 20)

    def test_missing_entry_fill_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("long", None, 46160))

    def test_missing_exit_fill_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("short", 46470, None))

    def test_both_missing_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("long", None, None))

    def test_result_is_rounded_int(self):
        self.assertEqual(rfp.compute_pnl_pts_real("long", 46100.4, 46160.4), 60)


class FinalizeExit(unittest.TestCase):
    def _record(self, dir_="long", entry_fill=46100):
        # mirrors the trades.jsonl record dict shape (key "dir", "entry_fill")
        return {"dir": dir_, "entry_fill": entry_fill,
                "exit_fill": None, "pnl_pts_real": None}

    def test_sets_exit_fill_and_pnl_when_both_present(self):
        rec = self._record(dir_="long", entry_fill=46100)
        out = rfp.finalize_exit(rec, 46160)
        self.assertEqual(out["exit_fill"], 46160)
        self.assertEqual(out["pnl_pts_real"], 60)

    def test_short_direction(self):
        rec = self._record(dir_="short", entry_fill=46470)
        rfp.finalize_exit(rec, 46450)
        self.assertEqual(rec["pnl_pts_real"], 20)

    def test_missing_entry_fill_leaves_pnl_none(self):
        rec = self._record(dir_="long", entry_fill=None)
        rfp.finalize_exit(rec, 46160)
        self.assertEqual(rec["exit_fill"], 46160)
        self.assertIsNone(rec["pnl_pts_real"])

    def test_timeout_flush_exit_fill_none(self):
        # poll-loop timeout path: no real fill arrived → exit_fill=None, pnl None
        rec = self._record(dir_="long", entry_fill=46100)
        rfp.finalize_exit(rec, None)
        self.assertIsNone(rec["exit_fill"])
        self.assertIsNone(rec["pnl_pts_real"])

    def test_mutates_in_place_and_returns_same_object(self):
        rec = self._record()
        self.assertIs(rfp.finalize_exit(rec, 46160), rec)
