# pnl_calc Provenance-FIFO Realized P&L — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `pnl_calc`'s day realized-P&L source (a sum of per-trade `pnl_pts_real` that silently drops `None`-fill trades) with an order-provenance FIFO over `orders.jsonl` that isolates bot fills via `sent→reply(orderno)→match` linkage, FIFOs per exact contract within the 08:45 trading-day window, and keeps the per-trade sum as a divergence cross-check.

**Architecture:** New pure functions in `pnl_calc.py` operate on parsed `orders.jsonl` rows: `bot_ordernos()` reconstructs which ordernos the bot actually sent; `realized_day_pts()` per-product-FIFOs the sent-backed match fills inside the trading-day window. `_compute()` makes the FIFO value the authoritative `real_trading_day_pnl_pts` (the circuit-breaker input), computes the old `pnl_pts_real` sum in parallel, and logs a warning when they diverge with zero missing fills. Fail-open: a missing/unreadable `orders.jsonl` yields `None` (breaker treats as no-data, does not lock).

**Tech Stack:** Python 3 stdlib only (`json`, `datetime`, `zoneinfo`, `logging`, `unittest`). No third-party deps. Run tests: `python3 -m unittest test_pnl_calc -v` from the repo root.

---

## File Structure

- **Modify** `pnl_calc.py` — add `_parse_iso`, `_trading_day_window`, `bot_ordernos`, `realized_day_pts`, `divergence_warn`, `_read_orders_raw`, `_log_divergence`; rewire `_compute`. `summarize_real_pnl`, `_fifo`, `_read_trades`, `_real_open`, `heartbeat_fields`, the 30 s cache stay as-is.
- **Modify** `test_pnl_calc.py` — add test classes for the new pure functions; keep the existing `SummarizeRealPnlTest` (still valid — the cross-check path).

All new P&L math lives in pure functions taking parsed rows + datetime bounds, matching the existing `summarize_real_pnl` style (I/O-free, unit-tested).

---

### Task 1: Time helpers — `_parse_iso` + `_trading_day_window`

**Files:**
- Modify: `pnl_calc.py`
- Test: `test_pnl_calc.py`

- [ ] **Step 1: Write the failing tests**

Add to `test_pnl_calc.py`:

```python
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pnl_calc import _parse_iso, _trading_day_window

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_pnl_calc.TimeHelpersTest -v`
Expected: FAIL with `ImportError: cannot import name '_parse_iso'`

- [ ] **Step 3: Implement the helpers**

Add to `pnl_calc.py` (after the existing imports / `_TZ` definition):

```python
def _parse_iso(ts):
    """Parse an ISO-8601 string (e.g. '2026-06-19T02:52:40+08:00') to an aware
    datetime. Returns None on any malformed / non-string input."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _trading_day_window(td):
    """[start, end) aware-datetime bounds for a trading day's 08:45 TW window.
    A day labelled 2026-06-18 spans 2026-06-18 08:45 → 2026-06-19 08:45, so a
    night-session round-trip closing after midnight still falls inside."""
    d = datetime.strptime(td, "%Y-%m-%d").date()
    start = datetime.combine(d, dtime(8, 45), _TZ)
    return start, start + timedelta(days=1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_pnl_calc.TimeHelpersTest -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add pnl_calc.py test_pnl_calc.py
git commit -m "feat(pnl): _parse_iso + _trading_day_window time helpers"
```

---

### Task 2: `bot_ordernos` — provenance linkage

**Files:**
- Modify: `pnl_calc.py`
- Test: `test_pnl_calc.py`

**Interface:** `bot_ordernos(rows: list[dict]) -> set[str]`. Reconstructs which ordernos the bot sent: each `sent` event (no orderno) is paired to the first unclaimed `reply` carrying an `orderno` within 3 s that shares `productid` + `bs`. Manual fills have no `sent`, so their ordernos never enter the set.

- [ ] **Step 1: Write the failing tests**

Add to `test_pnl_calc.py`:

