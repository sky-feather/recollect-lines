"""Deterministic CLI stand-in for no-model-call compatibility probe tests."""
import sys


def main() -> int:
    if "--version" in sys.argv:
        print("fake-compat-cli 9.9.9-test")
        return 0
    if "--help" in sys.argv:
        print("Usage: fake-compat-cli [options]")
        print("  resume   Resume an existing session")
        print("  session  Manage sessions")
        print("  continue Continue from checkpoint")
        return 0
    print("unexpected invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
