"""Pure settlement-window detection for the MTX auto-trader.

No SDK / network / strategy imports — unit-testable on system python3
(python3 -m unittest test_settlement_calendar). All inputs passed in, no hidden clock.

TAIFEX equity-index futures settle on the 3rd Wednesday of the delivery month at the
13:30 day-session close; the night session (15:00) trades the next month. During
13:30-15:00 on the settlement day the front contract has settled, so no ticks arrive
and any held position is gone at the broker. Callers use this to treat that window as
"break" (no tick-stale kill, no trading) — see strategy._get_session.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

SETTLEMENT_START = time(13, 30)   # day session settles
SETTLEMENT_END = time(15, 0)      # night session opens


def third_wednesday(year: int, month: int) -> date:
    """The 3rd Wednesday of the month (nominal TAIFEX settlement day)."""
    first = date(year, month, 1)
    first_wed_offset = (2 - first.weekday()) % 7   # weekday(): Mon=0 .. Wed=2 .. Sun=6
    return date(year, month, 1 + first_wed_offset + 14)


def is_settlement_window(now: datetime, override_date: Optional[date] = None) -> bool:
    """True iff `now` (TW-local) is on the settlement day AND time in [13:30, 15:00).

    Settlement day = override_date if given (holiday shift), else the 3rd Wednesday of
    now's month. override_date is passed in (no env coupling) so this stays pure.
    """
    settle_day = override_date or third_wednesday(now.year, now.month)
    return now.date() == settle_day and SETTLEMENT_START <= now.time() < SETTLEMENT_END
