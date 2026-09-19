"""Reproducible onedir Windows build with frozen source and pinned dependencies.

Use a NEW output directory on a drive with at least 5 GB free. This never
publishes, deploys, imports user databases, or changes antivirus settings.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_snapshot():
    paths = [
        *PROJECT.joinpath("src").rglob("*.py"),
        PROJECT / "packaging/app.spec",
        PROJECT / "packaging/version_info.txt",
        PROJECT / "packaging/entrypoint.py",
        PROJECT / "requirements-lock.txt",
    ]
    return {p.relative_to(PROJECT).as_posix(): digest(p) for p in sorted(paths)}


def run(command, name, root, env):
    with (
        (root / "validation" / f"{name}.stdout.log").open("x", encoding="utf-8") as out,
        (root / "validation" / f"{name}.stderr.log").open("x", encoding="utf-8") as err,
    ):
        result = subprocess.run(command, cwd=PROJECT, env=env, stdout=out, stderr=err, timeout=1800)
    if result.returncode:
        raise RuntimeError(f"{name} failed; original logs were retained in the output directory")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="Recheck a completed frozen build without rebuilding or overwriting logs",
    )
    args = parser.parse_args()
    root = args.output_root.resolve()
    if (
        os.name != "nt"
        or root == PROJECT
        or root.is_relative_to(PROJECT)
        or root.exists() != args.verify_existing
    ):
        raise RuntimeError("A new external Windows build directory is required")
    if not args.verify_existing and shutil.disk_usage(root.parent).free < 5 * 1024**3:
        raise RuntimeError("At least 5 GB free space is required for a safe isolated build")
    locks = re.findall(
        r"^([a-zA-Z0-9._-]+)==([^\s\\]+)",
        (PROJECT / "requirements-lock.txt").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    mismatches = []
    for name, version in locks:
        try:
            if importlib.metadata.version(name) != version:
                mismatches.append(name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(name)
    if not locks or mismatches:
        raise RuntimeError("Locked dependency mismatch: " + ", ".join(mismatches))
    if args.verify_existing:
        frozen = json.loads((root / "source-freeze.json").read_text(encoding="utf-8"))
        if source_snapshot() != frozen or (root / "build-summary.json").exists():
            raise RuntimeError(
                "Frozen source mismatch or summary already exists; nothing overwritten"
            )
    else:
        for child in ("validation", "temp"):
            (root / child).mkdir(parents=True, exist_ok=False)
        frozen = source_snapshot()
        (root / "source-freeze.json").write_text(json.dumps(frozen, indent=2), encoding="utf-8")
    env = dict(os.environ)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "QT_QPA_PLATFORM",
        "DEEPSEEK_API_KEY",
        "OLLAMA_API_KEY",
        "OPENAI_API_KEY",
        "HEALTHLIFE_CLOUD_BASE_URL",
    ):
        env.pop(key, None)
    env["PATH"] = str(Path(sys.executable).parent) + ";C:\\Windows\\System32;C:\\Windows"
    env["TEMP"] = env["TMP"] = str(root / "temp")
    env["PYTHONIOENCODING"] = "utf-8"
    suffix = "-recheck" if args.verify_existing else ""
    run([sys.executable, "-m", "pip", "check"], "pip-check" + suffix, root, env)
    if not args.verify_existing:
        run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--distpath",
                str(root / "dist"),
                "--workpath",
                str(root / "build"),
                "packaging/app.spec",
            ],
            "build",
            root,
            env,
        )
    if source_snapshot() != frozen:
        raise RuntimeError("Source changed during build: keep evidence and use a new build folder")
    env["PYTHONPATH"] = str(PROJECT / "src") + ";" + str(PROJECT)
    distribution = root / "dist/健康生活服务平台"
    executable = distribution / "健康生活服务平台.exe"
    run(
        [
            sys.executable,
            "packaging/verify_build.py",
            str(root / "build/app/Analysis-00.toc"),
            str(distribution),
        ],
        "verify-build" + suffix,
        root,
        env,
    )
    run(
        [sys.executable, "packaging/verify_embedded_source.py", str(executable)],
        "verify-source" + suffix,
        root,
        env,
    )
    if source_snapshot() != frozen:
        raise RuntimeError("Source changed during final verification")
    version = re.search(
        r'^APP_VERSION = "([^"]+)"',
        (PROJECT / "src/ollama_chat_app/config.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )[1]
    result = {
        "status": "passed",
        "version": version,
        "utc": datetime.now(UTC).isoformat(),
        "locked_dependencies": len(locks),
        "frozen_sources": len(frozen),
        "executable": str(executable),
        "executable_sha256": digest(executable),
        "source_unchanged_during_build": True,
        "production_actions": 0,
        "reverified_existing_build": args.verify_existing,
    }
    (root / "build-summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
