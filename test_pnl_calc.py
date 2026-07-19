"""Tests for pnl_calc.summarize_real_pnl — the real-fill daily/month P&L summary.
Pure stdlib unittest (system python3, no deps). Run: python3 -m unittest test_pnl_calc -v

2026-06-18 fix: real P&L now sums each trade's own stamped pnl_pts_real from
trades.jsonl (bot-only, self-paired) instead of FIFO-ing the whole orders.jsonl
history — which mis-paired today's first close against a stale 5-day-old leg
(reported +2176/+322 vs real -47/-54) and could be contaminated by Sean's manual
shared-account trades. These tests pin the correct behaviour.
"""
import unittest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pnl_calc import summarize_real_pnl, _parse_iso, _trading_day_window, bot_ordernos


def rec(trading_day, real, signal=0.0, source="mtx"):
    return {"trading_day": trading_day, "pnl_pts_real": real, "pnl_pts": signal, "source": source}


def _sent(ts, pid="MXFG6", bs="B"):
    return {"ts": ts, "event": "sent", "productid": pid, "bs": bs}


def _reply(ts, ono, pid="MXFG6", bs="B"):
    return {"ts": ts, "event": "reply", "productid": pid, "bs": bs, "orderno": ono}


class SummarizeRealPnlTest(unittest.TestCase):
    def test_sums_today_real_fills_only(self):
        recs = [rec("2026-06-18", 36), rec("2026-06-18", -54), rec("2026-06-17", 999)]
        out = summarize_real_pnl(recs, "2026-06-18")
        self.assertEqual(out["real_trading_day_pnl_pts"], -18)   # 36 + -54
        self.assertEqual(out["real_trading_day_trades"], 2)      # yesterday excluded

    def test_stale_other_day_trade_cannot_inflate_today(self):
        # The core regression: a huge value on ANOTHER trading day must not leak into
        # today (the old FIFO let a 6/12 stale leg inflate today's first close to +2176).
        recs = [rec("2026-06-12", 2176), rec("2026-06-18", -47)]
        out = summarize_real_pnl(recs, "2026-06-18")
        self.assertEqual(out["real_trading_day_pnl_pts"], -47)

    def test_missing_fill_excluded_not_inflated(self):
        # pnl_pts_real None (paper trade OR genuinely-missing fill) is excluded from the
        # P&L sum (never falls back to the signal value) and counted separately.
        recs = [rec("2026-06-18", 30), rec("2026-06-18", None, signal=120)]
        out = summarize_real_pnl(recs, "2026-06-18")
        self.assertEqual(out["real_trading_day_pnl_pts"], 30)    # the None NOT added
        self.assertEqual(out["real_trading_day_trades"], 1)
        self.assertEqual(out["real_day_missing_fill"], 1)

    def test_month_sum_spans_days_excludes_other_month(self):
        recs = [rec("2026-06-01", 10), rec("2026-06-18", 5), rec("2026-05-30", 999)]
        out = summarize_real_pnl(recs, "2026-06-18")
        self.assertEqual(out["real_month_pnl_pts"], 15)          # June only

    def test_combines_mtx_and_fvg_bot_trades(self):
        # Both live bot sources count; trades.jsonl never holds manual trades.
        recs = [rec("2026-06-18", 78, source="mtx"), rec("2026-06-18", -11, source="fvg")]
        out = summarize_real_pnl(recs, "2026-06-18")
        self.assertEqual(out["real_trading_day_pnl_pts"], 67)
        self.assertEqual(out["real_trading_day_trades"], 2)

    def test_empty_is_flat_zero(self):
        out = summarize_real_pnl([], "2026-06-18")
        self.assertEqual(out["real_trading_day_pnl_pts"], 0)
        self.assertEqual(out["real_trading_day_trades"], 0)
        self.assertEqual(out["real_month_pnl_pts"], 0)
        self.assertEqual(out["real_day_missing_fill"], 0)


