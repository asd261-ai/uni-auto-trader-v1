"""orderno_claim: live sent→reply orderno claiming (P4-2, 2026-07-20 design).

Mirrors pnl_calc.bot_ordernos' proven semantics (2026-06-19 provenance design)
for the LIVE callback path, so fills/rejects from Sean's MANUAL orders on the
shared account can be told apart from the bot's own.

Run:  python3 -m unittest test_orderno_claim -v
"""
import unittest

from orderno_claim import OrdernoClaimer


class ClaimBasics(unittest.TestCase):
    def setUp(self):
        self.c = OrdernoClaimer(link_window_sec=3)

    def test_reply_within_window_claims(self):
        self.c.note_sent(100.0, "MXFH6", "B")
        self.assertTrue(self.c.note_reply(100.4, "MXFH6", "B", "PY001"))
        self.assertTrue(self.c.is_bot_fill("PY001"))

    def test_foreign_reply_without_sent_not_claimed(self):
        # Manual order's reply: no outstanding bot sent → foreign.
        self.assertFalse(self.c.note_reply(100.0, "MXFH6", "B", "QI999"))
        self.assertFalse(self.c.is_bot_fill("QI999"))

    def test_bs_mismatch_not_claimed(self):
        self.c.note_sent(100.0, "MXFH6", "B")
        self.assertFalse(self.c.note_reply(100.5, "MXFH6", "S", "PY002"))

    def test_product_mismatch_not_claimed(self):
        self.c.note_sent(100.0, "MXFH6", "B")
        self.assertFalse(self.c.note_reply(100.5, "TXFH6", "B", "PY003"))

    def test_reply_after_window_not_claimed(self):
        self.c.note_sent(100.0, "MXFH6", "B")
        self.assertFalse(self.c.note_reply(104.5, "MXFH6", "B", "PY004"))

    def test_only_first_reply_per_orderno_claims(self):
        # Broker emits multiple replies per order (委託成功→部分成交→…): a dup
        # reply must not shadow the NEXT order's claim (2026-07-06 incident).
        self.c.note_sent(100.0, "MXFH6", "S")
        self.assertTrue(self.c.note_reply(100.2, "MXFH6", "S", "PY005"))
        self.c.note_sent(101.0, "MXFH6", "S")
        # Dup reply for an already-claimed orderno: still "ours" (True) but must
        # NOT consume the new sent — PY006's claim below proves it didn't.
        self.assertTrue(self.c.note_reply(101.1, "MXFH6", "S", "PY005"))
        self.assertTrue(self.c.note_reply(101.2, "MXFH6", "S", "PY006"))
        self.assertTrue(self.c.is_bot_fill("PY006"))

    def test_each_sent_claimed_once(self):
        # One sent can back only one orderno — a second (manual) reply in the
        # window must not ride the already-consumed sent.
        self.c.note_sent(100.0, "MXFH6", "B")
        self.assertTrue(self.c.note_reply(100.2, "MXFH6", "B", "PY007"))
        self.assertFalse(self.c.note_reply(100.4, "MXFH6", "B", "QI777"))

    def test_oldest_sent_claimed_first(self):
        self.c.note_sent(100.0, "MXFH6", "B")
        self.c.note_sent(100.5, "MXFH6", "B")
        self.assertTrue(self.c.note_reply(100.9, "MXFH6", "B", "PY008"))
        self.assertTrue(self.c.note_reply(101.0, "MXFH6", "B", "PY009"))
        self.assertTrue(self.c.is_bot_fill("PY008"))
        self.assertTrue(self.c.is_bot_fill("PY009"))

    def test_bot_set_capped(self):
        c = OrdernoClaimer(link_window_sec=3, max_ordernos=5)
        for i in range(8):
            c.note_sent(100.0 + i, "MXFH6", "B")
            self.assertTrue(c.note_reply(100.1 + i, "MXFH6", "B", f"PY{i:03d}"))
        self.assertFalse(c.is_bot_fill("PY000"))   # oldest evicted
        self.assertTrue(c.is_bot_fill("PY007"))


if __name__ == "__main__":
    unittest.main()
