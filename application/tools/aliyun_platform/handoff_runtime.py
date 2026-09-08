"""Windows administrator: DPAPI runtime profile -> pinned-host SSH anonymous stdin."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from .guard import DeploymentError, public_host


def handoff(*, host: str, deployment_id: str, profile: Path, key: Path,
            known_hosts: Path, ssh: Path, confirm: bool = False) -> dict:
    if not confirm or os.name != "nt":
        raise DeploymentError("Explicit Windows administrator handoff is required.")
    public_host(host)
    if not re.fullmatch(r"[a-f0-9]{32}", deployment_id):
        raise DeploymentError("An inspected deployment identifier is required.")
    for path in (key, known_hosts, ssh):
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise DeploymentError("Use explicit existing key, pinned-host file and SSH program.")
    from server.cloud_connection import ca_file
    from server.platform_admin import load_runtime_url
    uri = load_runtime_url(profile)
    ca = Path(ca_file(uri.query.get("sslrootcert"))).read_text(encoding="utf-8")
    payload = json.dumps({"version": 1, "deployment_id": deployment_id,
                          "runtime_url": uri.render_as_string(hide_password=False),
                          "ca": ca}).encode("utf-8")
    if len(payload) > 32768:
        raise DeploymentError("Runtime payload is too large.")
    command = [str(ssh), "-F", "none", "-T", "-i", str(key), "-o", "BatchMode=yes",
               "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "GlobalKnownHostsFile=NUL", "-o", "KnownHostsCommand=none",
               "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
               "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
               "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "IdentityAgent=none",
               "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
               "-o", "UserKnownHostsFile=" + known_hosts.as_posix(),
               "-o", "ConnectTimeout=10", "admin@" + host,
               "cd /opt/medical-app/administration && sudo -n python3 "
               "-m tools.aliyun_platform.receive_runtime --deployment-id " + deployment_id]
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=45)
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ValueError
        report = json.loads(result.stdout)
        if (report.get("status") != "runtime_installed"
                or report.get("deployment_id") != deployment_id):
            raise ValueError
        # Remote stdout is untrusted; do not relay additional fields or diagnostics.
        return {"status": "runtime_installed", "deployment_id": deployment_id,
                "administrator_credential_sent": False, "cloud_tables_changed": False}
    except Exception:
        raise DeploymentError(
            "SSH runtime handoff unverified; inspect target before retrying."
        ) from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--ssh", type=Path, required=True)
    parser.add_argument("--confirm-runtime-handoff", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(handoff(host=args.host, deployment_id=args.deployment_id,
              profile=args.profile, key=args.key, known_hosts=args.known_hosts,
              ssh=args.ssh, confirm=args.confirm_runtime_handoff)))
        return 0
    except Exception:
        print("Runtime handoff not verified; raw errors and credentials are hidden.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
