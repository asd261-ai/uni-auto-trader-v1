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
    if not isinstance(data, dict):
        return []
    units = data.get("mtx_units")
    return units if isinstance(units, list) else []


def load_mtx_product(path):
    """Return the persisted active product code, or None if missing/corrupt/absent."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    prod = data.get("product")
    return prod if isinstance(prod, str) and prod else None


def save_mtx_state(path, units, product=None):
    """Atomic write of the MTX units list + active product (tmp + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"product": product, "mtx_units": list(units)}, f, ensure_ascii=False)
    os.replace(tmp, path)


def rolled_over(stored_product, current_product):
    """True iff the contract rolled since the last save (= settlement rollover).

    Only fires when BOTH are known and differ — a missing stored product (legacy file
    or first boot) is conservative and never triggers a drop. Whitespace is stripped so a
    stray space in the env-sourced product never causes a false drop of a live position.
    """
    s = stored_product.strip() if isinstance(stored_product, str) else stored_product
    c = current_product.strip() if isinstance(current_product, str) else current_product
    return bool(s) and bool(c) and s != c


def _worker_by_id(worker_history):
    out = {}
    for t in worker_history or []:
        if isinstance(t, dict) and "id" in t:
            out[t["id"]] = t
    return out


def reconcile_restore(local_units, worker_history, cutoff_ms):
    """Decide what to do with each locally-persisted MTX unit at startup.

    Returns dict:
      to_restore       : [unit]            restore as open; stop/target refreshed from Worker
      to_record_exit   : [(unit, worker)]  exited while bot was down; record once, do NOT restore
      dropped_stale    : [id]              local unit at/below the session boot floor; drop
      skipped_phantoms : [id]              Worker-open ids NOT in local (the phantom class); not restored
    """
    by_id = _worker_by_id(worker_history)
    local_ids = set()
    to_restore, to_record_exit, dropped_stale = [], [], []
    for u in local_units or []:
        if not isinstance(u, dict) or "id" not in u:
            continue
        uid = u["id"]
        local_ids.add(uid)
        w = by_id.get(uid)
        # Worker status outranks the cutoff (2026-07-19 audit): a Worker-confirmed
        # "open" unit is a live broker position even if it was opened before the
        # current session (carried across the boundary) — dropping it would leave
        # a real position untracked. The cutoff only drops units the Worker has
        # NO record of (true ghosts).
        if w is None:
            if uid <= cutoff_ms:
                dropped_stale.append(uid)
            else:
                to_restore.append(dict(u))                   # conservative: keep local as-is
        elif w.get("status") == "open":
            merged = dict(u)
            for k in ("stop", "target"):                     # refresh current levels from Worker
                if w.get(k) is not None:
                    merged[k] = w[k]
            to_restore.append(merged)
        elif w.get("status") in TERMINAL_STATUSES:
            to_record_exit.append((dict(u), dict(w)))
        else:
            to_restore.append(dict(u))                       # unknown status: conservative
    skipped_phantoms = [
        t["id"] for t in (worker_history or [])
        if isinstance(t, dict) and t.get("status") == "open"
        and t.get("id") not in local_ids and t.get("id", 0) > cutoff_ms
    ]
    return {"to_restore": to_restore, "to_record_exit": to_record_exit,
            "dropped_stale": dropped_stale, "skipped_phantoms": skipped_phantoms}
