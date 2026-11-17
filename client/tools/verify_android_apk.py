"""Release artifact verification, without uploading APK or private data anywhere."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import marshal
import re
import struct
import subprocess
import types
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def elf_load_alignment(data: bytes) -> list[int]:
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("Expected 64-bit ELF")
    endian = "<" if data[5] == 1 else ">"
    offset = struct.unpack_from(endian + "Q", data, 32)[0]
    size, count = struct.unpack_from(endian + "HH", data, 54)
    return [
        struct.unpack_from(endian + "Q", data, offset + i * size + 48)[0]
        for i in range(count)
        if struct.unpack_from(endian + "I", data, offset + i * size)[0] == 1
    ]


def scan_archive(archive: zipfile.ZipFile, prefix: str = "", depth: int = 0) -> list[dict]:
    findings = []
    for entry in archive.infolist():
        if entry.filename.endswith(".so"):
            aligns = elf_load_alignment(archive.read(entry))
            findings.append(
                {
                    "path": prefix + entry.filename,
                    "alignments": aligns,
                    "pass_16kb": bool(aligns) and all(x >= 16384 for x in aligns),
                }
            )
        elif depth < 2 and entry.filename.endswith((".imy", ".zip")):
            payload = archive.read(entry)
            if payload[:4] == b"PK\x03\x04":
                with zipfile.ZipFile(io.BytesIO(payload)) as nested:
                    findings.extend(scan_archive(nested, prefix + entry.filename + "!", depth + 1))
    return findings


def run(executable: Path, *arguments: str) -> dict:
    result = subprocess.run(
        [str(executable), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    return {
        "passed": result.returncode == 0,
        "exit_code": result.returncode,
        "output": result.stdout + result.stderr,
    }


def verify(apk: Path, build_tools: Path, *, allow_debug: bool = False) -> dict:
    mobile_root = Path(__file__).resolve().parents[1]
    source_assets = mobile_root / "android/app/src/main/assets/www"
    source_errors = []
    python_errors = []
    python_sources = {}
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        crc = archive.testzip()
        native = scan_archive(archive)
        sensitive = [
            name
            for name in names
            if name.lower().endswith((".db", ".sqlite", ".jks", ".keystore", ".pem", ".dpapi.xml"))
        ]
        # certifi's public CA root bundle is expected; it is not a signing secret.
        sensitive = [name for name in sensitive if not name.endswith("cacert.pem")]
        assets = "assets/www/index.html" in names and "assets/mobile-resource-manifest.json" in names
        resource_errors = []
        resources = []
        if assets:
            resource_manifest = json.loads(archive.read("assets/mobile-resource-manifest.json"))
            resources = resource_manifest.get("files", [])
            expected = {"assets/www/" + entry["path"] for entry in resources}
            packaged = {name for name in names if name.startswith("assets/www/") and not name.endswith("/")}
            if expected != packaged:
                resource_errors.append("Packaged www files differ from the generated allow-list")
            for entry in resources:
                location = "assets/www/" + entry["path"]
                if location not in names:
                    resource_errors.append("Missing resource: " + entry["path"])
                    continue
                payload = archive.read(location)
                if len(payload) != entry["size_bytes"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                    resource_errors.append("Resource integrity mismatch: " + entry["path"])
                source = source_assets / entry["path"]
                if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
                    source_errors.append("Source changed after packaging: " + entry["path"])
            disk_resources = {path.relative_to(source_assets).as_posix() for path in source_assets.rglob("*") if path.is_file()}
            if disk_resources != {entry["path"] for entry in resources}:
                source_errors.append("Current www file set differs from the packaged version")
        assets = assets and bool(resources) and not resource_errors
        # Compare the actual Chaquopy code objects with current source, rather
        # than inferring facade freshness from www hashes or ZIP timestamps.
        entry_points = {
            **{source.name: source for source in sorted(
                (mobile_root / "android/app/src/main/python").glob("*.py")
            )},
            **{name: mobile_root / "src" / name for name in (
                "ollama_chat_app/web_desktop/member_bridge.py",
                "ollama_chat_app/services/chat_attachments.py",
                "ollama_chat_app/providers/deepseek_cloud.py",
            )},
        }
        try:
            with zipfile.ZipFile(io.BytesIO(archive.read("assets/chaquopy/app.imy"))) as python_archive:
                for name, source in entry_points.items():
                    payload = source.read_bytes()
                    packaged_code = marshal.loads(python_archive.read(name + "c")[16:])
                    expected_code = compile(payload, packaged_code.co_filename, "exec", dont_inherit=True, optimize=0)
                    same = isinstance(packaged_code, types.CodeType) and expected_code == packaged_code
                    python_sources[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "matches_packaged_bytecode": same}
                    if not same:
                        python_errors.append("Python source changed after packaging: " + name)
        except Exception as error:
            python_errors.append("Unable to compare compiled member runtime: " + type(error).__name__)
        duplicate_design = any(name.startswith("assets/design/") for name in names)
    signature = run(build_tools / "apksigner.bat", "verify", "--verbose", "--print-certs", str(apk))
    alignment = run(build_tools / "zipalign.exe", "-c", "-P", "16", "4", str(apk))
    manifest = run(
        build_tools / "aapt2.exe", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)
    )
    badging = run(build_tools / "aapt2.exe", "dump", "badging", str(apk))
    text = manifest["output"]
    debug_lines = [line for line in text.splitlines() if "android:debuggable" in line]
    nondebug = all("0xffffffff" not in line and "true" not in line for line in debug_lines)
    native_pass = bool(native) and all(item["pass_16kb"] for item in native)
    package_expected = "com.fawnsee.mobile.debug" if allow_debug else "com.fawnsee.mobile"
    gradle = (mobile_root / 'android/app/build.gradle').read_text(encoding='utf-8')
    version = re.search(r"versionName\s+'([^']+)'", gradle)
    code = re.search(r'versionCode\s+(\d+)', gradle)
    if not version or not code:
        raise RuntimeError('Current Android release identity is missing')
    version_expected = version.group(1) + ('-debug' if allow_debug else '')
    identity_pass = bool(
        "package: name='" + package_expected + "'" in badging["output"]
        and "versionName='" + version_expected + "'" in badging["output"]
        and "versionCode='" + code.group(1) + "'" in badging["output"]
    )
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {
        "apk": str(apk.resolve()),
        "size_bytes": apk.stat().st_size,
        "sha256": digest,
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "zip_crc_pass": crc is None,
        "mobile_assets_pass": assets,
        "resource_manifest_count": len(resources),
        "resource_manifest_errors": resource_errors,
        "current_source_resources_match": not source_errors,
        "current_source_resource_errors": source_errors,
        "python_runtime_matches_source": not python_errors,
        "python_runtime_source_errors": python_errors,
        "python_runtime_sources": python_sources,
        "duplicate_source_design_packaged": duplicate_design,
        "expected_identity_pass": identity_pass,
        "expected_debug_build": allow_debug,
        "unexpected_private_files": sensitive,
        "release_not_debuggable": nondebug,
        "signature": signature,
        "zip_alignment_16kb": alignment,
        "native_elf_16kb_pass": native_pass,
        "native_library_count": len(native),
        "native_libraries": native,
        "manifest": manifest,
        "badging": badging,
        "device_runtime_test": "Not implied by static artifact checks; consult device test report.",
    }
    result["passed"] = bool(
        crc is None
        and assets
        and not source_errors
        and not python_errors
        and not duplicate_design
        and not sensitive
        and (allow_debug or nondebug)
        and identity_pass
        and native_pass
        and signature["passed"]
        and alignment["passed"]
        and manifest["passed"]
        and badging["passed"]
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("apk", type=Path)
    parser.add_argument("--build-tools", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-debug", action="store_true", help="Verify the separate debug application ID; do not require non-debuggable release")
    args = parser.parse_args()
    report = verify(args.apk, args.build_tools, allow_debug=args.allow_debug)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "passed",
                    "size_bytes",
                    "sha256",
                    "native_library_count",
                    "native_elf_16kb_pass",
                    "release_not_debuggable",
                )
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["passed"] else 1)
