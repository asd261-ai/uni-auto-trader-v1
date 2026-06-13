"""Tests for demote_gate.should_demote. Pure stdlib unittest.
Run:  python3 -m unittest test_demote_gate -v
"""
import unittest

from demote_gate import should_demote


class DemoteGateTest(unittest.TestCase):

    # --- Empty demote set (env unset) — never demote ---
    def test_empty_set_never_demotes(self):
        self.assertFalse(should_demote(2, frozenset()))

    def test_empty_set_code8(self):
        self.assertFalse(should_demote(8, frozenset()))

    # --- Code in the demote set — demote ---
    def test_code2_in_set_demotes(self):
        self.assertTrue(should_demote(2, frozenset({2})))

    def test_code2_in_multi_set_demotes(self):
        self.assertTrue(should_demote(2, frozenset({2, 3})))

    def test_code3_in_multi_set_demotes(self):
        self.assertTrue(should_demote(3, frozenset({2, 3})))

    # --- Code not in the set — never demote ---
    def test_code8_not_in_set(self):
        self.assertFalse(should_demote(8, frozenset({2})))

    def test_code4_not_in_set(self):
        self.assertFalse(should_demote(4, frozenset({2})))

    # --- String sigCode coerces (Worker may send "2") ---
    def test_string_code_in_set_demotes(self):
        self.assertTrue(should_demote("2", frozenset({2})))

    def test_string_code_not_in_set(self):
        self.assertFalse(should_demote("8", frozenset({2})))

    # --- Fail-open on invalid sigCode (never demote on bad data) ---
    def test_none_code_does_not_demote(self):
        self.assertFalse(should_demote(None, frozenset({2})))

    def test_nonnumeric_string_does_not_demote(self):
        self.assertFalse(should_demote("abc", frozenset({2})))

    def test_bool_code_does_not_demote(self):
        # bool is subclass of int — True==1, exclude explicitly so True never
        # matches a {1} demote set by accident.
        self.assertFalse(should_demote(True, frozenset({1})))

    def test_float_code_does_not_demote(self):
        # Worker sends integer codes; a float is unexpected → fail-open.
        self.assertFalse(should_demote(2.0, frozenset({2})))


if __name__ == "__main__":
    unittest.main()
