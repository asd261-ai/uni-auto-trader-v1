import signal
import time
import logging

from config import CONFIG
from trader import AutoTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

trader = AutoTrader(CONFIG)


def _shutdown(sig, frame):
    print("\nShutting down...")
    trader.stop()
    exit(0)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

trader.start()

while True:
    time.sleep(1)