```python
from pnl_calc import bot_ordernos

def _sent(ts, pid="MXFG6", bs="B"):
    return {"ts": ts, "event": "sent", "productid": pid, "bs": bs}

def _reply(ts, ono, pid="MXFG6", bs="B"):
    return {"ts": ts, "event": "reply", "productid": pid, "bs": bs, "orderno": ono}

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_pnl_calc.BotOrdernosTest -v`
Expected: FAIL with `ImportError: cannot import name 'bot_ordernos'`

- [ ] **Step 3: Implement `bot_ordernos`**

Add to `pnl_calc.py`:

```python
_LINK_WINDOW_SEC = 3  # max gap from a bot 'sent' to its 'reply' (real data: same second)


def bot_ordernos(rows):
    """Set of ordernos the bot actually sent. A 'sent' event carries no orderno;
    it is paired to the first unclaimed 'reply' (which has the orderno) within
    _LINK_WINDOW_SEC seconds sharing productid + bs. Manual/non-bot fills have no
    'sent' and are therefore excluded. See provenance design 2026-06-19."""
    sents, replies = [], []
    for r in rows:
        ev = r.get("event")
        ts = _parse_iso(r.get("ts"))
        if ts is None:
            continue
        if ev == "sent":
            sents.append((ts, r.get("productid"), r.get("bs")))
        elif ev == "reply" and r.get("orderno"):
            replies.append((ts, r.get("productid"), r.get("bs"), r.get("orderno")))
    sents.sort(key=lambda x: x[0])
    replies.sort(key=lambda x: x[0])
    claimed = set()
    bot = set()
    for sts, spid, sbs in sents:
        for i, (rts, rpid, rbs, ono) in enumerate(replies):
            if i in claimed:
                continue
            if rpid == spid and rbs == sbs and 0 <= (rts - sts).total_seconds() <= _LINK_WINDOW_SEC:
                bot.add(ono)
                claimed.add(i)
                break
    return bot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_pnl_calc.BotOrdernosTest -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add pnl_calc.py test_pnl_calc.py
git commit -m "feat(pnl): bot_ordernos provenance linkage (sent->reply->orderno)"
```

---

### Task 3: `realized_day_pts` — per-product FIFO over bot fills in window

**Files:**
- Modify: `pnl_calc.py`
- Test: `test_pnl_calc.py`

**Interface:** `realized_day_pts(rows: list[dict], start: datetime, end: datetime) -> tuple[float, int]`. Returns `(realized_points, round_trips)`. Keeps only `match` events whose orderno is in `bot_ordernos(rows)` and whose ts is within `[start, end)`, groups by exact `productid`, FIFOs each independently, sums realized points. Empty → `(0.0, 0)`.

- [ ] **Step 1: Write the failing tests**

Add to `test_pnl_calc.py`:

```python
from pnl_calc import realized_day_pts, _trading_day_window

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_pnl_calc.RealizedDayPtsTest -v`
Expected: FAIL with `ImportError: cannot import name 'realized_day_pts'`

- [ ] **Step 3: Implement `realized_day_pts`**

Add to `pnl_calc.py` (uses the existing `_fifo`):

```python
def realized_day_pts(rows, start, end):
    """(realized_points, round_trips) from bot match fills inside [start, end),
    FIFO'd per EXACT contract. Provenance (bot_ordernos) drops manual/non-bot
    fills; the window drops stale prior-day legs; per-product grouping prevents
    cross-contract pairing on a settlement-day rollover. Open legs stay open
    (unrealized, not counted)."""
    bot = bot_ordernos(rows)
    by_pid = {}
    for r in rows:
        if r.get("event") != "match" or r.get("orderno") not in bot:
            continue
        ts = _parse_iso(r.get("ts"))
        if ts is None or not (start <= ts < end):
            continue
        bs = r.get("bs")
        price = r.get("matchprice")
        if bs not in ("B", "S") or price is None:
            continue
        qty = int(r.get("matchqty") or 1)
        for _ in range(max(1, qty)):
            by_pid.setdefault(r.get("productid"), []).append((ts, bs, float(price)))
    total = 0.0
    trips = 0
    for fills in by_pid.values():
        fills.sort(key=lambda x: x[0])
        closed, _open = _fifo(fills)
        total += sum(pnl for _ts, pnl in closed)
        trips += len(closed)
    return round(total, 1), trips
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_pnl_calc.RealizedDayPtsTest -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add pnl_calc.py test_pnl_calc.py
git commit -m "feat(pnl): realized_day_pts per-product provenance FIFO"
```

