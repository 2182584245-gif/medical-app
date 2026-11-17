"""Convert a checked APK report and signer readbacks into the update publisher audit."""
from argparse import ArgumentParser
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from publish_static_release import APPLICATION_ID, require, sha


BUILD_CHECKS = (
    "zip_crc_pass",
    "mobile_assets_pass",
    "current_source_resources_match",
    "python_runtime_matches_source",
    "expected_identity_pass",
    "release_not_debuggable",
    "native_elf_16kb_pass",
)


def verified_signer(text):
    fingerprints = re.findall(
        r"^Signer #[0-9]+ certificate SHA-256 digest: ([a-fA-F0-9]{64})\s*$",
        text,
        flags=re.MULTILINE,
    )
    require(text.splitlines()[:1] == ["Verifies"] and len(fingerprints) == 1,
            "Expected apksigner verification for exactly one signer")
    return fingerprints[0].lower()


def create_audit(apk, report, candidate_signature, previous_signature, badging):
    require(report.get("passed") is True and all(report.get(check) is True for check in BUILD_CHECKS),
            "The client APK build audit must fully pass")
    require(report.get("signature", {}).get("passed") is True
            and report.get("zip_alignment_16kb", {}).get("passed") is True,
            "The client APK build audit must pass signature and 16 KB alignment checks")
    size, checksum = apk.stat().st_size, sha(apk)
    require(size == report.get("size_bytes") and checksum == report.get("sha256"),
            "The candidate APK bytes no longer match the build audit")
    candidate_fingerprint = verified_signer(candidate_signature)
    require(candidate_fingerprint == verified_signer(previous_signature),
            "The candidate signer differs from the previous release")
    for version in (2, 3):
        require(f"Verified using v{version} scheme (APK Signature Scheme v{version}): true"
                in candidate_signature.splitlines(), f"The candidate APK must verify with signature scheme v{version}")

    identity = re.findall(
        r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'",
        badging,
        flags=re.MULTILINE,
    )
    require(len(identity) == 1 and identity[0][0] == APPLICATION_ID,
            "Expected badging for exactly one com.fawnsee.mobile application")
    app_id, code, name = identity[0]
    minimum = re.findall(r"^(?:minSdkVersion|sdkVersion):'([0-9]+)'\s*$", badging, flags=re.MULTILINE)
    require(len(minimum) == 1 and 1 <= int(minimum[0]) <= 1000,
            "Expected the candidate APK's actual minimum SDK from badging")
    return {
        "passed": True,
        "checks": {
            **dict.fromkeys(BUILD_CHECKS, True),
            "matching_existing_release_signer": True,
            "fresh_v2_and_v3_release_signature": True,
            "16kb_zip_alignment": True,
        },
        "apk": {
            "identity": {"application_id": app_id, "version_code": int(code), "version_name": name},
            "bytes": size,
            "sha256": checksum,
            "min_sdk": int(minimum[0]),
            "signer_certificate_sha256": candidate_fingerprint,
        },
        "inspected_utc": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = ArgumentParser(description=__doc__)
    for name in (
        "apk", "build-report", "candidate-signature", "previous-signature", "candidate-badging", "output",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    audit = create_audit(
        args.apk,
        json.loads(args.build_report.read_text(encoding="utf-8-sig")),
        args.candidate_signature.read_text(encoding="utf-8-sig"),
        args.previous_signature.read_text(encoding="utf-8-sig"),
        args.candidate_badging.read_text(encoding="utf-8-sig"),
    )
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(audit, output, ensure_ascii=False, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()

