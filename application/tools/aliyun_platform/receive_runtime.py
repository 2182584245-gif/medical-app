"""Retired runtime receiver; never consumes stdin or installs credentials."""

from __future__ import annotations

from .guard import DeploymentError


def receive(raw: bytes, expected_deployment: str) -> dict:
    raise DeploymentError("Supabase runtime receiver retired; no credentials installed.")


def main() -> int:
    print("Supabase runtime receiver retired; stdin not consumed, no credentials installed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
