"""Tests for entry_guard. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_entry_guard -v
"""
import unittest

import entry_guard as eg


def _u(dir_):
    return {"dir": dir_}


class CrossSourceOpposite(unittest.TestCase):
    def test_other_source_opposite_blocks(self):
        # FVG long open; MTX wants short → opposite → True
        units = {"fvg": [_u("long")], "mtx": []}
        self.assertTrue(eg.cross_source_opposite(units, "mtx", "short"))

    def test_other_source_opposite_blocks_symmetric(self):
        units = {"mtx": [_u("short")], "fvg": []}
        self.assertTrue(eg.cross_source_opposite(units, "fvg", "long"))

    def test_other_source_same_direction_allowed(self):
        # MTX short + FVG short = 2 lots short at broker, nets fine → False
        units = {"mtx": [_u("short")], "fvg": []}
        self.assertFalse(eg.cross_source_opposite(units, "fvg", "short"))

    def test_no_other_source_position_allowed(self):
        units = {"mtx": [], "fvg": []}
        self.assertFalse(eg.cross_source_opposite(units, "mtx", "short"))

    def test_same_source_opposite_ignored(self):
        # Only OTHER sources count; this source's own units are not a cross-source collision.
        units = {"mtx": [_u("long")], "fvg": []}
        self.assertFalse(eg.cross_source_opposite(units, "mtx", "short"))

    def test_missing_source_key_allowed(self):
        units = {"mtx": [_u("short")]}
        self.assertFalse(eg.cross_source_opposite(units, "fvg", "short"))

    def test_malformed_units_fail_open(self):
        self.assertFalse(eg.cross_source_opposite(None, "mtx", "short"))
        self.assertFalse(eg.cross_source_opposite({"fvg": None}, "mtx", "short"))
        self.assertFalse(eg.cross_source_opposite({"fvg": [{}]}, "mtx", "short"))

    def test_unknown_direction_fail_open(self):
        units = {"fvg": [_u("long")]}
        self.assertFalse(eg.cross_source_opposite(units, "mtx", "sideways"))