---

### Task 4: `divergence_warn` — cross-check decision

**Files:**
- Modify: `pnl_calc.py`
- Test: `test_pnl_calc.py`

**Interface:** `divergence_warn(fifo_pts, pertrade_pts, missing_fill) -> bool`. True when the FIFO and per-trade numbers disagree by more than 1.0 pt **and** there are zero missing fills (i.e. a disagreement not explained by unstamped exits — a provenance/data smell). Returns False if `fifo_pts is None`.

- [ ] **Step 1: Write the failing tests**

Add to `test_pnl_calc.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_pnl_calc.DivergenceWarnTest -v`
Expected: FAIL with `ImportError: cannot import name 'divergence_warn'`

- [ ] **Step 3: Implement `divergence_warn`**

Add to `pnl_calc.py`:

```python
_DIVERGENCE_TOL_PTS = 1.0


def divergence_warn(fifo_pts, pertrade_pts, missing_fill):
    """True when the authoritative orders-FIFO value and the per-trade pnl_pts_real
    sum disagree by more than _DIVERGENCE_TOL_PTS with ZERO missing fills — a
    disagreement not explained by unstamped exits, so a provenance/data smell worth
    a log line. Never blocks the breaker."""
    if fifo_pts is None or pertrade_pts is None:
        return False
    if missing_fill != 0:
        return False
    return abs(fifo_pts - pertrade_pts) > _DIVERGENCE_TOL_PTS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_pnl_calc.DivergenceWarnTest -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add pnl_calc.py test_pnl_calc.py
git commit -m "feat(pnl): divergence_warn cross-check decision"
```

---

### Task 5: Wire `_compute` — FIFO authoritative, fail-open, divergence log

**Files:**
- Modify: `pnl_calc.py`
- Test: `test_pnl_calc.py`

**Behaviour:** `_compute` reads `orders.jsonl` (missing/unreadable → day P&L `None`, fail-open), makes `realized_day_pts` the authoritative `real_trading_day_pnl_pts` / `real_trading_day_trades`, keeps `summarize_real_pnl` for `real_month_pnl_pts` + `real_day_missing_fill` + a `real_day_pnl_pertrade` visibility field, and logs a warning via `divergence_warn`. `heartbeat_fields` cache wrapper is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `test_pnl_calc.py` (these drive an `orders.jsonl` reader + the wiring through a tmp dir + monkeypatched paths):

```python
import os, tempfile, importlib
import pnl_calc as pc

class ComputeWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_orders = pc.__dict__.get("_ORDERS_PATH_OVERRIDE")

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test_pnl_calc.ComputeWiringTest -v`
Expected: FAIL with `AttributeError: module 'pnl_calc' has no attribute '_read_orders_raw'`

- [ ] **Step 3: Implement the reader, logger, and rewire `_compute`**

Add near the top of `pnl_calc.py` (after imports):

```python
import logging
_log = logging.getLogger("pnl_calc")
```

Add the reader (parameterised path so it is unit-testable; default = the live file):

```python
def _read_orders_raw(path=None):
    """Parsed orders.jsonl rows (bad JSON lines skipped). Raises if the file is
    absent/unreadable so _compute can fail-open to None. An empty file -> []."""
    p = path or os.path.join(os.path.dirname(__file__), "orders.jsonl")
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out
```

Replace the existing `_compute`:

```python
def _compute(base=None):
    td = _today_trading_day()
    xcheck = summarize_real_pnl(_read_trades(), td)   # per-trade sum + month + missing
    start, end = _trading_day_window(td)
    try:
        day_pnl, day_trades = realized_day_pts(_read_orders_raw(), start, end)
    except Exception:
        day_pnl, day_trades = None, None              # fail-open: breaker sees no-data
    if divergence_warn(day_pnl, xcheck["real_trading_day_pnl_pts"],
                       xcheck["real_day_missing_fill"]):
        _log.warning("PNL_DIVERGENCE: orders-FIFO=%s vs per-trade=%s (missing_fill=0) "
                     "— check provenance/data", day_pnl, xcheck["real_trading_day_pnl_pts"])
    return {
        "real_trading_day_pnl_pts": day_pnl,                              # FIFO -> breaker
        "real_trading_day_trades":  day_trades,
        "real_month_pnl_pts":       xcheck["real_month_pnl_pts"],         # display (per-trade)
        "real_day_missing_fill":    xcheck["real_day_missing_fill"],
        "real_day_pnl_pertrade":    xcheck["real_trading_day_pnl_pts"],   # visibility
        "real_open":                _real_open(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_pnl_calc.ComputeWiringTest -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add pnl_calc.py test_pnl_calc.py
git commit -m "feat(pnl): _compute uses provenance FIFO (authoritative) + fail-open + divergence log"
```

---

### Task 6: Update module docstring + full-suite green + real-data sanity

**Files:**
- Modify: `pnl_calc.py` (module docstring only)
- Test: `test_pnl_calc.py` (full run)

- [ ] **Step 1: Update the module docstring**

Replace the top docstring of `pnl_calc.py` to describe the provenance-FIFO source (keep it factual; reference the design doc date 2026-06-19). The docstring must state: day P&L = per-product provenance FIFO over orders.jsonl (authoritative, feeds breaker); per-trade `pnl_pts_real` kept as cross-check + month/display; fail-open None on unreadable orders.jsonl; manual fills excluded by absence of a `sent` event; 30 s cache on the 08:45 window unchanged.

- [ ] **Step 2: Run the FULL test suite**

Run: `python3 -m unittest test_pnl_calc -v`
Expected: PASS — all classes green (`SummarizeRealPnlTest`, `TimeHelpersTest`, `BotOrdernosTest`, `RealizedDayPtsTest`, `DivergenceWarnTest`, `ComputeWiringTest`).

- [ ] **Step 3: Real-data sanity check (read-only, off-VPS copy)**

Run against the captured 2026-06-18 data (already in `/tmp/pnl_recon_0618/`, or re-pull fresh) — confirm the provenance FIFO reproduces the validated number:

```bash
cd /tmp/pnl_recon_0618 && python3 -c "
import json, pnl_calc as pc
rows=[json.loads(l) for l in open('orders.jsonl') if l.strip()]
s,e=pc._trading_day_window('2026-06-18')
print(pc.realized_day_pts(rows,s,e))   # expect (170.0, 19)
"
```
Expected: `(170.0, 19)`

- [ ] **Step 4: Commit**

```bash
git add pnl_calc.py
git commit -m "docs(pnl): module docstring for provenance-FIFO realized P&L"
```

---

## Deployment (out of plan scope — ask-first)

Do NOT deploy as part of this plan. `pnl_calc.py` feeds the live `DAILY_MAX_LOSS` circuit breaker and there is no paper env. After the branch is green and code-reviewed, deployment follows the trader SOP separately: drift guard → scp → sha verify → `trader-precheck.sh && restart` → boot verify → Telegram/Discord report — in a break/flat window, with Sean's explicit GO. Observe-first: compare heartbeat `real_trading_day_pnl_pts` vs `real_day_pnl_pertrade` for a session before trusting it blindly.

---

## Self-Review

- **Spec coverage:** provenance filter → Task 2; window floor → Tasks 1 & 3 (`test_stale_leg_before_window_excluded`); per-product FIFO → Task 3 (`test_two_products_...`); null-fill captured → Task 3 (`test_null_pnl_pts_real_...`); authoritative + cross-check + fail-open → Tasks 4 & 5; caching unchanged → noted (no task touches `heartbeat_fields`). All spec testing bullets map to a test. ✅
- **Placeholders:** none — every code step has complete code; Task 6 Step 1 describes the docstring content explicitly rather than pasting prose. ✅
- **Type consistency:** `bot_ordernos -> set`, `realized_day_pts -> (float, int)`, `divergence_warn -> bool`, `_read_orders_raw -> list[dict]`, `_trading_day_window -> (datetime, datetime)`, `_parse_iso -> datetime|None` — used consistently across Tasks 2–5. ✅
