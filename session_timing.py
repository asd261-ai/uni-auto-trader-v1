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
