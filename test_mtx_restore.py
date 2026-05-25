"""Tests for mtx_restore. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_mtx_restore -v
"""
import os
import tempfile
import unittest

import mtx_restore as mr


class StateRoundTripTest(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "mtx_state.json")
        units = [{"id": 100, "dir": "long", "entry": 43419, "stop": 43559}]
        mr.save_mtx_state(path, units)
        self.assertEqual(mr.load_mtx_state(path), units)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(mr.load_mtx_state("/nonexistent/mtx_state.json"), [])

    def test_load_corrupt_file_returns_empty(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "mtx_state.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(mr.load_mtx_state(path), [])


class ReconcileRestoreTest(unittest.TestCase):
    CUTOFF = 1000  # boot floor; ids must be > this to be in-session

    def _local(self, **kw):
        u = {"id": 2000, "dir": "long", "entry": 43419, "stop": 43559, "target": 43800}
        u.update(kw)
        return u

    def test_phantom_skipped_worker_open_not_in_local(self):
        rec = mr.reconcile_restore(
            local_units=[],
            worker_history=[{"id": 2000, "status": "open"}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(rec["skipped_phantoms"], [2000])

    def test_normal_restore_refreshes_levels(self):
        rec = mr.reconcile_restore(
            local_units=[self._local(stop=43559, target=43800)],
            worker_history=[{"id": 2000, "status": "open", "stop": 43600, "target": 43900}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(len(rec["to_restore"]), 1)
        self.assertEqual(rec["to_restore"][0]["stop"], 43600)
        self.assertEqual(rec["to_restore"][0]["target"], 43900)

    def test_missed_exit_when_worker_terminal(self):
        rec = mr.reconcile_restore(
            local_units=[self._local()],
            worker_history=[{"id": 2000, "status": "loss", "exit": 43500}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(len(rec["to_record_exit"]), 1)
        unit, worker = rec["to_record_exit"][0]
        self.assertEqual(unit["id"], 2000)
        self.assertEqual(worker["status"], "loss")

    def test_worker_missing_id_restores_conservatively(self):
        rec = mr.reconcile_restore(
            local_units=[self._local()],
            worker_history=[],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(len(rec["to_restore"]), 1)

    def test_stale_local_unit_below_cutoff_dropped(self):
        rec = mr.reconcile_restore(
            local_units=[self._local(id=500)],
            worker_history=[{"id": 500, "status": "open"}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(rec["dropped_stale"], [500])
