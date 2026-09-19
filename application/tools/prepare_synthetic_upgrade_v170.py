"""Prepare private, reviewed fictional migration inputs; never connect or apply.

Output contains fictional account password hashes, not plaintext credentials.
Keep it outside Git/release archives and transfer it only over authenticated SSH.
No production snapshot, private desktop copy or cloud credential is accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from server.platform_transfer import deserialize, digest, serialize, sqlite_snapshot
from tools.plan_synthetic_upgrade_v170 import BUSINESS_SHA, PROFILE, ROLES, _business_digest
from tools.upgrade_synthetic_demo_v7 import contract

HISTORY_SHA = "59c89581628363ec546c3630b970f5735593546f93074f84f2e0b6320b342072"
BASELINE_SHA = "c0474d2ebedd9198cc46c571ae4f8f0d8674719cad2ccf08c7e1ad10b3e637b3"
FILENAMES = ("baseline.json", "source.json", "history.json", "manifest.json")


class PreparationError(RuntimeError):
    """Diagnostics intentionally exclude input contents and credentials."""


def _require(condition, message):
    if not condition:
        raise PreparationError(message)


def _path(value):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts
             and not str(path).startswith("\\\\"), "Explicit local absolute paths are required.")
    for current in (path, *path.parents):
        if not os.path.lexists(current):
            continue
        details = current.lstat()
        _require(not stat.S_ISLNK(details.st_mode)
                 and not getattr(details, "st_file_attributes", 0) & 0x400,
                 "Links and reparse points are forbidden.")
        _require(stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode),
                 "Only ordinary local files and directories are supported.")
        _require(not stat.S_ISREG(details.st_mode) or details.st_nlink == 1,
                 "Hard-linked inputs are forbidden.")
    return path.resolve()


def _outside_git(path):
    _require(not any(os.path.lexists(parent / ".git") for parent in (path, *path.parents)),
             "Private input packages must remain outside Git repositories.")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _secure_new_directory(path):
    """Remove inherited Windows ACLs before writing any input contents."""
    path.mkdir(mode=0o700, exist_ok=False)
    if os.name != "nt":
        path.chmod(0o700)
        _require(stat.S_IMODE(path.stat().st_mode) == 0o700, "Private permissions failed.")
        return "owner-only-posix"
    command = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    result = subprocess.run(
        [*command, "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    sid = result.stdout.strip()
    _require(result.returncode == 0 and re.fullmatch(r"S-1-5-\d+(?:-\d+)+", sid),
             "Could not establish the current local Windows identity.")
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
         "/remove:g", "*S-1-3-4", "*S-1-5-32-544"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    _require(result.returncode == 0, "Could not protect the new input directory.")
    environment = os.environ.copy()
    environment["MEDICAL_SYNTHETIC_INPUT_PATH"] = str(path)
    # A caller running PowerShell 7 may export a module path incompatible with
    # Windows PowerShell 5.1. Let the child reconstruct its own built-in modules.
    environment.pop("PSModulePath", None)
    script = (
        "$ErrorActionPreference='Stop'; "
        "$a=[System.IO.Directory]::GetAccessControl($env:MEDICAL_SYNTHETIC_INPUT_PATH); "
        "[pscustomobject]@{protected=$a.AreAccessRulesProtected; rules=@($a.Access | "
        "ForEach-Object {[pscustomobject]@{sid=$_.IdentityReference.Translate("
        "[System.Security.Principal.SecurityIdentifier]).Value; "
        "inherited=$_.IsInherited; allow=($_.AccessControlType -eq 'Allow'); "
        "full=($_.FileSystemRights -eq 'FullControl')}})} | ConvertTo-Json -Depth 4 -Compress"
    )
    result = subprocess.run(
        [*command, script], env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    try:
        acl = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise PreparationError("Could not verify private directory permissions.") from None
    _require(result.returncode == 0 and acl.get("protected") is True
             and {row.get("sid") for row in acl.get("rules", [])} == {sid, "S-1-5-18"}
             and all(row.get("inherited") is False and row.get("allow") is True
                     and row.get("full") is True for row in acl["rules"]),
             "Unexpected access rules on the private input directory.")
    return "current-user-and-system-only-windows"


def _check_payload(baseline, source, history):
    """Pin the actual previously deployed baseline, not an arbitrary recomputed plan."""
    historic = dict(history)
    claimed = historic.pop("plan_sha256", None)
    _require(claimed == HISTORY_SHA and digest(historic) == HISTORY_SHA,
             "History differs from the reviewed actual cloud import plan.")
    _require(history.get("mode") == "append-only"
             and history.get("status") == "review_required"
             and history.get("choices", {}).get("renames", {}) == {},
             "Unexpected historical import policy.")
    baseline["identity"] = history["source"]
    _require(digest(baseline) == history.get("source_sha256") == BASELINE_SHA,
             "Baseline does not reproduce the previously imported source.")
    _require(_business_digest(source["rows"]) == BUSINESS_SHA,
             "Source does not match the reviewed expanded fictional corpus.")
    for snapshot in (baseline, source):
        actual = {row["username"]: row["role_code"] for row in snapshot["rows"]["users"]}
        _require(actual == ROLES and len(snapshot["rows"]["users"]) == 5,
                 "Only the five reviewed fictional identities are accepted.")


def prepare_inputs(baseline_root, source_root, history_file, destination):
    baseline_root, source_root, history_file, destination = (
        _path(value) for value in (baseline_root, source_root, history_file, destination)
    )
    for root in (baseline_root, source_root, history_file, destination):
        _outside_git(root)
    _require(not destination.exists() and destination.parent.is_dir()
             and destination.suffix == "", "Use a new, extension-free destination directory.")
    _require(all(not destination.is_relative_to(root) and not root.is_relative_to(destination)
                 for root in (baseline_root, source_root, history_file.parent)),
             "Output must not overlap source material.")
    common = contract()
    common.verify_payload(baseline_root)
    common.sibling("_demo_v170").verify_payload(source_root)
    inputs = [*(baseline_root / name for name in common.PAYLOAD_FILES),
              *(source_root / name for name in common.PAYLOAD_FILES), history_file]
    before = {item: _sha(item) for item in inputs}
    history = deserialize(history_file.read_bytes())
    baseline = sqlite_snapshot(
        baseline_root / "data/app.db", label="historic-fictional-source", asset_root=baseline_root
    )
    source = sqlite_snapshot(source_root / "data/app.db", label=PROFILE, asset_root=source_root)
    _check_payload(baseline, source, history)
    payloads = {"baseline.json": serialize(baseline), "source.json": serialize(source),
                "history.json": serialize(history)}
    _require(all(_sha(path) == value for path, value in before.items()),
             "Input files changed during preparation; no package was created.")
    access = _secure_new_directory(destination)
    manifest = {
        "version": 1, "kind": "private-reviewed-fictional-inputs", "profile": PROFILE,
        "public_distribution_allowed": False, "contains_plaintext_credentials": False,
        "contains_fictional_password_hashes": True, "contains_cloud_target_snapshot": False,
        "source_read_only": True, "cloud_connected": False, "cloud_modified": False,
        "access_policy": access, "baseline_rebind_verified": True,
        "baseline_source_sha256": digest(baseline), "source_snapshot_sha256": digest(source),
        "source_business_sha256": BUSINESS_SHA, "historical_plan_sha256": HISTORY_SHA,
        "baseline_db_sha256": before[baseline_root / "data/app.db"],
        "source_db_sha256": before[source_root / "data/app.db"],
        "source_counts": {table: len(rows) for table, rows in source["rows"].items()},
        "baseline_counts": {table: len(rows) for table, rows in baseline["rows"].items()},
        "files": {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                  for name, raw in payloads.items()},
        "consumer": "server.platform_transfer.deserialize (binary asset decoding required)",
        "instructions": "Root-only server staging; never place in Git, public ZIP or web root. "
                        "Obtain the target snapshot only on the server. Review plan before apply.",
    }
    # Manifest is written last: an interrupted directory is never a complete package.
    for name, raw in {**payloads, "manifest.json": serialize(manifest)}.items():
        with (destination / name).open("xb") as stream:
            stream.write(raw)
        if os.name != "nt":
            (destination / name).chmod(0o600)
    _require(set(item.name for item in destination.iterdir()) == set(FILENAMES),
             "Unexpected output contents; keep the directory private and inspect manually.")
    for name, raw in payloads.items():
        _require((destination / name).read_bytes() == raw,
                 "Output verification failed; retain private files for inspection.")
    return {"destination": str(destination), **manifest,
            "manifest_sha256": _sha(destination / "manifest.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare_inputs(args.baseline, args.source, args.history, args.destination)
    except Exception:
        print("Preparation refused or interrupted; no cloud action occurred. "
              "Any newly created directory remains private. Raw errors are suppressed.")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
