#!/bin/bash
# One-shot recovery for uni-trader after the 2026-05-23 weekend broker-login outage.
# Verify broker login is back BEFORE restarting, so we don't re-create the zombie
# (login RuntimeError leaves a dead main thread the SDK C-extension keeps alive).
# Runs as root (via systemd-run/at); python runs as ubuntu for .env/.venv perms.
set -u
LOG=/tmp/uni-recover.log
DIR=/home/ubuntu/uni-auto-trader-v1

if runuser -u ubuntu -- bash -c "cd $DIR && timeout 45 ./.venv/bin/python verify_login.py" \
        >/tmp/uni-recover-verify.log 2>&1; then
    # Broker login OK. Smoke-test the fill schema gate before restart: never bring
    # the real-money trader up on a broken parse_fill (a garbled gate would let a
    # junk matchprice contaminate order_log P&L / FILL_ANCHOR FIFO). Fail loud and
    # do NOT restart if the gate tests fail.
    if runuser -u ubuntu -- bash -c "cd $DIR && ./.venv/bin/python -m unittest test_fill_schema" \
            >/tmp/uni-recover-smoke.log 2>&1; then
        # Cool-off ≥30s before restart so the fresh login's dquote subscribe isn't
        # rate-limited (known broker behavior on rapid logins).
        sleep 35
        systemctl restart uni-trader
        sleep 12
        ACTIVE=$(systemctl is-active uni-trader)
        STARTED=$(journalctl -u uni-trader --since "40 sec ago" --no-pager 2>/dev/null | grep -c "AutoTrader started")
        echo "$(date '+%F %T') recover: login OK + fill-gate OK -> restarted; active=$ACTIVE started_log=$STARTED" >>"$LOG"
    else
        echo "$(date '+%F %T') recover: login OK but FILL-GATE TESTS FAILED -> NOT restarted (check feed_schema.py + /tmp/uni-recover-smoke.log)" >>"$LOG"
    fi
else
    echo "$(date '+%F %T') recover: login STILL FAIL -> not restarted (broker still down)" >>"$LOG"
fi