class TimeHelpersTest(unittest.TestCase):
    def test_parse_iso_with_offset(self):
        dt = _parse_iso("2026-06-19T02:52:40+08:00")
        self.assertEqual(dt, datetime(2026, 6, 19, 2, 52, 40,
                                      tzinfo=timezone(timedelta(hours=8))))

    def test_parse_iso_bad_returns_none(self):
        self.assertIsNone(_parse_iso("not-a-timestamp"))
        self.assertIsNone(_parse_iso(None))

    def test_trading_day_window_spans_0845_to_0845(self):
        start, end = _trading_day_window("2026-06-18")
        tz = ZoneInfo("Asia/Taipei")
        self.assertEqual(start, datetime(2026, 6, 18, 8, 45, tzinfo=tz))
        self.assertEqual(end,   datetime(2026, 6, 19, 8, 45, tzinfo=tz))

    def test_window_includes_after_midnight_night_fill(self):
        # a night-session fill at 02:52 on 6/19 belongs to trading-day 6/18
        start, end = _trading_day_window("2026-06-18")
        night = _parse_iso("2026-06-19T02:52:40+08:00")
        self.assertTrue(start <= night < end)

    def test_window_lower_boundary_is_start(self):
        # 08:45:00 sharp is the inclusive lower edge of the window.
        start, end = _trading_day_window("2026-06-18")
        self.assertEqual(start, _parse_iso("2026-06-18T08:45:00+08:00"))

    def test_consecutive_windows_have_no_gap(self):
        # end of one trading day == start of the next: half-open [start,end) tiling,
        # no 1-second hole that could drop a fill for the fuse.
        _, end_prev = _trading_day_window("2026-06-17")
        start_next, _ = _trading_day_window("2026-06-18")
        self.assertEqual(end_prev, start_next)


class BotOrdernosTest(unittest.TestCase):
    def test_sent_backed_reply_is_bot(self):
        rows = [_sent("2026-06-18T10:00:00+08:00"),
                _reply("2026-06-18T10:00:00+08:00", "QN001")]
        self.assertEqual(bot_ordernos(rows), {"QN001"})

    def test_orphan_reply_no_sent_is_manual(self):
        # Sean's manual MXFH6 fill: a reply/match with no preceding bot 'sent'.
        rows = [_reply("2026-06-18T10:00:00+08:00", "QN999", pid="MXFH6")]
        self.assertEqual(bot_ordernos(rows), set())

    def test_two_sents_same_second_claim_distinct_replies(self):
        rows = [_sent("2026-06-18T10:00:00+08:00"),
                _sent("2026-06-18T10:00:00+08:00"),
                _reply("2026-06-18T10:00:00+08:00", "QN001"),
                _reply("2026-06-18T10:00:00+08:00", "QN002")]
        self.assertEqual(bot_ordernos(rows), {"QN001", "QN002"})

    def test_reply_outside_3s_window_not_matched(self):
        rows = [_sent("2026-06-18T10:00:00+08:00"),
                _reply("2026-06-18T10:00:10+08:00", "QN001")]  # 10s later
        self.assertEqual(bot_ordernos(rows), set())

    def test_different_product_not_matched(self):
        rows = [_sent("2026-06-18T10:00:00+08:00", pid="MXFG6"),
                _reply("2026-06-18T10:00:00+08:00", "QN001", pid="MXFH6")]
        self.assertEqual(bot_ordernos(rows), set())

    def test_duplicate_replies_do_not_shadow_second_order(self):
        # Real incident 2026-07-06 13:24:35 (reverse: close-long + open-short, both S):
        # broker emitted TWO reply events per orderno. The second sent claimed 0I934's
        # duplicate reply, leaving 0I935 unclaimed -> its fill dropped from FIFO ->
        # PNL_DIVERGENCE cascade (orders-FIFO +102 vs per-trade -310).
        rows = [_sent("2026-07-06T13:24:34+08:00", bs="S"),
                _sent("2026-07-06T13:24:34+08:00", bs="S"),
                _reply("2026-07-06T13:24:34+08:00", "0I934", bs="S"),
                _reply("2026-07-06T13:24:35+08:00", "0I934", bs="S"),
                _reply("2026-07-06T13:24:35+08:00", "0I935", bs="S"),
                _reply("2026-07-06T13:24:35+08:00", "0I935", bs="S")]
        self.assertEqual(bot_ordernos(rows), {"0I934", "0I935"})


