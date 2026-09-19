"""Synthetic source trees only; no production profile, network, Docker or deployment."""

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.aliyun_platform import build_public_bundle as bundle


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "source"
    names = {
        bundle.ALLOWLISTS[0]: "**\n!server/\nserver/**\n!server/__init__.py\n!server/main.py\n",
        bundle.ALLOWLISTS[1]: "**\n!server/\n!server/helper.py\n",
        "server/__init__.py": "",
        "server/main.py": "from . import helper\nVALUE = helper.VALUE\n",
        "server/helper.py": "VALUE = 7\n",
    }
    for name, content in names.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(bundle, "EXTRA", bundle.ALLOWLISTS)
    monkeypatch.setattr(bundle, "COMPATIBILITY_SOURCES", {})
    monkeypatch.setattr(bundle, "OUTPUT_DRIVE", tmp_path.drive)
    monkeypatch.setattr(bundle, "SOURCE_ROOT", root)
    return root, tmp_path / "public-output"


def test_validation_default_creates_nothing_and_cli_reports_hash(tree, capsys):
    root, output = tree
    assert bundle.main(["--output-dir", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "validated_only" and result["created"] is False
    assert len(result["source_sha256"]) == 64
    assert result["source_file_count"] == 5
    assert not output.exists()
    assert bundle.build_bundle(root, output) == result


def test_create_is_deterministic_verified_and_contains_exact_literal_members(tree):
    root, output = tree
    reviewed = bundle.build_bundle(root, output)
    result = bundle.build_bundle(root, output, create=True,
                                 expected_source_sha256=reviewed["source_sha256"])
    assert result["status"] == "created_and_verified"
    assert sorted(p.name for p in output.iterdir()) == [
        "source-v170-manifest.json", "source-v170.sha256", "source-v170.tar.gz"]
    manifest = json.loads((output / "source-v170-manifest.json").read_text("utf-8"))
    archive_path = output / "source-v170.tar.gz"
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == manifest["archive_sha256"]
    assert manifest["source_sha256"] == reviewed["source_sha256"]
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == [row["path"] for row in manifest["files"]]
        for member in archive.getmembers():
            assert member.isfile() and member.uid == member.gid == member.mtime == 0
            assert member.mode == 0o644
            assert archive.extractfile(member).read() == (root / member.name).read_bytes()
    for line in (output / "source-v170.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    second = output.with_name("second-public-output")
    bundle.build_bundle(root, second, create=True)
    assert (second / archive_path.name).read_bytes() == archive_path.read_bytes()
    assert all(path.stat().st_nlink == 1 for path in output.iterdir())


def test_existing_or_in_repo_destination_refused_without_overwrite(tree):
    root, output = tree
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("original")
    with pytest.raises(bundle.BundleError):
        bundle.build_bundle(root, output, create=True)
    assert marker.read_text() == "original"
    with pytest.raises(bundle.BundleError):
        bundle.build_bundle(root, root / "out", create=True)
    with pytest.raises(bundle.BundleError):
        bundle.build_bundle(root, Path("relative-output"), create=True)


def test_d_drive_is_mandatory_not_an_optional_cli_override(tree, monkeypatch):
    root, output = tree
    monkeypatch.setattr(bundle, "OUTPUT_DRIVE", "D:" if output.drive != "D:" else "C:")
    with pytest.raises(bundle.BundleError, match="D-drive"):
        bundle.build_bundle(root, output)


@pytest.mark.parametrize("name", [
    "../outside.py", "server//helper.py", "C:/outside.py", "server\\helper.py",
    ".local/profile.txt", ".venv-server/code.py", "secrets/password.txt", "server/key.pem",
    "server/history.db", "server/private.json", "data/account.py", "tests/fixture.py",
    "server/*.py", "server/[a].py",
])
def test_unreviewed_paths_and_private_formats_are_never_read(tree, name):
    root, _ = tree
    with pytest.raises(bundle.BundleError):
        bundle.safe_source(root, name)


@pytest.mark.parametrize("rules", ["!server/main.py\n", "**\n!server/**\n", "**\n!server/*/\n"])
def test_context_requires_deny_all_and_literal_permissions(tree, rules):
    root, output = tree
    (root / bundle.ALLOWLISTS[0]).write_text(rules)
    with pytest.raises(bundle.BundleError):
        bundle.build_bundle(root, output)
    assert not output.exists()


def test_missing_dependency_is_reported_not_recursively_added(tree):
    root, output = tree
    (root / bundle.ALLOWLISTS[1]).write_text("**\n!server/\n")
    result = bundle.build_bundle(root, output, create=True)
    assert result["status"] == "missing_explicit_dependencies"
    assert result["missing"] == [{"source": "server/main.py", "required": "server/helper.py"}]
    assert not output.exists()


def test_direct_module_import_includes_package_initializer_in_closure(tree):
    root, output = tree
    (root / "server/main.py").write_text("import server.helper\n")
    (root / bundle.ALLOWLISTS[0]).write_text("**\n!server/main.py\n")
    result = bundle.build_bundle(root, output)
    assert {item["required"] for item in result["missing"]} == {"server/__init__.py"}


def test_nonexistent_explicit_local_module_is_reported_instead_of_silently_skipped(tree):
    root, output = tree
    (root / "server/main.py").write_text("import server.missing\n")
    result = bundle.build_bundle(root, output)
    assert result["missing"] == [{"source": "server/main.py", "required": "server/missing.py"}]
    assert not output.exists()


@pytest.mark.parametrize("secret", [
    "sk-" + "a" * 24,
    "-----BEGIN PRIVATE KEY-----",
    "postgresql://synthetic:privatevalue@database.invalid/db",
    "sb_secret_" + "a" * 30,
    "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
])
def test_potential_secrets_block_output_and_never_appear_in_diagnostics(tree, secret, capsys):
    root, output = tree
    (root / "server/helper.py").write_text("VALUE = " + repr(secret) + "\n")
    assert bundle.main(["--output-dir", str(output), "--create"]) == 2
    diagnostic = capsys.readouterr().out
    assert "Potential secret" in diagnostic and secret not in diagnostic
    assert not output.exists()


def test_hardlinked_source_refused(tree):
    root, output = tree
    os.link(root / "server/helper.py", output.with_name("other-name.py"))
    with pytest.raises(bundle.BundleError, match="Invalid public"):
        bundle.build_bundle(root, output)


def test_symlink_source_refused_before_any_read(tree):
    root, output = tree
    original = root / "server/helper.py"
    original.rename(root / "server/real.py")
    try:
        original.symlink_to(root / "server/real.py")
    except OSError:
        pytest.skip("This Windows test account cannot create symlinks")
    with pytest.raises(bundle.BundleError, match="Links"):
        bundle.build_bundle(root, output)


def test_windows_reparse_source_is_rejected_without_needing_symlink_privileges(tree, monkeypatch):
    root, output = tree
    original = Path.lstat

    def reparse(path):
        metadata = original(path)
        if path == root / "server/helper.py":
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(bundle.BundleError, match="reparse"):
        bundle.build_bundle(root, output)
    assert not output.exists()


def test_changed_reviewed_source_hash_blocks_before_creation(tree):
    root, output = tree
    reviewed = bundle.build_bundle(root, output)
    (root / "server/helper.py").write_text("VALUE = 8\n")
    with pytest.raises(bundle.BundleError, match="reviewed SHA256"):
        bundle.build_bundle(root, output, create=True,
                            expected_source_sha256=reviewed["source_sha256"])
    assert not output.exists()


def test_source_change_during_archive_build_blocks_before_creation(tree, monkeypatch):
    root, output = tree
    original = bundle._archive

    def changed(blobs):
        result = original(blobs)
        (root / "server/helper.py").write_text("VALUE = 9\n")
        return result

    monkeypatch.setattr(bundle, "_archive", changed)
    with pytest.raises(bundle.BundleError, match="Source changed"):
        bundle.build_bundle(root, output, create=True)
    assert not output.exists()


def test_source_change_after_disk_write_never_promotes_pending_artifacts(tree, monkeypatch):
    root, output = tree
    original = bundle.verify_archive
    calls = []

    def changed(raw, blobs):
        original(raw, blobs)
        calls.append(True)
        if len(calls) == 2:
            (root / "server/helper.py").write_text("VALUE = 10\n")

    monkeypatch.setattr(bundle, "verify_archive", changed)
    with pytest.raises(bundle.BundleError, match="Source changed"):
        bundle.build_bundle(root, output, create=True)
    assert output.exists()
    assert all(path.name.endswith(".pending") for path in output.iterdir())
    assert not (output / "source-v170-manifest.json").exists()


def test_archive_members_and_content_are_independently_checked():
    blobs = {"server/main.py": b"VALUE=1\n"}
    with pytest.raises(bundle.BundleError, match="readback"):
        bundle.verify_archive(bundle._archive(blobs), {"server/main.py": b"VALUE=2\n"})
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        link = tarfile.TarInfo("server/main.py")
        link.type, link.linkname = tarfile.SYMTYPE, "../../outside"
        archive.addfile(link)
    with pytest.raises(bundle.BundleError, match="membership"):
        bundle.verify_archive(raw.getvalue(), blobs)
