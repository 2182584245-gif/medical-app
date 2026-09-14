"""Root-only fixed-path ciphertext receiver, invoked over pinned SSH stdin.

No plaintext API key or database credential is accepted. Existing key material
is never replaced; a byte-identical replay is safe after an uncertain response.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path

from .guard import ROOT, DeploymentError, host_root, marker

MAGIC = b"HEALTHLIFE-DEFAULT-AI-1\n"


def _private_directory(path: Path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise DeploymentError("Default AI paths cannot contain links.")
    if not path.parent.is_dir():
        raise DeploymentError("Expected existing deployment secrets parent.")
    if not path.exists():
        path.mkdir(mode=0o750)
        os.chown(path, 0, 10001)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o027:
        raise DeploymentError("AI directory must be root-owned and private.")
    # Empty directories made by prepare are root/0700. Grant traverse to the
    # dedicated runtime group, never to other users or through a writable group.
    os.chown(path, 0, 10001)
    os.chmod(path, 0o750)


def _same_existing(path, expected):
    if path.is_symlink():
        raise DeploymentError("Existing AI secret is not an ordinary private file.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 10001
            or stat.S_IMODE(info.st_mode) != 0o400
        ):
            raise DeploymentError("Existing AI secret ownership or permissions differ.")
        return hmac.compare_digest(stream.read(8193), expected)


def _install(path, raw):
    if path.exists():
        if not _same_existing(path, raw):
            raise DeploymentError("A different AI secret already exists; not replaced.")
        return False
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchown(stream.fileno(), 10001, 10001)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def receive(raw: bytes, expected_deployment: str):
    if os.name != "posix" or os.geteuid() != 0:
        raise DeploymentError("Authorized Linux root receiver required.")
    host_root()
    if (
        not re.fullmatch(r"[a-f0-9]{32}", expected_deployment)
        or marker(ROOT / "deployment.json")["id"] != expected_deployment
    ):
        raise DeploymentError("Deployment identity differs; no secret installed.")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 16384:
        raise DeploymentError("Invalid bounded AI payload.")
    try:

        def unique(pairs):
            value = {}
            for name, item in pairs:
                if name in value:
                    raise ValueError
                value[name] = item
            return value

        value = json.loads(raw, object_pairs_hook=unique)
        if (
            set(value) != {"version", "deployment_id", "kek", "envelope"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["deployment_id"] != expected_deployment
        ):
            raise ValueError
        kek = base64.b64decode(value["kek"], validate=True)
        envelope = base64.b64decode(value["envelope"], validate=True)
        if (
            len(kek) != 32
            or len(set(kek)) < 16
            or not envelope.startswith(MAGIC)
            or not len(MAGIC) + 28 < len(envelope) <= 8192
        ):
            raise ValueError
    except Exception:
        raise DeploymentError("AI payload format refused; details hidden.") from None
    key_dir = ROOT / "secrets" / "ai-key"
    envelope_dir = ROOT / "secrets" / "ai-envelope"
    _private_directory(key_dir)
    _private_directory(envelope_dir)
    targets = (
        (key_dir / "default-ai-kek.bin", kek),
        (envelope_dir / "deepseek-key.aesgcm", envelope),
    )
    # Validate BOTH existing files before any new file; no silent rotation.
    for path, content in targets:
        if path.exists() and not _same_existing(path, content):
            raise DeploymentError("Existing AI material differs; nothing replaced.")
    created = sum(_install(path, content) for path, content in targets)
    return {
        "status": "default_ai_installed" if created else "default_ai_already_installed",
        "deployment_id": expected_deployment,
        "plaintext_key_received": False,
        "cloud_tables_changed": False,
        "runtime_decryption_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-id", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(receive(sys.stdin.buffer.read(16385), args.deployment_id)))
        return 0
    except Exception:
        print("Default AI handoff unverified; secrets and raw errors are hidden.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
