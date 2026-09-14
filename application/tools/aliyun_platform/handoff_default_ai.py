"""Prepare DPAPI private retry material, then send ciphertext over pinned SSH stdin."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path

from .guard import DeploymentError, public_host

SOURCE = Path(__file__).resolve().parents[2]
MAGIC = b"HEALTHLIFE-DEFAULT-AI-1\n"


def _package_path(package):
    from ollama_chat_app.security.private_payload import _safe_path

    package = _safe_path(package)
    if package == SOURCE or SOURCE in package.parents:
        raise DeploymentError("Private AI retry material must remain outside the source tree.")
    return package


def _validated_payload(value, deployment_id):
    try:
        if (
            type(value) is not dict
            or set(value) != {"version", "deployment_id", "kek", "envelope"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["deployment_id"] != deployment_id
            or type(value["kek"]) is not str
            or len(value["kek"]) != 44
            or type(value["envelope"]) is not str
            or len(value["envelope"]) > 10924
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
        raw = json.dumps(value).encode()
        if len(raw) > 16384:
            raise ValueError
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        body = envelope[len(MAGIC) :]
        clear = AESGCM(kek).decrypt(body[:12], body[12:], b"HealthLife/server-default-ai/v1")
        if not re.fullmatch(rb"sk-[A-Za-z0-9_-]{16,200}", clear):
            raise ValueError
        return raw
    except Exception:
        raise DeploymentError("Private AI package is invalid; nothing sent.") from None


def prepare(package: Path, deployment_id: str):
    if os.name != "nt" or not re.fullmatch(r"[a-f0-9]{32}", deployment_id):
        raise DeploymentError("Explicit Windows deployment identity required.")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from ollama_chat_app.security.private_payload import protect_payload
    from ollama_chat_app.security.secret_store import SecretStore

    package = _package_path(package)
    if package.exists():
        raise DeploymentError("Private retry package already exists; not replaced.")
    secret = SecretStore().get_default_api_key("deepseek_cloud")
    if not secret or not re.fullmatch(r"sk-[A-Za-z0-9_-]{16,200}", secret):
        raise DeploymentError("No valid Windows-encrypted default DeepSeek key is available.")
    kek, nonce = os.urandom(32), os.urandom(12)
    envelope = (
        MAGIC
        + nonce
        + AESGCM(kek).encrypt(nonce, secret.encode(), b"HealthLife/server-default-ai/v1")
    )
    value = {
        "version": 1,
        "deployment_id": deployment_id,
        "kek": base64.b64encode(kek).decode(),
        "envelope": base64.b64encode(envelope).decode(),
    }
    encrypted = protect_payload(
        _validated_payload(value, deployment_id), "default-ai-handoff/" + deployment_id
    )
    package.parent.mkdir(parents=True, exist_ok=True)
    _package_path(package)
    # Exclusive creation prevents a concurrent prepare from overwriting the
    # immutable package needed to recover an uncertain remote acknowledgement.
    descriptor = os.open(package, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "status": "private_retry_package_prepared",
        "cloud_connected": False,
        "plaintext_key_saved": False,
    }


def handoff(
    *,
    package: Path,
    host: str,
    deployment_id: str,
    key: Path,
    known_hosts: Path,
    ssh: Path,
    confirm: bool,
):
    if not confirm or os.name != "nt" or not re.fullmatch(r"[a-f0-9]{32}", deployment_id):
        raise DeploymentError("Explicit authorized Windows administrator handoff required.")
    public_host(host)
    from ollama_chat_app.security.private_payload import _safe_path, read_private_json

    for path in (key, known_hosts, ssh):
        if not path.is_absolute() or not _safe_path(path).is_file():
            raise DeploymentError("Explicit private key and pinned host file required.")
    value = read_private_json(_package_path(package), "default-ai-handoff/" + deployment_id)
    raw = _validated_payload(value, deployment_id)
    options = [
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=NUL",
        "KnownHostsCommand=none",
        "ProxyCommand=none",
        "ProxyJump=none",
        "ClearAllForwardings=yes",
        "ForwardAgent=no",
        "ControlMaster=no",
        "ControlPath=none",
        "IdentityAgent=none",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "ConnectTimeout=10",
        "UserKnownHostsFile=" + known_hosts.as_posix(),
    ]
    command = [str(ssh), "-F", "none", "-T", "-i", str(key)]
    for option in options:
        command.extend(["-o", option])
    command.extend(
        [
            "admin@" + host,
            "cd /opt/medical-app/administration && sudo -n python3 "
            "-m tools.aliyun_platform.receive_default_ai --deployment-id " + deployment_id,
        ]
    )
    try:
        result = subprocess.run(command, input=raw, capture_output=True, timeout=45)
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ValueError
        report = json.loads(result.stdout)
        if (
            report.get("status") not in {"default_ai_installed", "default_ai_already_installed"}
            or report.get("deployment_id") != deployment_id
        ):
            raise ValueError
        return {
            "status": report["status"],
            "deployment_id": deployment_id,
            "plaintext_key_sent": False,
            "cloud_tables_changed": False,
            "runtime_decryption_verified": False,
        }
    except Exception:
        raise DeploymentError(
            "AI handoff unverified. Inspect or retry SAME private package only."
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "send"))
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--host")
    parser.add_argument("--key", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--ssh", type=Path)
    parser.add_argument("--confirm-default-ai-handoff", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            prepare(args.package, args.deployment_id)
            if args.action == "prepare"
            else handoff(
                package=args.package,
                deployment_id=args.deployment_id,
                host=args.host,
                key=args.key,
                known_hosts=args.known_hosts,
                ssh=args.ssh,
                confirm=args.confirm_default_ai_handoff,
            )
        )
        print(json.dumps(result))
        return 0
    except Exception:
        print("Default AI preparation or handoff unverified; no secret details displayed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
