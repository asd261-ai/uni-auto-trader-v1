"""Unit tests for the broker-fill schema gate (feed_schema.parse_fill).

WHY this gate exists: a Match object reaches order_log (the orders.jsonl P&L
source) and the fill FIFO / FILL_ANCHOR. A junk matchprice (0, negative, NaN,
inf) would contaminate realised P&L — and could trip DAILY_MAX_LOSS_PTS or
re-anchor a live stop to garbage. So every test below asserts that a *specific*
class of garbage is rejected (returns None) BEFORE it can touch money, and that
legitimate fills survive unchanged.

Pure-function tests: feed_schema imports with no broker-SDK dependency, so this
runs locally and on the VPS via `python3 -m unittest test_fill_schema -v`.
"""
import math
import unittest

from feed_schema import parse_fill, _MAX_SANE_QTY


class TestParseFillAccepts(unittest.TestCase):
    """Legitimate fills must pass through with exact, typed values."""

    def test_valid_buy_returns_price_qty(self):
        self.assertEqual(parse_fill("B", 21000.0, 1), (21000.0, 1))

    def test_valid_sell_returns_price_qty(self):
        self.assertEqual(parse_fill("S", 21000.0, 2), (21000.0, 2))

    def test_numeric_string_price_is_coerced(self):
        # Broker SDK sometimes hands back stringified numerics; must not reject.
        self.assertEqual(parse_fill("B", "21000.5", "3"), (21000.5, 3))

    def test_qty_at_max_sane_is_accepted(self):
        # Upper bound is inclusive — exactly _MAX_SANE_QTY is still a real fill.
        self.assertEqual(parse_fill("S", 21000.0, _MAX_SANE_QTY), (21000.0, _MAX_SANE_QTY))


class TestParseFillRejectsBadSide(unittest.TestCase):
    """A wrong/empty side would mis-attribute P&L direction → must reject."""

    def test_unknown_side_rejected(self):
        self.assertIsNone(parse_fill("X", 21000.0, 1))

    def test_empty_side_rejected(self):
        self.assertIsNone(parse_fill("", 21000.0, 1))

    def test_none_side_rejected(self):
        self.assertIsNone(parse_fill(None, 21000.0, 1))


class TestParseFillRejectsBadPrice(unittest.TestCase):
    """Junk prices are the contamination this gate primarily exists to stop."""

    def test_zero_price_rejected(self):
        # 0 reads as a "free" fill — would wreck FIFO P&L.
        self.assertIsNone(parse_fill("B", 0, 1))

    def test_negative_price_rejected(self):
        self.assertIsNone(parse_fill("B", -21000.0, 1))

    def test_nan_price_rejected(self):
        # NaN > 0 is False, so the band rejects it; this is the comment's claim.
        self.assertIsNone(parse_fill("B", float("nan"), 1))

    def test_positive_inf_price_rejected(self):
        self.assertIsNone(parse_fill("B", float("inf"), 1))

    def test_negative_inf_price_rejected(self):
        self.assertIsNone(parse_fill("B", float("-inf"), 1))

    def test_absurdly_large_price_rejected(self):
        self.assertIsNone(parse_fill("B", 1_000_000, 1))

    def test_none_price_rejected(self):
        self.assertIsNone(parse_fill("B", None, 1))

    def test_non_numeric_price_rejected(self):
        self.assertIsNone(parse_fill("B", "abc", 1))


class TestParseFillRejectsBadQty(unittest.TestCase):
    """Qty out of sane range = fat-finger / SDK drift → reject, don't book it."""

    def test_zero_qty_rejected(self):
        self.assertIsNone(parse_fill("B", 21000.0, 0))

    def test_negative_qty_rejected(self):
        self.assertIsNone(parse_fill("B", 21000.0, -1))

    def test_over_max_qty_rejected(self):
        self.assertIsNone(parse_fill("B", 21000.0, _MAX_SANE_QTY + 1))

    def test_non_numeric_qty_rejected(self):
        self.assertIsNone(parse_fill("B", 21000.0, "abc"))

    def test_none_qty_rejected(self):
        self.assertIsNone(parse_fill("B", 21000.0, None))


if __name__ == "__main__":
    unittest.main()
