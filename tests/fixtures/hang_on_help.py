"""CLI stand-in that hangs on --help for subprocess lifecycle tests."""
import sys
import time

if __name__ == "__main__":
    if "--version" in sys.argv:
        print("hang-on-help 1.0.0")
        raise SystemExit(0)
    if "--help" in sys.argv:
        time.sleep(3600)
    raise SystemExit(2)
