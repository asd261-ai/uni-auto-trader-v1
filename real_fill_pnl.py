# real_fill_pnl.py
"""Pure helpers for trades.jsonl real-fill P&L (task B). No I/O, no broker SDK.
Caller passes now_ms so timeout logic stays deterministic and unit-testable."""
from typing import Optional, List, Dict, Any


def compute_pnl_pts_real(dir_: str, entry_fill, exit_fill) -> Optional[int]:
    """Real-fill P&L in points. Returns None if EITHER fill is missing —
    never substitute a signal value (real-money attribution must not lie)."""
    if entry_fill is None or exit_fill is None:
        return None
    diff = (exit_fill - entry_fill) if dir_ == "long" else (entry_fill - exit_fill)
    return round(diff)


def finalize_exit(record: Dict[str, Any], exit_fill) -> Dict[str, Any]:
    """Stamp exit_fill + pnl_pts_real onto a deferred trade record, in place.
    exit_fill=None is the timeout-flush case → pnl_pts_real stays None."""
    record["exit_fill"] = exit_fill
    record["pnl_pts_real"] = compute_pnl_pts_real(
        record.get("dir"), record.get("entry_fill"), exit_fill)
    return record
