"""Explicit configuration changes; no credential rotation, remote DDL, or secret overwrite."""

from __future__ import annotations

import argparse
import ssl
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .guard import ROOT, DeploymentError, host_root, marker, read_file, secret
from .prepare import _new, caddyfile


def configure_supabase(url_file: Path, ca_file: Path) -> None:
    configure_supabase_values(read_file(url_file), read_file(ca_file))


def configure_supabase_values(value: str, ca: str) -> None:
    """Initialize the restricted runtime secret from memory, never logging it."""
    host_root()
    marker(ROOT / "deployment.json")
    folder = ROOT / "secrets/api-supabase"
    if any((folder / name).exists() for name in ("database-url", "database-ca.crt")):
        raise DeploymentError("Existing Supabase configuration is preserved; no overwrite allowed.")
    # No SQLAlchemy/desktop imports required on the production host.
    try:
        if (not isinstance(value, str) or not isinstance(ca, str)
                or len(value) > 16384 or len(ca) > 16384
                or any(ord(char) < 32 for char in value)):
            raise ValueError
        url = urlsplit(value)
        username = unquote(url.username or "")
        import re

        if (
            url.scheme != "postgresql+psycopg"
            or not url.hostname
            or url.port not in (None, 5432)
            or not url.path.strip("/")
            or not re.fullmatch(r"medical_app_platform_runtime(?:\.[a-z0-9]{20})?", username)
            or len(unquote(url.password or "")) < 32
            or not (
                url.hostname.endswith(".supabase.co")
                or url.hostname.endswith(".pooler.supabase.com")
            )
            or url.fragment
        ):
            raise ValueError
        query = dict(parse_qsl(url.query, strict_parsing=True))
        if query.get("sslmode") != "verify-full" or set(query) - {
            "sslmode",
            "sslrootcert",
            "connect_timeout",
        }:
            raise ValueError
        ssl.create_default_context(cadata=ca)
        query.update(sslrootcert="/run/api-secrets/database-ca.crt", connect_timeout="10")
        value = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
    except Exception:
        raise DeploymentError(
            "Use an existing restricted runtime URL with verify-full and valid CA."
        ) from None
    if secret(folder / "token-pepper") == secret(ROOT / "secrets/api-aliyun/token-pepper"):
        raise DeploymentError("The two API session peppers must differ.")
    _new(folder / "database-ca.crt", ca + "\n", 0o444)
    _new(folder / "database-url", value, 0o400, 10001)


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
    remote = commands.add_parser("supabase")
    remote.add_argument("--runtime-url-file", type=Path, required=True)
    remote.add_argument("--ca-file", type=Path, required=True)
    production = commands.add_parser("production")
    production.add_argument("--confirm-production", action="store_true")
    commands.add_parser("repair-ip-sni")
    args = parser.parse_args()
    try:
        if args.action == "supabase":
            configure_supabase(args.runtime_url_file, args.ca_file)
        elif args.action == "repair-ip-sni":
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