from pnl_calc import realized_day_pts


def _match(ts, ono, bs, price, pid="MXFG6", qty=1):
    return {"ts": ts, "event": "match", "productid": pid, "bs": bs,
            "orderno": ono, "matchprice": price, "matchqty": qty}


class RealizedDayPtsTest(unittest.TestCase):
    def setUp(self):
        self.start, self.end = _trading_day_window("2026-06-18")

    def _long_roundtrip(self, t1, t2, ono_in, ono_out, ein, eout):
        # bot long: sent+reply open (B), sent+reply close (S)
        return [_sent(t1, bs="B"), _reply(t1, ono_in, bs="B"),
                _match(t1, ono_in, "B", ein),
                _sent(t2, bs="S"), _reply(t2, ono_out, bs="S"),
                _match(t2, ono_out, "S", eout)]

    def test_bot_only_roundtrip_realizes_correctly(self):
        rows = self._long_roundtrip("2026-06-18T10:00:00+08:00",
                                    "2026-06-18T10:10:00+08:00",
                                    "QN1", "QN2", 46430.0, 46508.0)
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, 78.0)   # long: 46508 - 46430
        self.assertEqual(trips, 1)

    def test_manual_fill_no_sent_excluded(self):
        rows = self._long_roundtrip("2026-06-18T10:00:00+08:00",
                                    "2026-06-18T10:10:00+08:00",
                                    "QN1", "QN2", 46430.0, 46508.0)
        # Sean's manual MXFH6 round-trip: match events with NO sent backing them.
        rows += [_match("2026-06-18T11:00:00+08:00", "M1", "S", 50000.0, pid="MXFH6"),
                 _match("2026-06-18T11:05:00+08:00", "M2", "B", 49000.0, pid="MXFH6")]
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, 78.0)   # manual MXFH6 ignored
        self.assertEqual(trips, 1)

    def test_two_products_fifo_independently_no_cross_pair(self):
        # settlement-day style: bot fills on old + new contract; each FIFOs alone.
        rows = self._long_roundtrip("2026-06-18T10:00:00+08:00",
                                    "2026-06-18T10:10:00+08:00",
                                    "QN1", "QN2", 46430.0, 46508.0)  # MXFG6 +78
        g = [_sent("2026-06-18T11:00:00+08:00", pid="MXFH6", bs="S"),
             _reply("2026-06-18T11:00:00+08:00", "QN3", pid="MXFH6", bs="S"),
             _match("2026-06-18T11:00:00+08:00", "QN3", "S", 47000.0, pid="MXFH6"),
             _sent("2026-06-18T11:10:00+08:00", pid="MXFH6", bs="B"),
             _reply("2026-06-18T11:10:00+08:00", "QN4", pid="MXFH6", bs="B"),
             _match("2026-06-18T11:10:00+08:00", "QN4", "B", 46980.0, pid="MXFH6")]  # short +20
        pts, trips = realized_day_pts(rows + g, self.start, self.end)
        self.assertEqual(pts, 98.0)   # 78 (MXFG6 long) + 20 (MXFH6 short), no cross-pair
        self.assertEqual(trips, 2)

    def test_null_pnl_pts_real_trade_still_captured(self):
        # The +170-vs-+167 case: a trade whose exit_fill was never stamped (so
        # trades.jsonl pnl_pts_real is None) STILL has real match fills here.
        rows = self._long_roundtrip("2026-06-19T03:00:00+08:00",
                                    "2026-06-19T03:05:00+08:00",
                                    "QN1", "QN2", 47545.0, 47480.0)  # long: 47480-47545 = -65
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, -65.0)
        self.assertEqual(trips, 1)

    def test_stale_leg_before_window_excluded(self):
        # An unmatched bot leg dated before the window must not pair with today's
        # close (the +2176 regression). Only the in-window open should remain open.
        rows = [_sent("2026-06-12T10:00:00+08:00", bs="B"),
                _reply("2026-06-12T10:00:00+08:00", "OLD", bs="B"),
                _match("2026-06-12T10:00:00+08:00", "OLD", "B", 43946.0),  # 6/12 stale
                _sent("2026-06-18T10:00:00+08:00", bs="S"),
                _reply("2026-06-18T10:00:00+08:00", "QN2", bs="S"),
                _match("2026-06-18T10:00:00+08:00", "QN2", "S", 46508.0)]  # today close
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, 0.0)   # stale 6/12 leg out of window; today's lone S stays open
        self.assertEqual(trips, 0)

    def test_empty_is_flat_zero(self):
        self.assertEqual(realized_day_pts([], self.start, self.end), (0.0, 0))


