from __future__ import annotations

import argparse

from .config import Settings
from .database import initialize_development_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Local development schema tooling only")
    parser.add_argument("command", choices=["init-db"])
    parser.parse_args()
    initialize_development_database(Settings())
    print("Development pilot tables initialized. No users or demo records were inserted.")


if __name__ == "__main__":
    main()
