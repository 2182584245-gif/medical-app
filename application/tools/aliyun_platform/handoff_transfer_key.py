"""Windows independent-key handoff through exclusively pinned SSH stdin."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from server.platform_transfer_package import read_key, safe_path

from .guard import DeploymentError, public_host


def handoff(
    *, host, deployment_id, export_sha256, key_file, ssh_key, known_hosts, ssh, confirm=False
):
    if not confirm or os.name != "nt":
        raise DeploymentError("Explicit Windows independent-key handoff confirmation is required.")
    public_host(host)
    if not re.fullmatch(r"[a-f0-9]{32}", deployment_id) or not re.fullmatch(
        r"[a-f0-9]{64}", export_sha256
    ):
        raise DeploymentError("Inspected deployment and export identities are required.")
    for path in (ssh_key, known_hosts, ssh):
        if not path.is_absolute():
            raise DeploymentError(
                "Explicit absolute SSH program/key/pinned-host paths are required."
            )
        safe_path(path, exists=True)
    raw = read_key(key_file)
    command = [
        str(ssh),
        "-F",
        "none",
        "-T",
        "-i",
        str(ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=NUL",
        "-o",
        "KnownHostsCommand=none",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "UserKnownHostsFile=" + known_hosts.as_posix(),
        "admin@" + host,
        "cd /opt/medical-app/administration && sudo -n python3 "
        "-m tools.aliyun_platform.receive_transfer_key --deployment-id "
        + deployment_id
        + " --export-sha256 "
        + export_sha256,
    ]
    try:
        result = subprocess.run(command, input=raw, capture_output=True, timeout=45)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError
        report = json.loads(result.stdout)
        if (
            report.get("status") != "transfer_key_installed"
            or report.get("deployment_id") != deployment_id
            or report.get("export_sha256") != export_sha256
        ):
            raise ValueError
        # Never relay arbitrary remote fields, stdout, stderr, or diagnostics.
        return {
            "status": "transfer_key_installed",
            "deployment_id": deployment_id,
            "export_sha256": export_sha256,
            "database_credentials_sent": False,
        }
    except Exception:
        raise DeploymentError(
            "Transfer key handoff unverified; inspect target before retrying."
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "deployment-id", "export-sha256"):
        parser.add_argument("--" + name, required=True)
    for name in ("key-file", "ssh-key", "known-hosts", "ssh"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(handoff(**vars(args))))
        return 0
    except Exception:
        print("Transfer key handoff not verified; secrets and raw errors are hidden.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
