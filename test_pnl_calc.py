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


if __name__ == "__main__":
    unittest.main()