class RealizedDayPtsCarryTest(unittest.TestCase):
    """2026-07-16 fix: a position opened in the PREVIOUS trading-day window and
    closed after 08:45 must pair with its real open leg (bounded carry lookback),
    not be mis-read as a fresh open that corrupts the whole day's FIFO
    (orders-FIFO -373 vs broker/per-trade -153 on 2026-07-16)."""

    def setUp(self):
        # trading day 2026-07-16: window 07-16 08:45 → 07-17 08:45
        self.start, self.end = _trading_day_window("2026-07-16")

    @staticmethod
    def _leg(ts, ono, bs, price, pid="MXFH6"):
        return [_sent(ts, pid=pid, bs=bs), _reply(ts, ono, pid=pid, bs=bs),
                _match(ts, ono, bs, price, pid=pid)]

    def _carry_rows(self):
        # night-session buy at 03:48 (belongs to trading day 07-15) carried
        # across 08:45, stopped out 08:50 — the real 2026-07-16 incident shape.
        return (self._leg("2026-07-16T03:48:19+08:00", "C1", "B", 45823.0)
                + self._leg("2026-07-16T08:50:00+08:00", "C2", "S", 45531.0))

    def test_carry_across_0845_boundary_pairs_with_real_open_leg(self):
        pts, trips = realized_day_pts(self._carry_rows(), self.start, self.end)
        self.assertEqual(pts, -292.0)   # 45531 - 45823, the broker-true loss
        self.assertEqual(trips, 1)

    def test_carry_close_does_not_cascade_into_day_trades(self):
        # After the carried close, the day's own round-trips must pair cleanly
        # (the old mis-pairing corrupted every later pair: -373 vs true -182 here).
        rows = (self._carry_rows()
                + self._leg("2026-07-16T09:01:36+08:00", "D1", "S", 45622.0)
                + self._leg("2026-07-16T09:02:31+08:00", "D2", "B", 45553.0)
                + self._leg("2026-07-16T09:04:43+08:00", "D3", "S", 45561.0)
                + self._leg("2026-07-16T09:07:44+08:00", "D4", "B", 45520.0))
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, -182.0)   # -292 + 69 + 41
        self.assertEqual(trips, 3)

    def test_carry_roundtrip_not_double_counted_on_prior_day(self):
        # The same round-trip queried for trading day 07-15 realizes NOTHING there
        # (its close is outside that window) — no double count across days.
        prev_start, prev_end = _trading_day_window("2026-07-15")
        pts, trips = realized_day_pts(self._carry_rows(), prev_start, prev_end)
        self.assertEqual(pts, 0.0)
        self.assertEqual(trips, 0)

    def test_prior_day_completed_roundtrip_not_counted_today(self):
        # A round-trip fully inside the previous trading day contributes zero today
        # even though its fills now sit inside the carry-lookback scan range.
        rows = (self._leg("2026-07-15T16:00:00+08:00", "P1", "B", 46100.0)
                + self._leg("2026-07-15T16:30:00+08:00", "P2", "S", 46150.0)
                + self._leg("2026-07-16T10:00:00+08:00", "T1", "B", 45600.0)
                + self._leg("2026-07-16T10:30:00+08:00", "T2", "S", 45650.0))
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, 50.0)     # today's +50 only; yesterday's +50 excluded
        self.assertEqual(trips, 1)

    def test_leg_older_than_lookback_still_ignored(self):
        # Stale-leg protection keeps its bound: an unmatched leg older than the
        # carry lookback must NOT absorb today's close (the +2176 regression class).
        rows = (self._leg("2026-07-05T10:00:00+08:00", "OLD", "B", 43946.0)
                + self._leg("2026-07-16T10:00:00+08:00", "N1", "S", 45650.0))
        pts, trips = realized_day_pts(rows, self.start, self.end)
        self.assertEqual(pts, 0.0)      # lone in-window S stays open, no pairing
        self.assertEqual(trips, 0)


