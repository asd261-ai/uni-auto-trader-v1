"""Tests for order_reject. Pure stdlib unittest (system python3, no deps).
Run:  python3 -m unittest test_order_reject -v
"""
import unittest

import order_reject as orj


def _unit(source="mtx", dir_="short", id_=111, entry_fill=None):
    return {"source": source, "id": id_, "dir": dir_, "entry": 46137,
            "stop": 46290, "entry_fill": entry_fill}


def _entry(unit, bs):
    return {"kind": "entry", "bs": bs, "unit": unit}


def _exit(bs):
    return {"kind": "exit", "bs": bs}


def _exit_pe(bs, pe="PE"):
    return {"kind": "exit", "bs": bs, "pe": pe}


class IsRejectStatus(unittest.TestCase):
    def test_real_reject_codes_are_rejects(self):
        for s in ("FUF1239:同ID客戶未沖銷部位及委託保證金超過使用額度",
                  "FUF0092:無足夠留倉口數平倉                                     0",
                  "FUF0026:商品代號錯誤",
                  "TTO0001:交易時間已結束",
                  "HHO0038:市價單不允許當日有效委託"):
            self.assertTrue(orj.is_reject_status(s), s)

    def test_psc_margin_reject_is_reject(self):
        # 2026-07-17 night: 4 entries rejected PSC0019 (保證金不足) — the PSC family
        # was missing from _REJECT_PREFIXES, so rollback never fired and the units
        # lingered as phantom trades (production ordernos PY381/QI277/QI876/RG149).
        s = "PSC0019:保證金不足                                        36614          0"
        self.assertTrue(orj.is_reject_status(s), s)

    def test_success_statuses_are_not_rejects(self):
        for s in ("委託成功", "完全成交", "刪單成功", "改價成功", "", None):
            self.assertFalse(orj.is_reject_status(s), repr(s))


class IsMarginReject(unittest.TestCase):
    """Margin-family rejects drive an immediate Health-bot alert (2026-07-17 night:
    the whole-night starvation was silent because alerting depended on the polling
    margin query succeeding; the reject reply itself is the most reliable signal)."""

    def test_margin_family_codes_are_margin_rejects(self):
        for s in ("PSC0019:保證金不足                                        36614          0",
                  "FUF1239:同ID客戶未沖銷部位及委託保證金超過使用額度"):
            self.assertTrue(orj.is_margin_reject(s), s)

    def test_non_margin_rejects_are_not(self):
        # Other reject families (no-position close, session, order-type) must NOT
        # trigger the margin alert — they are not starvation signals.
        for s in ("FUF0092:無足夠留倉口數平倉", "FUF0026:商品代號錯誤",
                  "TTO0001:交易時間已結束", "HHO0038:市價單不允許當日有效委託"):
            self.assertFalse(orj.is_margin_reject(s), s)

    def test_success_and_empty_are_not(self):
        for s in ("委託成功", "完全成交", "", None):
            self.assertFalse(orj.is_margin_reject(s), repr(s))


