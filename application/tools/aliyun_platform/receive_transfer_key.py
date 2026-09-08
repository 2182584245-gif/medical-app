"""Root-only fixed-path receiver for an independent transfer key, not DB credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys

from .guard import ROOT, DeploymentError, host_root, marker


def private_directory(expected_deployment):
    if os.name != "posix" or os.geteuid() != 0:
        raise DeploymentError("Only the authorized Linux root administrator may receive a key.")
    host_root()
    if marker(ROOT / "deployment.json")["id"] != expected_deployment:
        raise DeploymentError("Deployment identity differs; no key received.")
    directory = ROOT / "administration" / "transfer-keys"
    for path in (directory, *directory.parents):
        if path.is_symlink():
            raise DeploymentError("Transfer key directory may not contain symlinks.")
    if not directory.parent.is_dir():
        raise DeploymentError("The explicitly installed administration directory is missing.")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise DeploymentError("Transfer key directory must be root-owned and private.")
    return directory


def receive(raw: bytes, expected_deployment: str, export_sha256: str):
    if (
        not re.fullmatch(r"[a-f0-9]{32}", expected_deployment)
        or not re.fullmatch(r"[a-f0-9]{64}", export_sha256)
        or not isinstance(raw, bytes)
        or len(raw) != 32
        or len(set(raw)) < 16
    ):
        raise DeploymentError("Invalid transfer key handoff identity or size.")
    directory = private_directory(expected_deployment)
    target = directory / (export_sha256 + ".key")
    # O_EXCL also rejects existing symlinks. Never replace a key or retry silently.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "status": "transfer_key_installed",
        "deployment_id": expected_deployment,
        "export_sha256": export_sha256,
        "database_credentials_received": False,
        "database_changed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--export-sha256", required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(receive(sys.stdin.buffer.read(33), args.deployment_id, args.export_sha256))
        )
        return 0
    except Exception:
        print("Transfer key handoff not verified; no secret or raw error displayed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
