"""READ-ONLY reconciliation: trades.jsonl `pnl_pts_real` vs orders.jsonl FIFO ground truth.

Monday observe-first tool for task B (trades.jsonl real-fill P&L, DEPLOYED 2026-06-06).
Verifies the new per-trade real-fill field agrees with the AUTHORITATIVE orders.jsonl FIFO.

Rule [[feedback-real-pnl-orders-not-trades-jsonl]]: orders.jsonl真實成交 FIFO is canonical;
trades.jsonl `pnl_pts` is signal-based and systematically UNDER-reports (6/5: signal −91 vs
real −325). The new `pnl_pts_real` column is supposed to close that gap by stamping broker
Match prices onto each row — this tool checks it actually does, the first real trading day.

Pure math (reconcile / day_window) is I/O-free and unit-tested. The CLI does the file reads,
reusing pnl_calc's FIFO so we never fork a second, divergent FIFO implementation.

Usage:
    python3 reconcile_real_fill.py                 # today's trading day
    python3 reconcile_real_fill.py 2026-06-08      # a specific trading day (08:45 TW boundary)
    python3 reconcile_real_fill.py 2026-06-08 MXF  # override contract base (default MXF)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import pnl_calc  # reuse the canonical orders.jsonl FIFO (_read_matches + _fifo)

_TZ = ZoneInfo("Asia/Taipei")
_TRADES_PATH = os.path.join(os.path.dirname(__file__), "trades.jsonl")


# ── pure helpers (unit-tested) ──────────────────────────────────────────────

def day_window(trading_day: str) -> Tuple[str, str]:
    """[start, end) ISO bounds for a trading day's 08:45 TW window.

    A trading day labelled 2026-06-08 spans 2026-06-08 08:45 → 2026-06-09 08:45,
    so a night-session round-trip that closes after midnight (e.g. 2026-06-09 01:00)
    still falls inside the window — matching how strategy._compute_trading_day labels it.
    """
    d = date.fromisoformat(trading_day)
    start = datetime.combine(d, dtime(8, 45), _TZ)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def reconcile(trades: List[Dict[str, Any]], fifo_realized_pts: float,
              fifo_roundtrips: int, fifo_source: str = "mtx") -> Dict[str, Any]:
    """Pure reconciliation math. Compares the rows whose broker fills land in orders.jsonl
    against the orders.jsonl FIFO ground truth (realized-points sum + round-trip count).

    ONLY `fifo_source` rows (default "mtx", the real-money signals on the MXF contract) are
    reconciled against the FIFO. FVG runs paper and NEVER hits orders.jsonl (strategy.py:1189),
    so FVG rows are surfaced separately (other_n / other_null) and never flip the verdict — a
    paper row legitimately has no broker fill, so its null pnl_pts_real is expected, not a bug.

    Returns a report dict — never raises on empty input.
      n_trades       all rows in trades.jsonl for the day (every source)
      n_real         fifo_source rows with a non-null pnl_pts_real (real exit_fill came back)
      n_null         fifo_source rows with pnl_pts_real == None (fill missing → RED flag)
      sum_signal     Σ pnl_pts over fifo_source rows  (signal-based, the under-reporting number)
      sum_real       Σ pnl_pts_real over fifo_source rows  (real-fill, non-null only)
      fifo_realized  orders.jsonl FIFO realized P&L (ground truth)
      real_vs_fifo   sum_real − fifo_realized   (≈0 ⇒ field is trustworthy)
      signal_vs_real sum_signal − sum_real      (quantifies the systematic under-report)
      count_mismatch n_real != fifo_roundtrips  (manual same-contract trades or missed fills)
      other_n        rows from non-fifo_source (e.g. FVG paper) — informational only
      other_null     of those, how many have null pnl_pts_real (expected for paper)
      by_source      per-source {n, sum_signal, sum_real, n_null}  (all sources)
      verdict        "OK" | "WARN" | "RED"  (RED only on a fifo_source null fill)
    """
    n_trades = len(trades)
    compared = [t for t in trades if t.get("source") == fifo_source]
    other = [t for t in trades if t.get("source") != fifo_source]
    real_rows = [t for t in compared if t.get("pnl_pts_real") is not None]
    null_rows = [t for t in compared if t.get("pnl_pts_real") is None]
    sum_signal = round(sum(_num(t.get("pnl_pts")) for t in compared), 1)
    sum_real = round(sum(_num(t.get("pnl_pts_real")) for t in real_rows), 1)
    fifo_realized = round(fifo_realized_pts, 1)

    by_source: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        s = t.get("source", "?")
        b = by_source.setdefault(s, {"n": 0, "sum_signal": 0.0, "sum_real": 0.0, "n_null": 0})
        b["n"] += 1
        b["sum_signal"] += _num(t.get("pnl_pts"))
        if t.get("pnl_pts_real") is None:
            b["n_null"] += 1
        else:
            b["sum_real"] += _num(t.get("pnl_pts_real"))
    for b in by_source.values():
        b["sum_signal"] = round(b["sum_signal"], 1)
        b["sum_real"] = round(b["sum_real"], 1)

    real_vs_fifo = round(sum_real - fifo_realized, 1)
    # tolerance: a couple of points absorbs integer rounding of pnl_pts_real per round() in
    # real_fill_pnl.compute_pnl_pts_real. A wider gap means real divergence, not rounding.
    tol = 2.0
    count_mismatch = (len(real_rows) != fifo_roundtrips)
    if null_rows:
        verdict = "RED"          # a missing real fill is the exact failure mode to catch
    elif abs(real_vs_fifo) > tol or count_mismatch:
        verdict = "WARN"
    else:
        verdict = "OK"

    return {
        "n_trades": n_trades,
        "n_real": len(real_rows),
        "n_null": len(null_rows),
        "sum_signal": sum_signal,
        "sum_real": sum_real,
        "fifo_realized": fifo_realized,
        "fifo_roundtrips": fifo_roundtrips,
        "real_vs_fifo": real_vs_fifo,
        "signal_vs_real": round(sum_signal - sum_real, 1),
        "count_mismatch": count_mismatch,
        "fifo_source": fifo_source,
        "other_n": len(other),
        "other_null": sum(1 for t in other if t.get("pnl_pts_real") is None),
        "by_source": by_source,
        "verdict": verdict,
        "null_ids": [(t.get("source"), t.get("id"), t.get("reason")) for t in null_rows],
    }


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


# ── file readers (CLI side) ─────────────────────────────────────────────────

def load_trades_for_day(path: str, trading_day: str) -> List[Dict[str, Any]]:
    """trades.jsonl rows whose `trading_day` == the given ISO date."""
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("trading_day") == trading_day:
                out.append(r)
    return out


def fifo_for_day(base: str, start_iso: str, end_iso: str) -> Tuple[float, int]:
    """orders.jsonl FIFO realized P&L + round-trip count for [start, end), per contract.
    Reuses pnl_calc so the FIFO logic is the single source of truth (per-contract bucketing,
    single-leg MXF only — see [[project-pnl-calc-contract-mixing]])."""
    by_pid = pnl_calc._read_matches(base)
    total = 0.0
    n = 0
    for pid in sorted(by_pid):
        closed, _open = pnl_calc._fifo(by_pid[pid])
        for ts, pnl in closed:
            if start_iso <= ts < end_iso:
                total += pnl
                n += 1
    return total, n


def format_report(trading_day: str, base: str, rep: Dict[str, Any]) -> str:
    icon = {"OK": "✅", "WARN": "⚠️", "RED": "🔴"}[rep["verdict"]]
    lines = [
        f"# Real-fill reconciliation — {trading_day} (base {base})",
        "",
        f"**Verdict: {icon} {rep['verdict']}**",
        "",
        f"_FIFO-reconciled source: **{rep['fifo_source']}** (real-money on MXF). "
        f"Other sources (FVG paper, not in orders.jsonl): {rep['other_n']} rows "
        f"({rep['other_null']} null — expected for paper)._",
        "",
        "| metric | value |",
        "|---|---|",
        f"| trades.jsonl rows (all sources) | {rep['n_trades']} |",
        f"| {rep['fifo_source']} rows with pnl_pts_real | {rep['n_real']} |",
        f"| **{rep['fifo_source']} null pnl_pts_real (missing fill)** | **{rep['n_null']}** |",
        f"| Σ pnl_pts ({rep['fifo_source']} signal) | {rep['sum_signal']} |",
        f"| Σ pnl_pts_real ({rep['fifo_source']}) | {rep['sum_real']} |",
        f"| orders.jsonl FIFO realized | {rep['fifo_realized']} ({rep['fifo_roundtrips']} round-trips) |",
        f"| **real − FIFO** (≈0 ⇒ trustworthy) | **{rep['real_vs_fifo']}** |",
        f"| signal − real (under-report) | {rep['signal_vs_real']} |",
        f"| count mismatch (real vs FIFO trips) | {rep['count_mismatch']} |",
        "",
        "## By source",
        "| source | n | Σ signal | Σ real | null |",
        "|---|---|---|---|---|",
    ]
    for s, b in sorted(rep["by_source"].items()):
        lines.append(f"| {s} | {b['n']} | {b['sum_signal']} | {b['sum_real']} | {b['n_null']} |")
    if rep["null_ids"]:
        lines += ["", "## 🔴 Rows missing a real fill (investigate vs orders.jsonl)",
                  "| source | id | reason |", "|---|---|---|"]
        for src, sid, reason in rep["null_ids"]:
            lines.append(f"| {src} | {sid} | {reason} |")
    lines += [
        "",
        "## Read",
        "- **OK** ⇒ mtx pnl_pts_real matches orders.jsonl FIFO; the field is trustworthy → safe to merge feat→main.",
        "- **null fills** (mtx) ⇒ on_fill didn't stamp exit_fill within 60s timeout; check the deferred-write path.",
        "- **count mismatch** with 0 nulls usually ⇒ a manual same-contract trade hit orders.jsonl "
        "(shared account 0239174) — FIFO includes it but trades.jsonl doesn't. Expected if Sean hand-traded.",
        "- FVG rows are paper (never in orders.jsonl) so they're excluded from the FIFO check; their "
        "null pnl_pts_real is expected, not a failure.",
        "- Authoritative number is always orders.jsonl FIFO, never signal pnl_pts.",
    ]
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    trading_day = argv[1] if len(argv) > 1 else _today_trading_day()
    base = argv[2] if len(argv) > 2 else "MXF"
    start_iso, end_iso = day_window(trading_day)
    trades = load_trades_for_day(_TRADES_PATH, trading_day)
    fifo_pts, fifo_n = fifo_for_day(base, start_iso, end_iso)
    rep = reconcile(trades, fifo_pts, fifo_n)
    print(format_report(trading_day, base, rep))
    return 0 if rep["verdict"] == "OK" else 1


def _today_trading_day() -> str:
    now = datetime.now(_TZ)
    d = now.date()
    if now.time() < dtime(8, 45):
        d = d - timedelta(days=1)
    return d.isoformat()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
