"""Retired Supabase runtime handoff; no profile, credential or network access."""

from __future__ import annotations

from pathlib import Path

from .guard import DeploymentError


def handoff(
    *,
    host: str,
    deployment_id: str,
    profile: Path,
    key: Path,
    known_hosts: Path,
    ssh: Path,
    confirm: bool = False,
) -> dict:
    raise DeploymentError("Supabase runtime handoff retired; no credentials read or sent.")


def main() -> int:
    print("Supabase runtime handoff retired; no credentials read or sent.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
