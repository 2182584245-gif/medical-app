"""Root-only bounded SSH-stdin receiver; never receives an admin DB credential."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .configure import configure_supabase_values
from .guard import ROOT, DeploymentError, marker


def receive(raw: bytes, expected_deployment: str) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise DeploymentError("Receiver requires the authorized deployment administrator.")
    if (not re.fullmatch(r"[a-f0-9]{32}", expected_deployment)
            or marker(ROOT / "deployment.json")["id"] != expected_deployment):
        raise DeploymentError("Deployment identity mismatch; no secret installed.")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 32768:
        raise DeploymentError("Invalid handoff size.")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        payload = json.loads(raw, object_pairs_hook=unique)
        if (set(payload) != {"version", "deployment_id", "runtime_url", "ca"}
                or type(payload["version"]) is not int or payload["version"] != 1
                or payload["deployment_id"] != expected_deployment):
            raise ValueError
        configure_supabase_values(payload["runtime_url"], payload["ca"])
    except Exception:
        raise DeploymentError("Restricted runtime handoff refused; details hidden.") from None
    return {"status": "runtime_installed", "deployment_id": expected_deployment,
            "administrator_credential_received": False, "cloud_tables_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-id", required=True)
    args = parser.parse_args()
    try:
        result = receive(sys.stdin.buffer.read(32769), args.deployment_id)
        print(json.dumps(result))
        return 0
    except Exception:
        print("Runtime handoff refused or incomplete; no credential details displayed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
