# Spec: Nightly trader-archive sync — daily VPS + Worker snapshot to local

**Date:** 2026-05-26
**Status:** Approved (design), proceeding to implementation plan
**Area:** Local Mac infra (bash script + launchd) — pulls READ-ONLY from VPS + Worker
**Repo:** `asd261-ai/uni-auto-trader-v1` (spec lives here; implementation files live in `~/Claude_Agent/000_Agent/scripts/` + `~/Library/LaunchAgents/`)

## Context

Today's 2026-05 carry-vs-flat / pre-close-blackout analysis ([[project-may-carry-vs-flat-study]]) ran into two
data-quality gaps that limited statistical power:

- **`trades.jsonl` on the VPS started at 2026-05-15** — the first half of May was either bot-off or
  pre-instrumentation. Either way, history older than that was unrecoverable.
- **Worker `/api/history` `closedAt` field was populated for only 28/219 May signals (13%)** — many trades
  lacked the close timestamp needed for boundary classification. Today's Worker session-end-close fix
  ([[project-pnl-calc-contract-mixing]] index entry) addresses this going forward, but the historical
  gap is permanent.

These files live only on the VPS / Worker KV. Any restart bug, disk failure, KV cap (Worker
`signal_history` has a 50-item cap before today's monthly-archive write was wired), or accidental
overwrite is silent loss.

**The fix:** every night after the bell, pull a complete snapshot of the trader's ledger files + the
Worker's current-month history to a dated local folder. Cheap, idempotent, read-only — guarantees future
analyses (June-end re-run, etc.) have a full month even if upstream loses data.

## Goal / Non-goals

- **Goal:** every trading-day close, archive a complete snapshot of the trader's signal ledger
  (`trades.jsonl`), real-fill ledger (`orders.jsonl`), monthly summary (`monthly_summary.jsonl`),
  current open state (`mtx_state.json`), and the Worker's current-month `signal_history` JSON to a
  dated local folder. Read-only on both ends. Idempotent. Survives a sleeping Mac (launchd queue-on-wake).
- **Non-goals:** real-time / sub-daily sync; remote off-Mac backup; Telegram notification on failure;
  alerting; archiving any signal source beyond `signal_history` (e.g. `fvg_30m_signal_history`);
  pulling Worker KV directly (the `/api/history` endpoint is sufficient).

## Decision (from brainstorming)

- **Where it runs:** Mac local bash script + launchd plist (same family as the existing journal-sync
  pipeline at `000_Agent/scripts/journal-sync.sh`). Must be local because the VPS sync needs the Mac's
  SSH key for the `uni-trader` host alias; a cloud RemoteTrigger cannot reach the VPS.
- **When:** 05:30 TW (= 21:30 UTC) daily. Captures the full prior trading day (night session ends 05:00).
  launchd `StartCalendarInterval` queues-on-wake by default, so a sleeping Mac runs the job when next
  woken — no missed days.
- **Output path:** `~/Claude_Agent/400_Outputs/trader_archive/YYYY-MM-DD/` where the date is the trading
  day just ended (compute from "now − 30 min", so 05:30 TW resolves to "today" since night session
  closed at 05:00 of today).
- **What's archived:** `trades.jsonl`, `orders.jsonl`, `monthly_summary.jsonl`, `mtx_state.json`,
  `worker_history_<YYYY-MM>.json`.

## Design

### Output layout
```
400_Outputs/trader_archive/
├── 2026-05-26/
│   ├── trades.jsonl
│   ├── orders.jsonl
│   ├── monthly_summary.jsonl       (may be missing on a fresh month — that's fine)
│   ├── mtx_state.json              (may be missing if never written — that's fine)
│   └── worker_history_2026-05.json
├── 2026-05-25/...
└── .sync.log                       (single append-only log; one timestamped line per run + per file)
```
Daily folder is overwritten in place (idempotent — running twice in the same day is a no-op data-wise).
Old folders are kept indefinitely (trades ~1–2 KB × ~10/day × 365 ≈ 5 MB/year; cost is irrelevant).

### Script: `000_Agent/scripts/trader-archive-sync.sh`

Bash, set -uo pipefail (NOT -e: we want partial-success when some files are missing). Reads everything
read-only; writes ONLY to the local archive dir + log file.

```bash
#!/usr/bin/env bash
# trader-archive-sync.sh — pull a daily snapshot of trader + Worker data to local archive.
# READ-ONLY on VPS + Worker. Idempotent. Safe on partial failure.
# Schedule via ~/Library/LaunchAgents/com.seanchen.trader-archive-sync.plist (daily 05:30 TW).
# Manual: ./trader-archive-sync.sh            (real run)
#         ./trader-archive-sync.sh --dry-run  (list what would be done)

set -uo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

ARCHIVE_ROOT="$HOME/Claude_Agent/400_Outputs/trader_archive"
VPS_HOST="uni-trader"
VPS_DIR="/home/ubuntu/uni-auto-trader-v1"
WORKER_BASE="https://mtx-monitor.asd261-af5.workers.dev"

# Trading-day-just-ended: use (now − 30 min) so 05:30 TW resolves to today's date (after 05:00 close).
DAY=$(date -v-30M '+%Y-%m-%d')
MONTH=$(date -v-30M '+%Y-%m')
OUT="$ARCHIVE_ROOT/$DAY"
LOG="$ARCHIVE_ROOT/.sync.log"

log() { printf '%s [trader-archive] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG" >&2; }

if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY-RUN: would write to $OUT/ ; files = trades.jsonl orders.jsonl monthly_summary.jsonl mtx_state.json worker_history_$MONTH.json"
  exit 0
fi

mkdir -p "$OUT"
log "begin sync → $OUT"

# 1) VPS files (scp -p preserves mtime; -q quiet). Missing files = OK (e.g. monthly_summary on day 1).
overall_rc=0
for f in trades.jsonl orders.jsonl monthly_summary.jsonl mtx_state.json; do
  if scp -pq "$VPS_HOST:$VPS_DIR/$f" "$OUT/$f" 2>/dev/null; then
    sz=$(wc -c <"$OUT/$f" | tr -d ' ')
    log "  ok  $f ($sz bytes)"
  else
    # Distinguish "file missing on VPS" (acceptable) from "ssh failed" (real error).
    if ssh -q -o BatchMode=yes "$VPS_HOST" "test -f $VPS_DIR/$f" 2>/dev/null; then
      log "  ERR $f (ssh OK, scp failed)"
      overall_rc=1
    else
      log "  skip $f (not on VPS — OK if expected)"
    fi
  fi
done

# 2) Worker /api/history?month=<current>. curl with timeout; sanity-check it's JSON with at least []/{ }.
W="$OUT/worker_history_$MONTH.json"
if curl -sS --max-time 30 "$WORKER_BASE/api/history?month=$MONTH" -o "$W"; then
  if head -c 1 "$W" | grep -qE '^[\[{]'; then
    sz=$(wc -c <"$W" | tr -d ' ')
    log "  ok  worker_history_$MONTH.json ($sz bytes)"
  else
    log "  ERR worker_history_$MONTH.json (not JSON, removed)"
    rm -f "$W"
    overall_rc=1
  fi
else
  log "  ERR worker_history_$MONTH.json (curl failed)"
  overall_rc=1
fi

log "end sync (rc=$overall_rc)"
exit $overall_rc
```

### launchd plist: `~/Library/LaunchAgents/com.seanchen.trader-archive-sync.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.seanchen.trader-archive-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/seanchen/Claude_Agent/000_Agent/scripts/trader-archive-sync.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>   <integer>21</integer>   <!-- 21:30 UTC = 05:30 TW -->
    <key>Minute</key> <integer>30</integer>
  </dict>
  <key>RunAtLoad</key>          <false/>
  <key>StandardOutPath</key>    <string>/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.launchd.out</string>
  <key>StandardErrorPath</key>  <string>/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.launchd.err</string>
</dict>
</plist>
```

Loaded with `launchctl bootstrap gui/$(id -u) <plist>` (or `launchctl load` on older macOS).
launchd's default queue-on-wake means if the Mac is asleep at 21:30 UTC, the job fires on next wake.

### SSH config sanity
`uni-trader` Host alias is already configured in `~/.ssh/config` (used all session today). Script uses
`BatchMode=yes` for the existence-probe so a missing alias / refused key fails cleanly without prompting.

## Error handling / edge cases

- **scp of one file fails, others succeed:** keep the ones that succeeded, set `overall_rc=1`, log
  per-file error. Folder is partial-but-readable; tomorrow's run will refill (trades/orders are
  append-only). No half-written corruption.
- **Worker curl fails:** likewise — keep VPS files, set rc=1, log.
- **Worker returns non-JSON** (e.g. HTML error page during a deploy): script detects `head -c 1` isn't
  `[`/`{`, removes the bad file, logs ERR, rc=1.
- **SSH alias missing / key rejected:** existence-probe falls through to "ssh failed" branch, rc=1.
- **Disk full:** scp/curl will error; rc=1. No silent corruption.
- **Running twice in the same day:** identical output → overwrites itself → no harm. Idempotent by design.
- **Running while Sean's actively using the VPS:** read-only file copy, no locking risk (these are
  append-only / atomically-rewritten files; worst case a snapshot is one-line-stale, which next day's
  run fixes).
- **No `mtx_state.json` yet (flat across sessions):** "skip" branch logs cleanly. Not an error.

## Testing

`trader-archive-sync.sh` is integration-shaped (depends on ssh + curl), so tests are manual:
1. `./trader-archive-sync.sh --dry-run` → prints intent, exits 0, writes nothing.
2. Real run → verify `400_Outputs/trader_archive/<today>/` has the expected files with non-zero sizes;
   `.sync.log` has one block of lines.
3. Worker outage simulation: temporarily point WORKER_BASE at an unreachable host → confirm rc=1,
   VPS files still archived, log shows ERR for worker_history.
4. After plist install: `launchctl print gui/$(id -u)/com.seanchen.trader-archive-sync` shows the job;
   `launchctl kickstart -k gui/$(id -u)/com.seanchen.trader-archive-sync` triggers an immediate run.

## Verification (end-to-end)

1. After install + first scheduled fire (05:30 TW), `ls 400_Outputs/trader_archive/<date>/` lists all
   expected files; sizes match VPS (`ssh uni-trader "ls -l /home/ubuntu/uni-auto-trader-v1/{trades,orders,monthly_summary}.jsonl"`).
2. `.sync.log` shows a successful block (5 lines + begin/end markers).
3. A week later: 7 dated folders, no failures in log.
4. June 30: the June carry-vs-flat re-run has a complete `400_Outputs/trader_archive/2026-06-*/`
   ledger to feed off — primary success metric.

## Files touched

- `000_Agent/scripts/trader-archive-sync.sh` (new, ~70 lines bash)
- `~/Library/LaunchAgents/com.seanchen.trader-archive-sync.plist` (new)
- `400_Outputs/trader_archive/` (new directory, populated by the script — git-ignored / outside repos)

Nothing in the trader or Worker repos changes. No git commits required for the runtime files (they
live in the Claude_Agent workspace, not in either code repo).
