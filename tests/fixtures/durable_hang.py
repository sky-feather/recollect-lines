"""Hang until signalled — for timeout/cancel and broker-loss tests."""
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_DFL)
time.sleep(300)
