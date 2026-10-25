"""obs-rust workflow automation: claims, the org board and the coordinator's
work list, with a production (gh) and a simulation (SQLite) backend."""

import sys

VERBOSE = True  # the simulation turns this off


def log(message: str) -> None:
    """Progress goes to stderr, so `--json` output stays parseable."""
    if VERBOSE:
        print(message, file=sys.stderr)