class RollbackRejectedEntry(unittest.TestCase):
    def test_single_unfilled_entry_rolled_back(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]
        self.assertIs(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"), u)
        self.assertEqual(units["mtx"], [])
        self.assertEqual(pending, [])

    def test_foreign_product_is_noop(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFG6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 1)

    def test_bs_mismatch_is_noop(self):
        u = _unit(dir_="long")
        units = {"mtx": [u]}
        pending = [_entry(u, "B")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])

    def test_filled_entry_is_never_removed(self):
        u = _unit(entry_fill=46132)        # already filled
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 1)

    def test_two_unfilled_same_side_entries_ambiguous_noop(self):
        u1, u2 = _unit(id_=1), _unit(id_=2)
        units = {"mtx": [u1, u2]}
        pending = [_entry(u1, "S"), _entry(u2, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u1, u2])
        self.assertEqual(len(pending), 2)

    def test_pending_exit_same_side_bails(self):
        # An exit (close) reject like FUF0092 shares bs with the close order; if a same-side
        # exit is pending, the reject may be for it, not the entry → bail.
        u = _unit()
        units = {"mtx": [u]}
        pending = [_exit("S"), _entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 2)

    def test_reversal_opposite_side_entry_rolled_back(self):
        # Reversal: pending exit (close long, B) + new entry (short, S). Reject S → the
        # exit is opposite side, so the entry is unambiguous → roll it back.
        u = _unit(dir_="short")
        units = {"mtx": [u]}
        pending = [_exit("B"), _entry(u, "S")]
        self.assertIs(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"), u)
        self.assertEqual(units["mtx"], [])
        self.assertEqual(pending, [_exit("B")])

    def test_exit_reject_no_entry_candidate_is_noop(self):
        units = {"mtx": []}
        pending = [_exit("B")]
        self.assertIsNone(orj.rollback_rejected_entry(pending, units, "MXFF6", "B", "MXFF6"))
        self.assertEqual(len(pending), 1)

    def test_empty_pending_is_noop(self):
        self.assertIsNone(orj.rollback_rejected_entry([], {"mtx": []}, "MXFF6", "S", "MXFF6"))


class RollbackFreshnessWindow(unittest.TestCase):
    """2026-07-19 audit (fresh-diff lens on the PSC family addition): the shared
    account means a MANUAL same-product order's margin reject (PSC0019/FUF1239)
    also reaches on_order_rejected. If the bot's own entry has been in flight for
    a while (its reject/fill would have arrived within seconds), a late foreign
    reject must NOT roll back the bot's unit. Pends carry a ts_ms; candidates
    older than max_age_ms are excluded. No ts / no window → legacy behaviour
    (always eligible) so existing callers/tests are unaffected."""

    NOW = 1_000_000_000_000
    WINDOW = 15_000

    def _entry_ts(self, unit, bs, ts_ms):
        return {"kind": "entry", "bs": bs, "unit": unit, "ts_ms": ts_ms}

    def test_fresh_entry_still_rolled_back(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [self._entry_ts(u, "S", self.NOW - 2_000)]
        got = orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6",
                                          now_ms=self.NOW, max_age_ms=self.WINDOW)
        self.assertIs(got, u)
        self.assertEqual(units["mtx"], [])

    def test_stale_entry_not_rolled_back(self):
        # In-flight for 60s: its own broker verdict long since arrived — this
        # reject belongs to a manual order. Bail, leave it to recon.
        u = _unit()
        units = {"mtx": [u]}
        pending = [self._entry_ts(u, "S", self.NOW - 60_000)]
        self.assertIsNone(orj.rollback_rejected_entry(
            pending, units, "MXFF6", "S", "MXFF6",
            now_ms=self.NOW, max_age_ms=self.WINDOW))
        self.assertEqual(units["mtx"], [u])
        self.assertEqual(len(pending), 1)

    def test_no_window_keeps_legacy_behaviour(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [self._entry_ts(u, "S", self.NOW - 60_000)]
        self.assertIs(orj.rollback_rejected_entry(pending, units, "MXFF6", "S", "MXFF6"), u)

    def test_pend_without_ts_treated_as_fresh(self):
        u = _unit()
        units = {"mtx": [u]}
        pending = [_entry(u, "S")]   # legacy pend, no ts_ms
        self.assertIs(orj.rollback_rejected_entry(
            pending, units, "MXFF6", "S", "MXFF6",
            now_ms=self.NOW, max_age_ms=self.WINDOW), u)

    def test_stale_exit_not_rolled_back(self):
        ex = {"kind": "exit", "bs": "S", "pe": "PE", "ts_ms": self.NOW - 60_000}
        pending = [ex]
        self.assertIsNone(orj.rollback_rejected_exit(
            pending, "MXFF6", "S", "MXFF6",
            now_ms=self.NOW, max_age_ms=self.WINDOW))
        self.assertEqual(pending, [ex])

    def test_fresh_exit_still_rolled_back(self):
        ex = {"kind": "exit", "bs": "S", "pe": "PE", "ts_ms": self.NOW - 2_000}
        pending = [ex]
        got = orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6",
                                         now_ms=self.NOW, max_age_ms=self.WINDOW)
        self.assertIs(got, ex)


class RollbackRejectedExit(unittest.TestCase):
    def test_single_exit_removed_and_returned(self):
        ex = _exit_pe("S")
        pending = [ex]
        got = orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6")
        self.assertIs(got, ex)
        self.assertEqual(pending, [])

    def test_foreign_product_is_noop(self):
        ex = _exit_pe("S")
        pending = [ex]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFG6", "S", "MXFF6"))
        self.assertEqual(pending, [ex])

    def test_bs_mismatch_is_noop(self):
        ex = _exit_pe("B")
        pending = [ex]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6"))
        self.assertEqual(pending, [ex])

    def test_competing_same_bs_unfilled_entry_bails(self):
        # Ambiguous: reject for "S" could be the close OR the unfilled short entry → bail.
        u = _unit()
        ex = _exit_pe("S")
        pending = [ex, _entry(u, "S")]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6"))
        self.assertEqual(len(pending), 2)

    def test_filled_competing_entry_does_not_block(self):
        # A FILLED same-bs entry is not a reject candidate, so the exit is unambiguous.
        u = _unit(entry_fill=46130)
        ex = _exit_pe("S")
        pending = [ex, _entry(u, "S")]
        got = orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6")
        self.assertIs(got, ex)
        self.assertEqual(pending, [_entry(u, "S")])

    def test_two_same_bs_exits_ambiguous_noop(self):
        pending = [_exit_pe("S", "PE1"), _exit_pe("S", "PE2")]
        self.assertIsNone(orj.rollback_rejected_exit(pending, "MXFF6", "S", "MXFF6"))
        self.assertEqual(len(pending), 2)

    def test_empty_pending_is_noop(self):
        self.assertIsNone(orj.rollback_rejected_exit([], "MXFF6", "S", "MXFF6"))
