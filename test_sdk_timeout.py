"""Tests for sdk_timeout. Pure stdlib unittest (runs on system python3, no deps).
Run:  python3 -m unittest test_sdk_timeout -v
"""
from __future__ import annotations

import time
import unittest

from sdk_timeout import call_with_timeout, SDKCallTimeout


class CallWithTimeoutTests(unittest.TestCase):
    def test_returns_value_when_fast(self):
        self.assertEqual(call_with_timeout(lambda: 42, timeout=1.0), 42)

    def test_passes_args_and_kwargs(self):
        self.assertEqual(call_with_timeout(lambda a, b: a + b, 2, b=3, timeout=1.0), 5)

    def test_raises_on_timeout(self):
        def slow():
            time.sleep(5)
        with self.assertRaises(SDKCallTimeout):
            call_with_timeout(slow, timeout=0.1)

    def test_propagates_fn_exception(self):
        def boom():
            raise ValueError("nope")
        with self.assertRaises(ValueError):
            call_with_timeout(boom, timeout=1.0)

    def test_stuck_call_does_not_block_next_call(self):
        def slow():
            time.sleep(2)
        t0 = time.monotonic()
        with self.assertRaises(SDKCallTimeout):
            call_with_timeout(slow, timeout=0.1)             # abandons the stuck thread
        self.assertEqual(call_with_timeout(lambda: "ok", timeout=0.5), "ok")  # next call works at once
        self.assertLess(time.monotonic() - t0, 1.5)          # didn't wait for the 2s stuck call


if __name__ == "__main__":
    unittest.main()
