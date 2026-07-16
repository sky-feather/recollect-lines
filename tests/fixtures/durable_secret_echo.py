"""Echo a secret from env/argv to output — manifest must never capture it."""
import os
import sys

secret = os.environ.get("RL_SECRET_SENTINEL", "")
prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
print(f"out:{secret}:{prompt}", flush=True)
sys.stderr.write(f"err:{secret}\n")
raise SystemExit(0)
