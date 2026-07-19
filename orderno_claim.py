"""Live sent→reply orderno claiming (P4-2, 2026-07-20 design).

The shared account (0239174) broadcasts EVERY order's replies and matches into
the bot's callbacks — Sean's manual same-product events included. The SDK has
no client correlation token (orderno is broker-assigned, async-only), so the
bot tells its own events apart the same way pnl_calc.bot_ordernos already does
offline (2026-06-19 provenance design, proven on real data):

    a 'sent' the bot just performed is claimed by the FIRST reply carrying a
    new orderno with the same productid+bs within LINK_WINDOW_SEC.

Claimed ordernos are "ours"; matches/rejects for unclaimed ordernos are
foreign. Pure, lock-free (caller is the single SDK callback thread), bounded.

See docs/superpowers/specs/2026-07-20-p4-flat-checkpoint-and-orderno-claim-design.md
"""
from __future__ import annotations

from collections import OrderedDict

# Mirrors pnl_calc._LINK_WINDOW_SEC (real data: send→reply lands the same second).
LINK_WINDOW_SEC = 3.0


class OrdernoClaimer:
    def __init__(self, link_window_sec: float = LINK_WINDOW_SEC, max_ordernos: int = 200):
        self._window = float(link_window_sec)
        self._max = int(max_ordernos)
        self._sents: list = []            # [(ts, productid, bs)] awaiting a reply
        self._bot: OrderedDict = OrderedDict()   # orderno -> True (insertion-ordered, capped)
        self._seen_replies: set = set()   # ordernos whose FIRST reply was already processed

    def note_sent(self, ts: float, productid: str, bs: str) -> None:
        """Register a bot order the moment issend succeeds."""
        self._sents.append((float(ts), productid, bs))

    def note_reply(self, ts: float, productid: str, bs: str, orderno) -> bool:
        """Process one reply. Returns True iff this reply belongs to a bot order
        (already-claimed orderno, or it just claimed an outstanding sent).

        Only the FIRST reply per orderno may claim a sent — the broker emits
        several replies per order and a duplicate must not shadow the next
        order's claim (2026-07-06 dup-reply incident, same rule as offline
        bot_ordernos)."""
        if not orderno:
            return False
        if orderno in self._bot:
            return True
        if orderno in self._seen_replies:
            return False                  # first reply already judged it foreign
        self._seen_replies.add(orderno)
        if len(self._seen_replies) > self._max * 4:
            self._seen_replies = set(list(self._seen_replies)[-self._max * 2:])
        ts = float(ts)
        # Drop sents that can no longer be claimed by anything.
        self._sents = [s for s in self._sents if ts - s[0] <= self._window]
        for i, (sts, spid, sbs) in enumerate(self._sents):   # oldest first
            if spid == productid and sbs == bs and 0 <= ts - sts <= self._window:
                del self._sents[i]        # each sent backs exactly one orderno
                self._bot[orderno] = True
                while len(self._bot) > self._max:
                    self._bot.popitem(last=False)
                return True
        return False

    def is_bot_fill(self, orderno) -> bool:
        return orderno in self._bot
