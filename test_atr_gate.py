"""Tests for atr_gate.should_skip_code4_atr. Pure stdlib unittest.
Run:  python3 -m unittest test_atr_gate -v
"""
import unittest

from atr_gate import should_skip_code4_atr


class AtrGateTest(unittest.TestCase):

    # --- Gate disabled (env unset / 0) — should never skip regardless of session ---
    def test_gate_disabled_threshold_0_night(self):
        self.assertFalse(should_skip_code4_atr(4, 100, 0, "night"))

    def test_gate_disabled_threshold_negative_night(self):
        self.assertFalse(should_skip_code4_atr(4, 100, -1, "night"))

    def test_gate_disabled_threshold_non_int(self):
        self.assertFalse(should_skip_code4_atr(4, 100, "58", "night"))

    def test_gate_disabled_threshold_none(self):
        self.assertFalse(should_skip_code4_atr(4, 100, None, "night"))

    # --- Night session + code 4 (④) — gate applies ---
    def test_code4_night_above_threshold_skips(self):
        self.assertTrue(should_skip_code4_atr(4, 60, 58, "night"))

    def test_code4_night_at_boundary_does_not_skip(self):
        # spec: strict > comparison, boundary value passes
        self.assertFalse(should_skip_code4_atr(4, 58, 58, "night"))

    def test_code4_night_below_threshold_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 50, 58, "night"))

    def test_code4_night_far_above_threshold_skips(self):
        self.assertTrue(should_skip_code4_atr(4, 128, 58, "night"))

    # --- Day session — must NEVER skip even ④ × High-ATR (2026-05-28 refinement) ---
    def test_code4_day_high_atr_does_not_skip(self):
        # The whole point of night-only refinement: day-session ④×High-ATR
        # actually has +edge (per 13-event counterfactual), so let it trade.
        self.assertFalse(should_skip_code4_atr(4, 128, 58, "day"))

    def test_code4_day_extreme_atr_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 200, 58, "day"))

    def test_code4_day_at_boundary_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 58, 58, "day"))

    # --- Break / unknown session — never skip ---
    def test_code4_break_session_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 128, 58, "break"))

    def test_code4_none_session_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 128, 58, None))

    def test_code4_unknown_session_string_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 128, 58, "weekend"))

    # --- Other codes — never skip even in night with high ATR ---
    def test_code3_night_high_atr_does_not_skip(self):
        # ③ × High-ATR is favorable, MUST NOT be gated even at night
        self.assertFalse(should_skip_code4_atr(3, 100, 58, "night"))

    def test_code8_night_high_atr_does_not_skip(self):
        # ⑧ × High-ATR is mostly winners, MUST NOT be gated
        self.assertFalse(should_skip_code4_atr(8, 100, 58, "night"))

    def test_code2_night_high_atr_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(2, 100, 58, "night"))

    def test_code1_night_high_atr_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(1, 100, 58, "night"))

    def test_code0_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(0, 100, 58, "night"))

    # --- Fail-open on missing / invalid ATR (still night-conditioned) ---
    def test_atr_none_night_does_not_skip(self):
        # Worker bug / missing atr field — MUST NOT skip
        self.assertFalse(should_skip_code4_atr(4, None, 58, "night"))

    def test_atr_string_night_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, "100", 58, "night"))

    def test_atr_bool_night_does_not_skip(self):
        # bool is subclass of int in Python — exclude explicitly
        self.assertFalse(should_skip_code4_atr(4, True, 58, "night"))

    # --- Float ATR works (still night-conditioned) ---
    def test_atr_float_night_above_threshold_skips(self):
        self.assertTrue(should_skip_code4_atr(4, 58.5, 58, "night"))

    def test_atr_float_night_below_threshold_does_not_skip(self):
        self.assertFalse(should_skip_code4_atr(4, 57.9, 58, "night"))


if __name__ == "__main__":
    unittest.main()
