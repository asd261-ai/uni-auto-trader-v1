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
