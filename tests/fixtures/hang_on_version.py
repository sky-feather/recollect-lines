"""CLI stand-in that hangs on --version for subprocess lifecycle tests."""
import sys
import time

if __name__ == "__main__":
    if "--version" in sys.argv:
        time.sleep(3600)
    if "--help" in sys.argv:
        print("help")
        raise SystemExit(0)
    raise SystemExit(2)