class FlatCheckpointFifoTest(unittest.TestCase):
    """P4-1 (2026-07-19 audit → 2026-07-20 design): an orphan close leg (its
    open's match event lost to a disconnect) used to be mis-read as an open leg
    and poison FIFO pairing for up to 5 lookback days. A `flat` event in
    orders.jsonl marks 'bot truly flat here' — the FIFO drops any unmatched
    legs at the marker (data hole absorbed, nothing counted for them)."""

    START = datetime(2026, 7, 17, 8, 45, tzinfo=timezone(timedelta(hours=8)))
    END   = datetime(2026, 7, 17, 13, 45, tzinfo=timezone(timedelta(hours=8)))

    def _sent_reply_match(self, ts_iso, bs, price, ono):
        return [
            {"event": "sent",  "ts": ts_iso, "productid": "MXFH6", "bs": bs},
            {"event": "reply", "ts": ts_iso, "productid": "MXFH6", "bs": bs, "orderno": ono},
            {"event": "match", "ts": ts_iso, "productid": "MXFH6", "bs": bs,
             "orderno": ono, "matchprice": price, "matchqty": 1},
        ]

    def test_orphan_close_before_checkpoint_absorbed(self):
        # Day-1: orphan S (its B open's match was lost). Then a flat checkpoint.
        # Day-2 window: clean B→S round trip must pay exactly its own pnl.
        rows = []
        rows += self._sent_reply_match("2026-07-16T10:00:00+08:00", "S", 44700, "A1")  # orphan close
        rows.append({"event": "flat", "ts": "2026-07-16T10:00:05+08:00"})
        rows += self._sent_reply_match("2026-07-17T09:00:00+08:00", "B", 44500, "B1")
        rows += self._sent_reply_match("2026-07-17T10:00:00+08:00", "S", 44600, "B2")
        pts, trips = realized_day_pts(rows, self.START, self.END)
        self.assertEqual(pts, 100.0)   # NOT 44700-44500=200 (orphan mis-pair)
        self.assertEqual(trips, 1)

    def test_no_checkpoint_keeps_legacy_behavior(self):
        # Without a marker the orphan still mis-pairs — documents that the fix
        # requires checkpoints (old files unchanged, no silent history rewrite).
        rows = []
        rows += self._sent_reply_match("2026-07-16T10:00:00+08:00", "S", 44700, "A1")
        rows += self._sent_reply_match("2026-07-17T09:00:00+08:00", "B", 44500, "B1")
        rows += self._sent_reply_match("2026-07-17T10:00:00+08:00", "S", 44600, "B2")
        pts, trips = realized_day_pts(rows, self.START, self.END)
        self.assertEqual(trips, 1)     # legacy poison: orphan S pairs with B1 → bogus +200, B2 dangles
        self.assertEqual(pts, 200.0)

    def test_checkpoint_with_clean_pairs_is_noop(self):
        rows = []
        rows += self._sent_reply_match("2026-07-17T09:00:00+08:00", "B", 44500, "C1")
        rows += self._sent_reply_match("2026-07-17T09:30:00+08:00", "S", 44550, "C2")
        rows.append({"event": "flat", "ts": "2026-07-17T09:30:05+08:00"})
        rows += self._sent_reply_match("2026-07-17T10:00:00+08:00", "B", 44600, "C3")
        rows += self._sent_reply_match("2026-07-17T11:00:00+08:00", "S", 44680, "C4")
        pts, trips = realized_day_pts(rows, self.START, self.END)
        self.assertEqual(pts, 130.0)   # 50 + 80, marker between complete pairs = no-op
        self.assertEqual(trips, 2)

    def test_same_second_marker_sorts_after_fill(self):
        # The flat transition is detected AFTER the closing fill; a same-second
        # marker must not split the pair it follows.
        rows = []
        rows += self._sent_reply_match("2026-07-17T09:00:00+08:00", "B", 44500, "D1")
        rows += self._sent_reply_match("2026-07-17T09:30:00+08:00", "S", 44550, "D2")
        rows.append({"event": "flat", "ts": "2026-07-17T09:30:00+08:00"})  # same second
        pts, trips = realized_day_pts(rows, self.START, self.END)
        self.assertEqual(pts, 50.0)
        self.assertEqual(trips, 1)


