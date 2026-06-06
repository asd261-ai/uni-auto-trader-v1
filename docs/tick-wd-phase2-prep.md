# Tick-stale watchdog — Phase 2 flip prep (target 2026-06-09)

**Status:** PREPARED 2026-06-06, NOT applied. Working tree stays == VPS so Monday's
trades.jsonl observe-first reconciliation compares cleanly. Apply on 6/9 (ask-first deploy).

## What Phase 2 does

Phase 1 (LIVE since 2026-05-25, observe-only) routes tick-stale alerts to the **log** only:
`lambda m: logger.warning(f"[tick-wd OBSERVE] {m}")`. Phase 2 routes the **same alerts to the
Health bot Telegram channel** (and keeps logging) so Sean is actually notified when the dquote
feed goes silent mid-session.

This is the `lambda → real notify` flip only. It is **NOT** the kill-tier arm — that is a
separate env flip (`TICK_STALE_KILL=on`, Phase B) covered at the bottom.

## Why it's safe (no Telegram spam)

`TickStaleWatchdog.check()` (tick_watchdog.py):
- **one-shot latch** `_alert_sent` → exactly ONE alert per outage episode + ONE recovery msg.
- internal **30 s throttle** (`check_interval`) + session/weekend gate before any notify.
- callback receives a **fully-formatted message** string → `_safe_health_notify(text)` takes it
  directly, signatures already compatible (verified 2026-06-06).

Phase 1 evidence backing the flip: **6/1 08:46 first true-positive** (fd-leak feed death caught),
weekend 6/6–6/8 **zero false alerts** (fd=6, Errno24=0, TICK_STALE=0, NRestarts=0).

## The exact change — 2 edits to strategy.py

### Edit 1 — add `_tick_wd_notify` method (mirrors `_tick_wd_kill`)

Insert immediately BEFORE `def _tick_wd_kill(self, msg: str) -> None:` (currently ~line 1901):

```python
    def _tick_wd_notify(self, msg: str) -> None:
        # Phase 2 (LIVE 2026-06-09): real Health-bot alert + keep the VPS log trail.
        # The watchdog passes a fully-formatted message and self-latches, so this fires
        # at most once per outage + once on recovery — no Telegram spam.
        logger.warning(f"[tick-wd] {msg}")
        self._safe_health_notify(msg)

```

### Edit 2 — swap the observe lambda for the real callback

Find (currently lines ~642-651):

```python
            # Tick-stale watchdog: detect if the dquote feed goes silent during an active
            # session (self-throttled + session/weekend-gated inside check()).
            # PHASE 1 (observe-only): route alerts to the log, NOT Telegram — validates
            # thresholds/gating live on viploginm with zero noise and zero trading impact.
            # PHASE 2: change the notify callback to self._safe_health_notify for real alerts.
            try:
                self._tick_wd.check(
                    time.time(), self._current_session,
                    datetime.now(TZ_TW).weekday() >= 5,
                    lambda m: logger.warning(f"[tick-wd OBSERVE] {m}"),  # PHASE 2: -> self._safe_health_notify
                    uptime=time.time() - self._proc_start_ts,
                    on_kill=self._tick_wd_kill,
                )
```

Replace with:

```python
            # Tick-stale watchdog: detect if the dquote feed goes silent during an active
            # session (self-throttled + session/weekend-gated inside check()).
            # PHASE 2 (LIVE 2026-06-09): real alerts route to the Health bot via
            # _tick_wd_notify (also logged). Phase 1 observe-only validated 5/25-6/8
            # (6/1 08:46 true-positive; weekend 6/6-6/8 zero false alerts).
            try:
                self._tick_wd.check(
                    time.time(), self._current_session,
                    datetime.now(TZ_TW).weekday() >= 5,
                    self._tick_wd_notify,  # PHASE 2 LIVE: Health-bot alert + log
                    uptime=time.time() - self._proc_start_ts,
                    on_kill=self._tick_wd_kill,
                )
```

## 6/9 deploy runbook (ask-first before scp)

1. Confirm weekend 6/6-6/8 stayed clean: `grep -c 'tick-wd OBSERVE' <vps log>` over the window
   should show only expected entries (ideally zero false alerts during active sessions).
2. Apply Edit 1 + Edit 2 above.
3. `python3 -m py_compile strategy.py` + run the suite: `python3 -m unittest test_tick_watchdog -v`
   (the watchdog logic is unchanged so existing tests still cover it; the new method is a thin
   log+notify wrapper). Optionally add a one-line test that `_tick_wd_notify` calls both.
4. sha256sum the local strategy.py, **ask Sean** ("我要 scp strategy.py 上 VPS 開 tick-wd Phase 2
   Telegram 告警,影響=feed 靜默時真的發 Health bot,風險=極低已 latch+throttle。可以?"), then scp +
   `trader-precheck.sh && systemctl restart uni-trader`, verify sha256 on VPS matches.
5. Smoke: confirm boot clean, no immediate tick-wd alert (means feed healthy at boot).

## Phase B (separate, also ~6/9) — arm the kill tier

Independent of the code flip above. `TICK_STALE_KILL` is env-only (strategy.py:163). Arming it
makes a sustained outage escalate to `os._exit(1)` → systemd restart (fd reclaim). Decide
AFTER Phase 2 alerting is confirmed healthy, as its own ask-first env change:
`TICK_STALE_KILL=on` in `.env.uni-trader` + restart. No code edit needed.
