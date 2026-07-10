"""Pure unittest for order_guard. No broker-SDK / no I/O.
Run:  python3 -m unittest test_order_guard -v
WHY: 2026-07-10 near-miss — unittest discovery imported a legacy smoke script on
the VPS and sent a REAL market order; only the broker's night-session rejection
prevented a fill. Renaming that one file removed one mine; this guard closes the
CLASS: no process may send a live order unless it is the systemd service
(TRADER_SERVICE=1, set ONLY in the unit file) or an explicitly-acked human
(TRADER_MANUAL_ORDER_ACK=1). Anything else fails closed. (BOARD T010)
"""
import unittest

from order_guard import (OrderGuardError, GuardRejectedResp,
                         assert_order_allowed)


class AssertOrderAllowed(unittest.TestCase):
    def test_service_env_allows(self):
        self.assertEqual(assert_order_allowed(env={"TRADER_SERVICE": "1"}), "service")

    def test_manual_ack_allows(self):
        self.assertEqual(
            assert_order_allowed(env={"TRADER_MANUAL_ORDER_ACK": "1"}), "manual")

    def test_bare_env_fails_closed(self):
        with self.assertRaises(OrderGuardError):
            assert_order_allowed(env={})

    def test_unrelated_env_fails_closed(self):
        with self.assertRaises(OrderGuardError):
            assert_order_allowed(env={"PATH": "/usr/bin", "HOME": "/root"})

    def test_strict_value_match_only_1(self):
        # "true"/"0"/"yes" must NOT pass — the gate is an exact contract, not truthiness.
        for bad in ("0", "true", "yes", "TRUE", " 1"):
            with self.assertRaises(OrderGuardError, msg=f"value {bad!r} must fail"):
                assert_order_allowed(env={"TRADER_SERVICE": bad})

    def test_both_set_reports_service(self):
        self.assertEqual(
            assert_order_allowed(env={"TRADER_SERVICE": "1",
                                      "TRADER_MANUAL_ORDER_ACK": "1"}), "service")

    def test_error_message_is_instructive(self):
        try:
            assert_order_allowed(env={})
        except OrderGuardError as e:
            msg = str(e)
            self.assertIn("TRADER_SERVICE", msg)
            self.assertIn("TRADER_MANUAL_ORDER_ACK", msg)
        else:
            self.fail("should have raised")


class GuardRejectedRespShape(unittest.TestCase):
    def test_mimics_sdk_rejection_contract(self):
        # trader._send_order's callers only touch .issend / .errormsg / .seq —
        # the fake rejection must satisfy that contract so the service degrades
        # to the existing "Order failed" path instead of crashing the poll loop.
        r = GuardRejectedResp("blocked by guard")
        self.assertFalse(r.issend)
        self.assertIsNone(r.seq)
        self.assertIn("blocked by guard", r.errormsg)


if __name__ == "__main__":
    unittest.main()
