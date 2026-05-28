import signal
import time
import logging
import os

from config import CONFIG
from trader import AutoTrader
from strategy import MTXStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

trader = AutoTrader(CONFIG)
strategy = MTXStrategy(trader, dry_run=DRY_RUN)
trader.strategy = strategy


def _shutdown(sig, frame):
    print("\nShutting down...")
    strategy.stop()
    trader.stop()
    exit(0)


def _toggle_log_level(sig, frame):
    # SIGUSR1: flip root logger between INFO and DEBUG at runtime (no restart).
    # Used to briefly capture DEBUG-level tick logs without disrupting trading.
    root = logging.getLogger()
    new_level = logging.INFO if root.level == logging.DEBUG else logging.DEBUG
    root.setLevel(new_level)
    logging.info(f"[SIGUSR1] log level → {logging.getLevelName(new_level)}")


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGUSR1, _toggle_log_level)

if DRY_RUN:
    # Dry run: 不需要真實登入，只啟動 strategy poller
    strategy.start()
    logging.info("Running in DRY RUN mode (no login, no real orders)")
else:
    # Wrap startup: any failure here (Login failed, contract resolve failed,
    # subscribe failed before the warn-only branch, etc.) MUST cause the process
    # to exit cleanly, not propagate as an uncaught exception. The unitrade SDK
    # C-extension keeps the process PID alive after the Python main thread dies,
    # leaving a zombie that systemd cannot detect (state stays active) and
    # cannot restart (Restart=always only fires on actual process exit).
    # os._exit(1) terminates immediately at the OS level — C-ext threads die
    # with the process, systemd sees a clean failure, Restart=always kicks in
    # after RestartSec=15.
    # See docs/superpowers/specs/2026-05-28-main-try-except-zombie-prevention.md
    # and [[feedback-trader-weekend-restart-zombie]] for the 5/23 incident and
    # 5/27 reproduction that motivated this fix.
    try:
        trader.start()
        strategy.start()
    except Exception as e:
        logging.exception(f"trader/strategy startup failed — exiting for systemd restart: {e}")
        import os as _os
        _os._exit(1)

while True:
    time.sleep(1)
