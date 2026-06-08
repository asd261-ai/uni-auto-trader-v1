"""Tests for entry_guard.entry_past_target — the FVG gap-open fill-past-target skip.

Intent: enter only when reward still exists. Entering at/through the target is RR≤0 (you'd be
at your exit on entry), so the guard must skip it — but it must FAIL OPEN on bad data so a feed
glitch can never silently halt all entries.
"""
import unittest

from entry_guard import entry_past_target


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


if __name__ == "__main__":
    unittest.main()
