"""update_daily_close: atomic save (2026-07-19 audit — in-place truncate-write
raced the live trader's cross-process read in _check_regime, which could cache
'undefined' regime off a partial file)."""
import json
import os
import tempfile
import unittest

import update_daily_close as udc


class AtomicSaveTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig = udc.STATE_PATH
        udc.STATE_PATH = type(udc.STATE_PATH)(os.path.join(self.tmpdir, "daily_closes.json"))

    def tearDown(self):
        udc.STATE_PATH = self.orig

    def test_roundtrip_and_no_tmp_leftover(self):
        udc.save_state([{"date": "2026-07-18", "close": 44500, "source": "manual"}])
        self.assertEqual(udc.load_state()[0]["close"], 44500)
        leftovers = [f for f in os.listdir(self.tmpdir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_uses_replace_not_truncate(self):
        # Pin the atomic pattern: a same-directory temp file renamed into place.
        import inspect
        src = inspect.getsource(udc.save_state)
        self.assertIn("replace", src)


if __name__ == "__main__":
    unittest.main()
