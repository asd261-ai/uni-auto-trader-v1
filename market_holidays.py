"""Pure TAIFEX market-holiday calendar.

No SDK/strategy imports -> unit-testable on system python3:
    python3 -m unittest test_market_holidays

Source: TAIFEX 2026 期貨集中交易市場行事曆 (2026Calendar.pdf), the pink 非交易日 cells.

Why this exists: _get_session()'s weekday-aware gate treats a weekday public
holiday (e.g. Fri 端午 6/19) as a normal trading day, so the dead holiday feed
during those "active" windows would trip the armed tick / disconnect-storm
watchdogs into a restart-storm. is_trading_day() folds the holiday calendar into
the same check that already excludes weekends, so _get_session returns "break" on
holidays and the watchdogs stay dormant (breakers remain armed year-round; no
disarm/arm dance needed).

Night-session nuance: TAIFEX runs the eve-of-holiday night session to the
holiday's 05:00 (per the 2026 calendar note: 6/19 端午 — 6月契約交易至當日凌晨5點).
So holiday gating blocks the holiday's DAY + NIGHT-START legs, but the holiday's
DAWN night-tail (00:00-05:00) belongs to the eve's session and is correctly kept
active by checking is_trading_day(prev_day) in _get_session.

Weekend-falling holidays are intentionally omitted (the weekday check already
excludes them): 2026-04-04 兒童節(Sat), 2026-04-05 清明(Sun),
2026-10-10 國慶(Sat), 2026-10-25 光復(Sun).
"""

# TAIFEX non-trading days that fall on a weekday (ISO 'YYYY-MM-DD').
TW_MARKET_HOLIDAYS = frozenset({
    "2026-01-01",  # 元旦 (Thu)
    "2026-02-12",  # 春節封關次1 (Thu)
    "2026-02-13",  # 春節封關次2 (Fri)
    "2026-02-16",  # 除夕 (Mon)
    "2026-02-17",  # 春節初一 (Tue)
    "2026-02-18",  # 春節初二 (Wed)
    "2026-02-19",  # 春節初三 (Thu)
    "2026-02-20",  # 春節初四 (Fri)
    "2026-02-27",  # 和平紀念日 228 補假 (Fri; 2/28 Sat)
    "2026-04-03",  # 清明/兒童節連假 (Fri)
    "2026-04-06",  # 清明連假補假 (Mon)
    "2026-05-01",  # 勞動節 (Fri)
    "2026-06-19",  # 端午節 (Fri)
    "2026-07-10",  # 颱風假 (Fri; ad-hoc typhoon closure announced 7/9 — day+night halted;
                   #   7/9 eve night session ran normally to 7/10 05:00 per TAIFEX practice)
    "2026-09-25",  # 中秋節 (Fri)
    "2026-09-28",  # 教師節 (Mon)
    "2026-10-09",  # 國慶日補假 (Fri; 10/10 Sat)
    "2026-10-26",  # 光復節補假 (Mon; 10/25 Sun)
    "2026-12-25",  # 行憲紀念日 (Fri)
})


def is_market_holiday(d):
    """d: datetime.date (or datetime) -> True if TAIFEX is closed for a holiday that day.
    Slicing isoformat to 10 chars tolerates a datetime being passed by mistake."""
    return d.isoformat()[:10] in TW_MARKET_HOLIDAYS


def is_trading_day(d):
    """d: datetime.date -> True if a normal TAIFEX trading day (weekday & not holiday)."""
    return d.weekday() <= 4 and not is_market_holiday(d)


# Latest year the table above actually covers. Bump when adding a new year's
# calendar. expiry_warning() lets the trader announce the table aging out
# instead of silently treating un-tabled holidays as trading days (2026-07-19
# audit: 2027-01-01 would restart-storm the armed tick-stale kill all day).
_CALENDAR_MAX_YEAR = 2026


def expiry_warning(d):
    """Optional[str]: a human-readable warning when the holiday table no longer
    (or soon won't) cover the date. December of the last covered year warns
    ahead of time; any date past the covered range warns loudly."""
    if d.year > _CALENDAR_MAX_YEAR:
        return (f"TAIFEX holiday calendar only covers up to {_CALENDAR_MAX_YEAR} — "
                f"{d.year} holidays are NOT gated (armed watchdogs may restart-storm "
                f"on holiday dead feeds). Update market_holidays.py.")
    if d.year == _CALENDAR_MAX_YEAR and d.month == 12:
        return (f"TAIFEX holiday calendar ends after {_CALENDAR_MAX_YEAR} — add the "
                f"{_CALENDAR_MAX_YEAR + 1} calendar before New Year "
                f"(first uncovered holiday: {_CALENDAR_MAX_YEAR + 1}-01-01).")
    return None
