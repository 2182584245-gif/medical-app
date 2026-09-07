"""Generate reviewable pilot SQL without credentials or any database connection."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from alembic import command
from alembic.config import Config


def render_sql(*, downgrade: bool = False) -> str:
    buffer = io.StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=buffer)
    if downgrade:
        command.downgrade(config, "pilot_0001:base", sql=True)
    else:
        command.upgrade(config, "pilot_0001", sql=True)
    heading = (
        "-- REVIEW ONLY: four-table cloud pilot; not the full desktop schema.\n"
        "-- Generated offline: not executed and not PostgreSQL integration-tested.\n"
        "-- Use an explicitly confirmed TEST project and separate migration credentials.\n"
        "-- Never use migration/admin credentials in the public backend or desktop.\n"
    )
    if downgrade:
        heading += (
            "-- DESTRUCTIVE WHEN EXECUTED: only explicit reviewed rollback is allowed.\n"
            "-- Refuses non-empty tables and unowned/extra objects; retains metadata schema.\n"
        )
    return heading + buffer.getvalue()


def export_sql(destination: Path) -> tuple[Path, Path]:
    # Render both scripts first. An output directory is never silently reused.
    forward, backward = render_sql(), render_sql(downgrade=True)
    destination.mkdir(parents=True, exist_ok=False)
    upgrade = destination / "pilot_0001_upgrade_REVIEW_ONLY.sql"
    downgrade = destination / "pilot_0001_downgrade_EXPLICIT_ONLY.sql"
    upgrade.write_text(forward, encoding="utf-8", newline="\n")
    downgrade.write_text(backward, encoding="utf-8", newline="\n")
    return upgrade, downgrade


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        paths = export_sql(args.output_dir)
    except FileExistsError:
        parser.error("Output already exists; choose a new directory. Nothing was overwritten.")
    print("Generated two review-only SQL files. No database was contacted or modified.")
    for path in paths:
        print(path.resolve())


if __name__ == "__main__":
    main()
