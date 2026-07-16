"""Short deterministic payload for durable runner tests."""
import sys

print("hello-durable", flush=True)
sys.stderr.write("err-durable\n")
raise SystemExit(0)
