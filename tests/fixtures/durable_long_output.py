"""Emit more bytes than the test runner's stdout cap."""
import sys

sys.stdout.write("X" * 200_000)
sys.stdout.flush()
raise SystemExit(0)
