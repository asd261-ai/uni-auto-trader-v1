"""Pure process-level guard for the live order path (BOARD T010).

No SDK/strategy imports -> unit-testable on system python3:
    python3 -m unittest test_order_guard

Why this exists: 2026-07-10 near-miss — `unittest discover` on the VPS imported
a legacy smoke script whose module-level code logged in and sent a REAL 1-lot
market order on the live account; only the broker's night-session order-type
rejection (HHO0038) prevented a fill. Renaming that file removed one mine; this
guard closes the class: **no process may reach the broker order call unless it
positively identifies itself**, so future scripts/tools/discovery accidents
fail closed instead of trading.

Contract (exact string "1", no truthiness):
  - TRADER_SERVICE=1            -> the systemd service. Set ONLY in the unit file
                                   (Environment=TRADER_SERVICE=1). ⚠️ NEVER put it
                                   in .env — every script load_dotenv()s that file,
                                   which would silently re-open the class of hole
                                   this guard exists to close.
  - TRADER_MANUAL_ORDER_ACK=1   -> an explicitly-acked human action (flat.py sets
                                   it AFTER its interactive Confirm; one-off manual
                                   ops export it deliberately).
Neither -> OrderGuardError. Callers in the service catch it and return
GuardRejectedResp so the poll loop degrades to the existing rejection path
(loud CRITICAL log) rather than crash-looping.
"""


class OrderGuardError(RuntimeError):
    pass


class GuardRejectedResp:
    """Shape-compatible stand-in for the SDK order response on guard rejection:
    callers only read .issend / .seq / .errormsg."""
    issend = False
    seq = None

    def __init__(self, reason: str):
        self.errormsg = f"order_guard blocked: {reason}"


def assert_order_allowed(env=None) -> str:
    """Return the caller's identity ('service' | 'manual') or raise OrderGuardError.
    `env` is injectable for tests; defaults to os.environ."""
    if env is None:
        import os
        env = os.environ
    if env.get("TRADER_SERVICE") == "1":
        return "service"
    if env.get("TRADER_MANUAL_ORDER_ACK") == "1":
        return "manual"
    raise OrderGuardError(
        "live order path reached by an unidentified process. "
        "Service: set TRADER_SERVICE=1 in the systemd unit (NOT .env). "
        "Manual action: export TRADER_MANUAL_ORDER_ACK=1 explicitly. "
        "(2026-07-10 near-miss class guard, BOARD T010)"
    )
