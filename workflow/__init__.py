"""obs-rust workflow automation: claims, the org board and the coordinator's
work list, with a production (gh) and a simulation (SQLite) backend."""

VERBOSE = True  # the simulation turns this off


def log(message: str) -> None:
    if VERBOSE:
        print(message)