from pnl_calc import divergence_warn


class DivergenceWarnTest(unittest.TestCase):
    def test_agree_no_warn(self):
        self.assertFalse(divergence_warn(170.0, 170.0, 0))

    def test_disagree_with_missing_fills_no_warn(self):
        # +170 FIFO vs +167 per-trade is EXPECTED when 3 exits were unstamped.
        self.assertFalse(divergence_warn(170.0, 167.0, 3))

    def test_disagree_zero_missing_warns(self):
        # disagreement with nothing missing => provenance/data problem.
        self.assertTrue(divergence_warn(170.0, 150.0, 0))

    def test_within_tolerance_no_warn(self):
        self.assertFalse(divergence_warn(170.0, 169.5, 0))

    def test_none_fifo_no_warn(self):
        self.assertFalse(divergence_warn(None, 167.0, 0))

    def test_open_position_suppresses_warn(self):
        # 2026-07-17 13:21–13:28: FIFO vs per-trade legitimately diverge while
        # overlapping positions are open (attribution transient) and reconverge at
        # flat — an in-position WARNING is structural noise, not a data smell.
        self.assertFalse(divergence_warn(170.0, 150.0, 0, is_flat=False))

    def test_flat_divergence_still_warns(self):
        # Both real bugs (2026-06-24, 2026-07-16) still diverged AT flat — the
        # flat-gate must not lose those true positives.
        self.assertTrue(divergence_warn(170.0, 150.0, 0, is_flat=True))


