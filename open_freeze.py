"""Session-open trading freeze (pure predicate).

Sean 2026-05-30: no trading — neither new entries NOR exits of carried positions
— may fire in the first `freeze_secs` seconds after a session open, to sit out the
opening spike ("sudden big up/down"). This module is the pure time-of-day predicate;
the order-path gating (entry loop, on_tick, _sync_worker_state) lives in strategy.py.

Full-freeze trade-off (accepted by Sean): freezing exits means software stop-loss is
OFF during the window. It dodges opening fakeout/stop-hunt wicks, but on a genuine
gap-and-go it holds a loser an extra <=freeze_secs and exits worse than the stop.

Session opens (TW): day 08:45:00, night 15:00:00. freeze_secs<=0 (or invalid type)
disables — always returns False — so the feature is a no-op without an explicit,
positive OPEN_FREEZE_SECS.
"""

_DAY_OPEN_SECS = 8 * 3600 + 45 * 60   # 08:45:00 → 31500
_NIGHT_OPEN_SECS = 15 * 3600          # 15:00:00 → 54000


def in_open_freeze_window(dt, freeze_secs) -> bool:
    """Return True iff `dt` (a TW-local datetime) falls within the first
    `freeze_secs` seconds of the day (08:45) or night (15:00) session open.

    Fail-open: any non-positive or non-int `freeze_secs` (incl. bool/None/str)
    disables the freeze. Comparison is half-open [open, open+freeze_secs): the
    second at open+freeze_secs is the first tradable second again.
    """
    # bool is an int subclass — exclude so True/False can't act as a 1s/0s window.
    if isinstance(freeze_secs, bool) or not isinstance(freeze_secs, int) or freeze_secs <= 0:
        return False

    t = dt.time()
    secs = t.hour * 3600 + t.minute * 60 + t.second
    if _DAY_OPEN_SECS <= secs < _DAY_OPEN_SECS + freeze_secs:
        return True
    if _NIGHT_OPEN_SECS <= secs < _NIGHT_OPEN_SECS + freeze_secs:
        return True
    return False
