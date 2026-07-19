"""
Append today's MXFG5 day-session close to daily_closes.json.

Sources tried in order (first successful wins):
  1. Worker /api/bars (if available — currently NOT implemented, placeholder)
  2. Manual --close <price> override (for backfill or when source unavailable)

Usage:
  # Auto (currently only --close mode works since Worker has no bars API yet):
  python3 update_daily_close.py --close 40720

  # Backfill multiple days:
  python3 update_daily_close.py --backfill backfill.csv
  # where backfill.csv has rows: 2026-04-15,40500

Schema of daily_closes.json (list of objects, sorted by date ascending):
  [
    {"date": "2026-04-16", "close": 40123, "source": "manual"},
    {"date": "2026-04-17", "close": 40250, "source": "manual"},
    ...
  ]

Run at 13:50 TW (5 min after day session close) — either manually or via cron:
  50 13 * * 1-5 cd /home/ubuntu/uni-auto-trader-v1 && python3 update_daily_close.py --close $(...)
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

STATE_PATH = Path(__file__).parent / "daily_closes.json"


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return []


def save_state(state):
    state.sort(key=lambda x: x["date"])
    # Atomic (2026-07-19 audit): the live trader reads this file cross-process
    # in _check_regime; an in-place truncate-write raced that read and could
    # cache a bogus 'undefined' regime off a partial file.
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)


def append_close(d: str, close: float, source: str = "manual"):
    state = load_state()
    # Dedupe by date — overwrite if same date
    state = [s for s in state if s["date"] != d]
    state.append({"date": d, "close": float(close), "source": source})
    save_state(state)
    print(f"✓ Recorded {d} close={close} source={source} (total {len(state)} days)")


def backfill(csv_path: str):
    """CSV format: date,close (no header)."""
    import csv
    with open(csv_path) as f:
        for row in csv.reader(f):
            if not row or row[0].startswith("#"):
                continue
            d, c = row[0].strip(), row[1].strip()
            append_close(d, float(c), source="backfill")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--close", type=float, help="Today's MXFG5 day-session close")
    p.add_argument("--date", type=str, help="Override date (YYYY-MM-DD), defaults today TW")
    p.add_argument("--backfill", type=str, help="CSV path with date,close rows")
    p.add_argument("--list", action="store_true", help="List current state")
    args = p.parse_args()

    if args.list:
        state = load_state()
        print(f"daily_closes.json: {len(state)} entries")
        for s in state[-10:]:
            print(f"  {s['date']}  {s['close']:>6}  {s.get('source', '?')}")
        return

    if args.backfill:
        backfill(args.backfill)
        return

    if args.close is None:
        print("Error: provide --close <price> or --backfill <csv> or --list", file=sys.stderr)
        sys.exit(1)

    d = args.date or datetime.now().strftime("%Y-%m-%d")
    append_close(d, args.close)


if __name__ == "__main__":
    main()
