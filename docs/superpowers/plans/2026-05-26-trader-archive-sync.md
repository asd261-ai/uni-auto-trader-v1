# Trader-Archive Nightly Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily at 05:30 TW (after night-session close) a launchd-scheduled bash script pulls a READ-ONLY snapshot of the trader's ledger files (`trades.jsonl`, `orders.jsonl`, `monthly_summary.jsonl`, `mtx_state.json`) and the Worker's current-month `/api/history` JSON to `400_Outputs/trader_archive/YYYY-MM-DD/`, so future analyses have complete monthly samples even if upstream loses data.

**Architecture:** Local Mac bash script (`000_Agent/scripts/trader-archive-sync.sh`) using `scp` + `curl`, plus a launchd plist (`000_Agent/launchd/com.seanchen.trader-archive-sync.plist`, installed into `~/Library/LaunchAgents/`) with `StartCalendarInterval` 21:30 UTC = 05:30 TW. Read-only on VPS + Worker. Idempotent (same-day re-run overwrites). Partial-failure tolerant (keep what was fetched, log per-file errors, exit non-zero).

**Tech Stack:** bash (`set -uo pipefail`), system `scp`/`ssh`/`curl`/`launchctl`/`plutil`. No deps. Runs on the Mac at `/Users/seanchen/Claude_Agent/`.

**Spec:** `docs/superpowers/specs/2026-05-26-trader-archive-sync-design.md`

---

### Task 0: Branch

**Files:** none

- [ ] **Step 1: Create a feature branch in the Claude_Agent workspace repo**

```bash
cd /Users/seanchen/Claude_Agent
git checkout -b trader-archive-sync
```

(Other repos are untouched — this work lives entirely in the `Claude_Agent` workspace repo.)

---

### Task 1: Create the sync script + verify `--dry-run`

**Files:**
- Create: `000_Agent/scripts/trader-archive-sync.sh`

- [ ] **Step 1: Create the script with the full content below**

Create `000_Agent/scripts/trader-archive-sync.sh`:

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

# Trading-day-just-ended: use (now - 30 min) so 05:30 TW resolves to today's date (after 05:00 close).
DAY=$(date -v-30M '+%Y-%m-%d')
MONTH=$(date -v-30M '+%Y-%m')
OUT="$ARCHIVE_ROOT/$DAY"
LOG="$ARCHIVE_ROOT/.sync.log"

