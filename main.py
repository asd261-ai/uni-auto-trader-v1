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
    trader.start()
    strategy.start()

while True:
    time.sleep(1)
