import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from prepare_release_audit import BUILD_CHECKS, create_audit, verified_signer


FINGERPRINT = "a" * 64
SIGNATURE = "\n".join((
    "Verifies",
    "Verified using v2 scheme (APK Signature Scheme v2): true",
    "Verified using v3 scheme (APK Signature Scheme v3): true",
    f"Signer #1 certificate SHA-256 digest: {FINGERPRINT}",
))
BADGING = "package: name='com.fawnsee.mobile' versionCode='11864' versionName='1.6.13-try1.2'\nsdkVersion:'24'\n"


class PrepareAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = b"synthetic APK bytes"
        self.apk = Path(self.temp.name) / "candidate.apk"
        self.apk.write_bytes(data)
        self.report = {
            **dict.fromkeys(BUILD_CHECKS, True),
            "passed": True,
            "signature": {"passed": True},
            "zip_alignment_16kb": {"passed": True},
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def test_release_report_has_android_identity(self):
        result = create_audit(self.apk, self.report, SIGNATURE, SIGNATURE, BADGING)
        self.assertTrue(result["passed"])
        self.assertEqual(result["apk"]["min_sdk"], 24)
        self.assertEqual(result["apk"]["identity"]["version_code"], 11864)
        self.assertEqual(result["apk"]["signer_certificate_sha256"], FINGERPRINT)

    def test_change_to_signature_is_refused(self):
        with self.assertRaisesRegex(ValueError, "differs from the previous release"):
            create_audit(self.apk, self.report, SIGNATURE, SIGNATURE.replace(FINGERPRINT, "b" * 64), BADGING)

    def test_change_to_apk_bytes_is_refused(self):
        self.apk.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "bytes no longer match"):
            create_audit(self.apk, self.report, SIGNATURE, SIGNATURE, BADGING)

    def test_multiple_signers_are_refused(self):
        with self.assertRaisesRegex(ValueError, "exactly one signer"):
            verified_signer(SIGNATURE + f"\nSigner #2 certificate SHA-256 digest: {'b' * 64}")

