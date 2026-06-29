"""Tests for entry_guard — entry_past_target and cross_source_opposite guards.

entry_past_target intent: enter only when reward still exists. Entering at/through the target is
RR≤0 (you'd be at your exit on entry), so the guard must skip it — but it must FAIL OPEN on bad
data so a feed glitch can never silently halt all entries.

cross_source_opposite intent: block a new entry when another source already holds a position in
the opposite direction (cross-source collision). Fails open on any bad input.

Run:  python3 -m unittest test_entry_guard -v
"""
import unittest

from entry_guard import entry_past_target
import entry_guard as eg


def _u(dir_):
    return {"dir": dir_}


class EntryPastTargetTests(unittest.TestCase):
    def test_long_past_target_skips(self):
        # The 2026-06-08 09:56 trigger: long filled 43121, target 43017 → reward gone.
        self.assertTrue(entry_past_target("long", price=43121, target=43017))

    def test_long_exactly_at_target_skips(self):
        # At the target the reward is already zero → no point entering.
        self.assertTrue(entry_past_target("long", price=43017, target=43017))

    def test_long_before_target_enters(self):
        self.assertFalse(entry_past_target("long", price=42900, target=43017))

    def test_short_past_target_skips(self):
        self.assertTrue(entry_past_target("short", price=42800, target=42900))

    def test_short_exactly_at_target_skips(self):
        self.assertTrue(entry_past_target("short", price=42900, target=42900))

    def test_short_before_target_enters(self):
        self.assertFalse(entry_past_target("short", price=43000, target=42900))

    def test_missing_price_fails_open(self):
        self.assertFalse(entry_past_target("long", price=None, target=43017))

    def test_missing_target_fails_open(self):
        self.assertFalse(entry_past_target("long", price=43121, target=None))

    def test_unknown_direction_does_not_block(self):
        self.assertFalse(entry_past_target("flat", price=43121, target=43017))

    def test_non_numeric_fails_open(self):
        self.assertFalse(entry_past_target("long", price="x", target=43017))


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


if __name__ == "__main__":
    unittest.main()
