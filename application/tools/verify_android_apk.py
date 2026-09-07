"""Release artifact verification, without uploading APK or private data anywhere."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
import subprocess
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


def verify(apk: Path, build_tools: Path) -> dict:
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
        assets = all(f"assets/www/{name}" in names for name in ("index.html", "app.css", "app.js"))
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
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {
        "apk": str(apk.resolve()),
        "size_bytes": apk.stat().st_size,
        "sha256": digest,
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "zip_crc_pass": crc is None,
        "mobile_assets_pass": assets,
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
        and not sensitive
        and nondebug
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
    args = parser.parse_args()
    report = verify(args.apk, args.build_tools)
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