class BotIsFlatTest(unittest.TestCase):
    """_bot_is_flat: flat means NO open units in EITHER source — mtx_state.json
    (mtx_units) AND fvg_state.json (fvg_units). An FVG-only position must not count
    as flat or the divergence flat-gate breaks for FVG-side overlaps."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_mtx, self._orig_fvg = pc._STATE_PATH, pc._FVG_STATE_PATH
        pc._STATE_PATH = os.path.join(self.tmp, "mtx_state.json")
        pc._FVG_STATE_PATH = os.path.join(self.tmp, "fvg_state.json")

    def tearDown(self):
        pc._STATE_PATH, pc._FVG_STATE_PATH = self._orig_mtx, self._orig_fvg

    def _write(self, path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_both_empty_is_flat(self):
        self._write(pc._STATE_PATH, {"product": "MXFH6", "mtx_units": []})
        self._write(pc._FVG_STATE_PATH, {"fvg_units": []})
        self.assertTrue(pc._bot_is_flat())

    def test_mtx_unit_open_not_flat(self):
        self._write(pc._STATE_PATH, {"product": "MXFH6",
                                     "mtx_units": [{"dir": "long", "entry": 44500}]})
        self._write(pc._FVG_STATE_PATH, {"fvg_units": []})
        self.assertFalse(pc._bot_is_flat())

    def test_fvg_only_position_not_flat(self):
        self._write(pc._STATE_PATH, {"product": "MXFH6", "mtx_units": []})
        self._write(pc._FVG_STATE_PATH, {"fvg_units": [{"dir": "long", "entry": 44400}]})
        self.assertFalse(pc._bot_is_flat())

    def test_missing_files_fail_open_to_flat(self):
        # Unreadable state → treat as flat (keep evaluating the check, matching
        # _real_open()'s exception → "flat" convention).
        self.assertTrue(pc._bot_is_flat())


import json, os, tempfile
import pnl_calc as pc


class ComputeWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, name, lines):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(l + "\n")
        return p

    def test_read_orders_raw_missing_file_raises(self):
        # _read_orders_raw on a non-existent path raises -> _compute maps to None.
        with self.assertRaises(Exception):
            pc._read_orders_raw(os.path.join(self.tmp, "nope.jsonl"))

    def test_read_orders_raw_skips_bad_lines(self):
        p = self._write("orders.jsonl",
                        ['{"event":"match","orderno":"Q1"}', 'GARBAGE', '{"event":"sent"}'])
        rows = pc._read_orders_raw(p)
        self.assertEqual(len(rows), 2)   # garbage skipped

    def test_read_orders_raw_empty_file_is_empty_list(self):
        p = self._write("orders.jsonl", [])
        self.assertEqual(pc._read_orders_raw(p), [])


class TestConservativeDayPnl(unittest.TestCase):
    """conservative_day_pnl: canonical breaker/display value = the more-pessimistic of
    the two real engines. Regression for 2026-06-24: orders-FIFO read +382 (a position
    spanning the 08:45 boundary mis-paired) while per-trade + broker = -421; the breaker
    and the per-close display must surface -421, never the optimistic +382."""

    def test_picks_pertrade_when_fifo_wrongly_positive_0624(self):
        # The real 2026-06-24 divergence: orders-FIFO +382 vs per-trade/broker -421.
        self.assertEqual(pc.conservative_day_pnl(382.0, 9, -421.0, 10), (-421.0, 10))

    def test_picks_fifo_when_pertrade_optimistic_missing_fill(self):
        # per-trade undercounts a loss (missing exit fill); orders-FIFO is more negative.
        self.assertEqual(pc.conservative_day_pnl(-500.0, 8, -400.0, 7), (-500.0, 8))

    def test_both_negative_takes_more_negative(self):
        self.assertEqual(pc.conservative_day_pnl(-100.0, 3, -250.0, 4), (-250.0, 4))

    def test_both_positive_takes_smaller(self):
        self.assertEqual(pc.conservative_day_pnl(300.0, 6, 120.0, 5), (120.0, 5))

    def test_tie_prefers_pertrade(self):
        self.assertEqual(pc.conservative_day_pnl(-120.0, 5, -120.0, 5), (-120.0, 5))

    def test_fifo_none_falls_back_to_pertrade(self):
        self.assertEqual(pc.conservative_day_pnl(None, None, -50.0, 2), (-50.0, 2))

    def test_pertrade_none_falls_back_to_fifo(self):
        self.assertEqual(pc.conservative_day_pnl(75.0, 1, None, None), (75.0, 1))

    def test_both_none_fails_open(self):
        # Both engines no-data → (None, None) so the breaker fail-opens (no lock).
        self.assertEqual(pc.conservative_day_pnl(None, None, None, None), (None, None))


if __name__ == "__main__":
    unittest.main()
