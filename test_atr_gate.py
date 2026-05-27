"""Tests for atr_gate.should_skip_code4_atr. Pure stdlib unittest.
Run:  python3 -m unittest test_atr_gate -v
"""
import unittest

from atr_gate import should_skip_code4_atr


class AtrGateTest(unittest.TestCase):

    # --- Gate disabled (env unset / 0) ---
    def test_gate_disabled_threshold_0(self):
        self.assertFalse(should_skip_code4_atr(4, 100, 0))

    def test_gate_disabled_threshold_negative(self):
        self.assertFalse(should_skip_code4_atr(4, 100, -1))

    def test_gate_disabled_threshold_non_int(self):
        self.assertFalse(should_skip_code4_atr(4, 100, "58"))

    def test_gate_disabled_threshold_none(self):
        self.assertFalse(should_skip_code4_atr(4, 100, None))

    # --- Code 4 (④) — gate applies ---
    def test_code4_above_threshold_skips(self):
        self.assertTrue(should_skip_code4_atr(4, 60, 58))

    def test_code4_at_boundary_does_not_skip(self):
        # spec: strict > comparison, boundary value passes
        self.assertFalse(should_skip_code4_atr(4, 58, 58))

    def test_code4_below_threshold_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 50, 58))

    def test_code4_far_above_threshold_skips(self):
        self.assertTrue(should_skip_code4_atr(4, 128, 58))

    # --- Other codes — gate never triggers ---
    def test_code3_high_atr_does_not_skip(self):
        # ③ × High-ATR is favorable, MUST NOT be gated
        self.assertFalse(should_skip_code4_atr(3, 100, 58))

    def test_code8_high_atr_does_not_skip(self):
        # ⑧ × High-ATR is mostly winners, MUST NOT be gated
        self.assertFalse(should_skip_code4_atr(8, 100, 58))

    def test_code2_high_atr_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(2, 100, 58))

    def test_code1_high_atr_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(1, 100, 58))

    def test_code0_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(0, 100, 58))

    # --- Fail-open on missing / invalid ATR ---
    def test_atr_none_does_not_skip(self):
        # Worker bug / missing atr field — MUST NOT skip
        self.assertFalse(should_skip_code4_atr(4, None, 58))

    def test_atr_string_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, "100", 58))

    def test_atr_bool_does_not_skip(self):
        # bool is subclass of int in Python — exclude explicitly
        self.assertFalse(should_skip_code4_atr(4, True, 58))

    # --- Float ATR works ---
    def test_atr_float_above_threshold_skips(self):
        self.assertTrue(should_skip_code4_atr(4, 58.5, 58))

    def test_atr_float_below_threshold_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 57.9, 58))


if __name__ == "__main__":
    unittest.main()
