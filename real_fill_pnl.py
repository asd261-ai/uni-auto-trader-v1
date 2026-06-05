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


def due_records(pending: List[Dict[str, Any]], now_ms: int) -> List[Dict[str, Any]]:
    """Pending-exit entries whose flush deadline has passed (timeout candidates).
    A missing deadline_ms is treated as due (0) so malformed entries never linger."""
    return [p for p in pending if p.get("deadline_ms", 0) <= now_ms]


def serialize_pending(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Plain JSON-able snapshot of pending-exit records for the state file."""
    return [{"record": p["record"], "deadline_ms": p.get("deadline_ms", 0)}
            for p in pending if p.get("record") is not None]


def deserialize_pending(blob) -> List[Dict[str, Any]]:
    """Rebuild pending-exit list from state-file blob (None → empty).
    Entries without a record are dropped (corruption-safe)."""
    if not blob:
        return []
    return [{"record": e["record"], "deadline_ms": e.get("deadline_ms", 0)}
            for e in blob if isinstance(e, dict) and e.get("record") is not None]
