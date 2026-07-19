"""Tests for mtx_restore. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_mtx_restore -v
"""
import json
import os
import tempfile
import unittest

import mtx_restore as mr
from mtx_restore import (
    load_mtx_state, save_mtx_state, load_mtx_product, rolled_over,
)


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

    def test_load_toplevel_list_returns_empty(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "mtx_state.json")
        with open(path, "w") as f:
            json.dump([{"id": 1}], f)
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
        self.assertEqual(rec["to_restore"][0]["id"], 2000)
        self.assertEqual(rec["to_restore"][0]["stop"], 43559)

    def test_all_terminal_statuses_go_to_record_exit(self):
        for status in ("profit", "loss", "trail", "reversed", "session_end"):
            rec = mr.reconcile_restore(
                local_units=[self._local()],
                worker_history=[{"id": 2000, "status": status, "exit": 43500}],
                cutoff_ms=self.CUTOFF,
            )
            self.assertEqual(rec["to_restore"], [], f"{status} should not restore")
            self.assertEqual(len(rec["to_record_exit"]), 1, f"{status} should record exit")

    # ── 2026-07-19 audit: worker status outranks the cutoff ─────────────────
    # Old semantics dropped any local unit below the session-open cutoff, even
    # when the Worker still said "open" — a position carried across the session
    # boundary (e.g. opened 23:00, day-session restart at 10:00) was silently
    # dropped from tracking while still live at the broker. The cutoff now only
    # applies to units the Worker has NO record of (true ghosts).

    def test_carried_open_below_cutoff_is_restored(self):
        rec = mr.reconcile_restore(
            local_units=[self._local(id=500)],
            worker_history=[{"id": 500, "status": "open", "stop": 43600, "target": 43900}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(len(rec["to_restore"]), 1)      # live position: keep managing it
        self.assertEqual(rec["to_restore"][0]["stop"], 43600)
        self.assertEqual(rec["dropped_stale"], [])

    def test_terminal_below_cutoff_records_missed_exit(self):
        rec = mr.reconcile_restore(
            local_units=[self._local(id=500)],
            worker_history=[{"id": 500, "status": "loss", "exit": 43500}],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(len(rec["to_record_exit"]), 1)  # not silently dropped
        self.assertEqual(rec["dropped_stale"], [])

    def test_no_worker_record_below_cutoff_still_dropped(self):
        # True ghost: nothing at the Worker knows this id and it predates the
        # session — the original stale-drop case, unchanged.
        rec = mr.reconcile_restore(
            local_units=[self._local(id=500)],
            worker_history=[],
            cutoff_ms=self.CUTOFF,
        )
        self.assertEqual(rec["to_restore"], [])
        self.assertEqual(rec["dropped_stale"], [500])


class RolledOverTests(unittest.TestCase):
    def test_changed_product_is_rollover(self):
        self.assertTrue(rolled_over("MXFG6", "MXFH6"))

    def test_same_product_is_not_rollover(self):
        self.assertFalse(rolled_over("MXFG6", "MXFG6"))

    def test_missing_stored_product_is_not_rollover(self):
        # legacy file (no product key) or first boot -> conservative, never drop
        self.assertFalse(rolled_over(None, "MXFG6"))

    def test_missing_current_product_is_not_rollover(self):
        self.assertFalse(rolled_over("MXFG6", None))

    def test_whitespace_stripped_no_false_rollover(self):
        # a stray space in the env-sourced product must NOT drop a live position
        self.assertFalse(rolled_over("MXFG6", " MXFG6"))
        self.assertFalse(rolled_over(" MXFG6 ", "MXFG6"))

    def test_whitespace_real_rollover_still_detected(self):
        self.assertTrue(rolled_over(" MXFG6 ", "MXFH6"))

    def test_whitespace_only_current_is_not_a_product(self):
        self.assertFalse(rolled_over("MXFG6", "   "))  # whitespace-only -> falsy after strip


class ProductPersistenceTests(unittest.TestCase):
    def _tmp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_save_then_load_product(self):
        path = self._tmp()
        save_mtx_state(path, [{"id": 1, "dir": "long"}], product="MXFG6")
        self.assertEqual(load_mtx_product(path), "MXFG6")
        self.assertEqual(load_mtx_state(path), [{"id": 1, "dir": "long"}])

    def test_save_without_product_writes_none(self):
        path = self._tmp()
        save_mtx_state(path, [])  # product defaults to None
        self.assertIsNone(load_mtx_product(path))

    def test_load_product_legacy_file_without_key(self):
        path = self._tmp()
        with open(path, "w") as f:
            json.dump({"mtx_units": [{"id": 1}]}, f)   # no "product" key
        self.assertIsNone(load_mtx_product(path))
        self.assertEqual(load_mtx_state(path), [{"id": 1}])  # units still load

    def test_load_product_missing_file(self):
        self.assertIsNone(load_mtx_product("/nonexistent/path/x.json"))

    def test_load_product_corrupt_json(self):
        path = self._tmp()
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(load_mtx_product(path))


if __name__ == "__main__":
    unittest.main()
