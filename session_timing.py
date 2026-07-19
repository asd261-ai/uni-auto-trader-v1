"""Pure timing decision for the deferred session-close summary.

No SDK/strategy imports -> unit-testable on system python3 (python3 -m unittest
test_session_timing). The session summary is delayed ~5 min after the day/night->break
transition so bell/session_end closes are captured before the tally is sent.
"""


def session_summary_action(prev_session, new_session, pending_session, due_at, now, delay):
    """Decide deferred session-summary firing. Returns dict:
       fire            : session str to send the summary for NOW, or None
       pending_session : new pending state (session str or None)
       due_at          : new due timestamp (float)

    Rules:
    - day/night -> break transition: schedule pending=prev, due=now+delay
      (and fire any already-pending summary first — shouldn't happen, delay << session gap).
    - no transition, pending set and now>=due: fire pending, clear it.
    - otherwise: state unchanged, no fire.
    """
    if prev_session in ("day", "night") and new_session == "break":
        return {"fire": pending_session, "pending_session": prev_session, "due_at": now + delay}
    if pending_session is not None and now >= due_at:
        return {"fire": pending_session, "pending_session": None, "due_at": 0.0}
    return {"fire": None, "pending_session": pending_session, "due_at": due_at}


def weekend_dormant(weekday: int, session: str) -> bool:
    """Whether trading-side loops (poll entries, recon, margin, tick watchdog)
    should treat "now" as the dead weekend.

    `weekday >= 5` alone is WRONG for Sat 00:00–05:00 — that is the live tail of
    Friday's night session (2026-07-19 audit: poll muted there left held
    positions without exit management, and every watchdog slept through an
    active session). Dormant requires BOTH a weekend calendar day AND no active
    session; _get_session already labels the Sat-dawn tail 'night' and knows
    holidays, so it is the session authority here."""
    return weekday >= 5 and session not in ("day", "night")
