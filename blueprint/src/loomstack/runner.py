"""Blueprint entry point — dispatched by NemoClaw via `python -m loomstack.runner`."""

import sys


def main() -> None:
    # Populated in a later task once dispatcher + CLI commands are wired.
    print("loomstack runner: not yet implemented", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