log() { printf '%s [trader-archive] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG" >&2; }

if [[ $DRY_RUN -eq 1 ]]; then
  mkdir -p "$ARCHIVE_ROOT"
  log "DRY-RUN: would write to $OUT/ ; files = trades.jsonl orders.jsonl monthly_summary.jsonl mtx_state.json worker_history_$MONTH.json"
  exit 0
fi

mkdir -p "$OUT"
log "begin sync -> $OUT"

overall_rc=0
for f in trades.jsonl orders.jsonl monthly_summary.jsonl mtx_state.json; do
  if scp -pq "$VPS_HOST:$VPS_DIR/$f" "$OUT/$f" 2>/dev/null; then
    sz=$(wc -c <"$OUT/$f" | tr -d ' ')
    log "  ok  $f ($sz bytes)"
  else
    if ssh -q -o BatchMode=yes "$VPS_HOST" "test -f $VPS_DIR/$f" 2>/dev/null; then
      log "  ERR $f (ssh OK, scp failed)"
      overall_rc=1
    else
      log "  skip $f (not on VPS — OK if expected)"
    fi
  fi
done

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

- [ ] **Step 2: Make it executable**

```bash
chmod +x /Users/seanchen/Claude_Agent/000_Agent/scripts/trader-archive-sync.sh
```

- [ ] **Step 3: Run `--dry-run` and verify no archive folder is created**

Run: `/Users/seanchen/Claude_Agent/000_Agent/scripts/trader-archive-sync.sh --dry-run`
Expected:
- stderr prints a `... DRY-RUN: would write to ...` line.
- exit code 0 (`echo $?` → 0).
- `400_Outputs/trader_archive/` exists (the `.sync.log` parent), but `400_Outputs/trader_archive/<today>/` was NOT created (dry-run skips the `mkdir -p "$OUT"`).
- `.sync.log` has one new line containing `DRY-RUN`.

Verify:
```bash
ls -la /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/
tail -1 /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.sync.log
```

- [ ] **Step 4: Commit**

```bash
cd /Users/seanchen/Claude_Agent
git add 000_Agent/scripts/trader-archive-sync.sh
git commit -m "feat(archive): trader-archive-sync.sh bash script + --dry-run verified"
```

---

### Task 2: Real-run verification + iterate if needed

**Files:** No new files; possibly tweak `000_Agent/scripts/trader-archive-sync.sh`.

- [ ] **Step 1: Run the script for real**

Run: `/Users/seanchen/Claude_Agent/000_Agent/scripts/trader-archive-sync.sh`
Capture: exit code (`echo $?`).

Expected: per-file log lines (some `ok`, possibly some `skip` for files not on VPS like `monthly_summary.jsonl` or `mtx_state.json` if absent). One `ok worker_history_<YYYY-MM>.json` line. End line `end sync (rc=0)` if all expected files succeeded; `rc=1` if any unexpected error.

- [ ] **Step 2: Verify the archive folder is populated**

```bash
DAY=$(date -v-30M '+%Y-%m-%d')
ls -la /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/$DAY/
wc -l /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/$DAY/trades.jsonl
wc -l /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/$DAY/orders.jsonl
```
Expected: at minimum `trades.jsonl` + `orders.jsonl` + `worker_history_<YYYY-MM>.json` exist with non-zero size. `monthly_summary.jsonl` and `mtx_state.json` may or may not exist depending on VPS state — that's acceptable (skip logged).

- [ ] **Step 3: Spot-check size match VS VPS**

```bash
ssh uni-trader "wc -c /home/ubuntu/uni-auto-trader-v1/trades.jsonl"
DAY=$(date -v-30M '+%Y-%m-%d')
wc -c /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/$DAY/trades.jsonl
```
Expected: same byte count (or differs by at most one trade's worth if the bot wrote during the copy window — acceptable).

- [ ] **Step 4: Verify `.sync.log` content + JSON sanity**

```bash
DAY=$(date -v-30M '+%Y-%m-%d')
tail -20 /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.sync.log
head -c 200 /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/$DAY/worker_history_$(date -v-30M '+%Y-%m').json
```
Expected: log shows `begin sync` → per-file lines → `end sync (rc=0|1)`. Worker JSON starts with `[` or `{`.

- [ ] **Step 5: If anything failed unexpectedly, tweak the script and re-run; otherwise no-op**

If `rc=1` for a reason other than a known-missing VPS file (e.g., ssh alias broken, Worker non-JSON), STOP and report what was observed. Otherwise no script change needed.

- [ ] **Step 6: Commit (only if script was tweaked)**

```bash
cd /Users/seanchen/Claude_Agent
git add 000_Agent/scripts/trader-archive-sync.sh
git commit -m "fix(archive): trader-archive-sync.sh post-real-run tweaks"
```
If no tweaks needed, skip this commit.

---

### Task 3: Create the launchd plist + lint

**Files:**
- Create: `000_Agent/launchd/com.seanchen.trader-archive-sync.plist`

- [ ] **Step 1: Create the plist with the content below**

Create `000_Agent/launchd/com.seanchen.trader-archive-sync.plist`:

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
    <key>Hour</key>   <integer>21</integer>
    <key>Minute</key> <integer>30</integer>
  </dict>
  <key>RunAtLoad</key>          <false/>
  <key>StandardOutPath</key>    <string>/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.launchd.out</string>
  <key>StandardErrorPath</key>  <string>/Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.launchd.err</string>
</dict>
</plist>
```

Note: `Hour: 21 Minute: 30` is UTC (launchd ignores local TZ by default), so this fires at 21:30 UTC = 05:30 TW.

- [ ] **Step 2: Lint the plist**

```bash
plutil -lint /Users/seanchen/Claude_Agent/000_Agent/launchd/com.seanchen.trader-archive-sync.plist
```
Expected: prints `... : OK`.

- [ ] **Step 3: Commit**

```bash
cd /Users/seanchen/Claude_Agent
mkdir -p 000_Agent/launchd
git add 000_Agent/launchd/com.seanchen.trader-archive-sync.plist
git commit -m "feat(archive): launchd plist for nightly trader-archive sync (05:30 TW)"
```

---

### Task 4 — ASK-FIRST: Install + load the launchd job (production scheduling)

> **STOP at this task. Do NOT execute without Sean's explicit go.** Installing the plist starts an auto-scheduled job on Sean's Mac (outward-facing — the script will SSH to the live trader and hit the Worker API every day). After Sean approves, run the steps below.

**Files:** copies the plist to `~/Library/LaunchAgents/`.

- [ ] **Step 1: Copy the plist to LaunchAgents**

```bash
cp /Users/seanchen/Claude_Agent/000_Agent/launchd/com.seanchen.trader-archive-sync.plist \
   ~/Library/LaunchAgents/com.seanchen.trader-archive-sync.plist
```

- [ ] **Step 2: Load the job**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seanchen.trader-archive-sync.plist
```
(If `bootstrap` errors with "service already loaded", first `launchctl bootout gui/$(id -u)/com.seanchen.trader-archive-sync` and retry. On older macOS without `bootstrap`, use `launchctl load ~/Library/LaunchAgents/com.seanchen.trader-archive-sync.plist`.)

- [ ] **Step 3: Verify the job is registered**

```bash
launchctl print gui/$(id -u)/com.seanchen.trader-archive-sync | head -30
```
Expected: a block of plist metadata; no error; `state = waiting` or similar.

- [ ] **Step 4: Trigger an immediate fire to verify end-to-end (optional but recommended)**

```bash
launchctl kickstart -k gui/$(id -u)/com.seanchen.trader-archive-sync
sleep 5
DAY=$(date -v-30M '+%Y-%m-%d')
tail -10 /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/.sync.log
ls -la /Users/seanchen/Claude_Agent/400_Outputs/trader_archive/$DAY/
```
Expected: a fresh `begin sync ... end sync (rc=0|1)` block in `.sync.log`; the day folder has the expected files (likely identical to Task 2's manual run since it's the same day — idempotent overwrite).

(Note: `kickstart -k` is "kill-and-start" — safe here because the script has no long-running state.)

---

## Verification (end-to-end after Task 4)

- `.sync.log` has the launchd-triggered run block.
- Tomorrow 05:30 TW: a new dated folder appears automatically. Confirm 24h after install.
- One week later: 7 dated folders, no unexplained ERR lines in `.sync.log`.
- **Primary success metric (end of June 2026):** `400_Outputs/trader_archive/2026-06-*/` has a complete daily ledger ready to feed the next carry-vs-flat re-run (per [[project-may-carry-vs-flat-study]]).
