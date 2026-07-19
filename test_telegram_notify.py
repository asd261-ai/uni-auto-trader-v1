"""telegram_notify: 429 Retry-After cap (2026-07-19 audit — an uncapped
server-supplied flood-wait ran time.sleep(minutes) synchronously on the poll
thread, freezing exits). Stubs `requests` so the module imports on machines
without it."""
import sys
import types
import unittest

if "requests" not in sys.modules:
    _rq = types.ModuleType("requests")
    class _RequestException(Exception):
        pass
    _rq.RequestException = _RequestException
    _rq.post = lambda *a, **k: (_ for _ in ()).throw(_RequestException("stub"))
    sys.modules["requests"] = _rq

from telegram_notify import _capped_retry_after, _RETRY_AFTER_CAP_SEC


class CappedRetryAfter(unittest.TestCase):
    def test_small_value_passes_through(self):
        self.assertEqual(_capped_retry_after("5"), 5)

    def test_flood_wait_minutes_is_capped(self):
        self.assertEqual(_capped_retry_after("300"), _RETRY_AFTER_CAP_SEC)

    def test_missing_or_garbage_defaults_small(self):
        self.assertEqual(_capped_retry_after(None), 5)
        self.assertEqual(_capped_retry_after("soon"), 5)

    def test_cap_is_poll_thread_safe(self):
        # The poll thread owns exits — a blocked send must resolve in seconds.
        self.assertLessEqual(_RETRY_AFTER_CAP_SEC, 15)


if __name__ == "__main__":
    unittest.main()
