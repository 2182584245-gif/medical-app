"""Explicit configuration changes; no credential rotation, remote DDL, or secret overwrite."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from .guard import ROOT, DeploymentError, host_root, marker, read_file
from .prepare import _new, caddyfile


def configure_supabase(url_file: Path, ca_file: Path) -> None:
    """Retired compatibility entry point: deliberately does not open either file."""
    raise DeploymentError("Supabase runtime retired; existing configuration and data preserved.")


def configure_supabase_values(value: str, ca: str) -> None:
    """Retired compatibility entry point; no validation, storage or network access."""
    raise DeploymentError("Supabase runtime retired; existing configuration and data preserved.")


def promote(*, confirm_production: bool) -> None:
    host_root()
    deployment = marker(ROOT / "deployment.json")
    if not confirm_production:
        raise DeploymentError("Production certificate issuance requires explicit confirmation.")
    path = ROOT / "config/Caddyfile"
    old = read_file(path)
    expected = caddyfile(deployment["public_host"]).strip()
    if old != expected:
        raise DeploymentError("Caddy configuration differs from the prepared staging version.")
    previous = (
        ROOT / "config" / ("Caddyfile.staging-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    _new(previous, old + "\n", 0o600)
    # Replace only the independently verified generated config; secrets and volumes unchanged.
    next_path = ROOT / "config/Caddyfile.production.pending"
    _new(next_path, caddyfile(deployment["public_host"], production=True), 0o644)
    next_path.replace(path)


def repair_ip_sni() -> None:
    """Add only the missing no-SNI certificate selection to a recognized config."""
    host_root()
    deployment = marker(ROOT / "deployment.json")
    path = ROOT / "config/Caddyfile"
    old = read_file(path)
    host = deployment["public_host"]
    for production in (False, True):
        desired = caddyfile(host, production=production)
        if old == desired.strip():
            return
        if old == desired.replace(f"    default_sni {host}\n", "").strip():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            _new(ROOT / "config" / ("Caddyfile.before-sni-" + stamp), old + "\n", 0o600)
            next_path = ROOT / "config" / ("Caddyfile.sni-pending-" + stamp)
            _new(next_path, desired, 0o644)
            next_path.replace(path)
            return
    raise DeploymentError("Unrecognized Caddy configuration; no edits made.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    production = commands.add_parser("production")
    production.add_argument("--confirm-production", action="store_true")
    commands.add_parser("repair-ip-sni")
    args = parser.parse_args()
    try:
        if args.action == "repair-ip-sni":
            repair_ip_sni()
        else:
            promote(confirm_production=args.confirm_production)
        print("Configuration prepared. Restart/recreate the affected service after review.")
        return 0
    except Exception:
        print(
            "Configuration refused; credentials and database contents are not displayed or changed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
