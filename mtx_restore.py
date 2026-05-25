"""Pure restore-reconciliation + local-state I/O for MTX units.

No SDK / network / strategy imports, so it is unit-testable on system python3
(python3 -m unittest test_mtx_restore).

The bot's AUTHORITATIVE record of which MTX units it actually opened is the local
mtx_state.json (written on real open/close). The Worker history is consulted only to
(a) refresh current exit levels and (b) detect exits that happened while the bot was down.
Worker-open signals the bot never filled (lock-refused / HALF_SIZE-skipped) are NOT in the
local file and must NOT be restored — that was the phantom-unit bug (2026-05-25).
"""
import json
import os

TERMINAL_STATUSES = ("profit", "loss", "trail", "reversed", "session_end")


def load_mtx_state(path):
    """Return the list of persisted MTX units, or [] if missing/corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    units = data.get("mtx_units")
    return units if isinstance(units, list) else []


def save_mtx_state(path, units):
    """Atomic write of the MTX units list (tmp + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mtx_units": list(units)}, f, ensure_ascii=False)
    os.replace(tmp, path)
