"""All databases are freshly generated fiction; executable fixtures are inert archives."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(
        "synthetic_demo_" + name, ROOT / "packaging" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assemble = script("assemble_demo_release")
verify = script("verify_demo_release")
common = script("_demo_release")
clean_verify = script("verify_release")


@pytest.fixture(scope="module")
def master_demo(tmp_path_factory):
    from tools.generate_synthetic_demo import build_demo

    target = tmp_path_factory.mktemp("synthetic-release-seed") / "demo"
    with patch.dict(os.environ, {}):
        build_demo(target, screenshots=False)
    return target


@pytest.fixture
def demo(tmp_path, master_demo):
    target = tmp_path / "synthetic-demo"
    shutil.copytree(master_demo, target)
    return target


@pytest.fixture
def built_source(tmp_path):
    source = tmp_path / "inert-build"
    source.mkdir()
    (source / "_internal").mkdir()
    (source / "_internal/fixture.txt").write_text("Inert fixture, not an application runtime")
    embedded = script("verify_embedded_source")
    names = embedded.REQUIRED_APP_MODULES | common.REQUIRED_MODULES
    entries, codes = [], {}
    for name in sorted(names):
        path = ROOT.joinpath("src", *name.split("."))
        path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        codes[name] = compile(path.read_bytes(), str(path), "exec", dont_inherit=True, optimize=0)
        entries.append((name, str(path), "PYMODULE"))
    pyz = tmp_path / "synthetic.pyz"
    ZlibArchiveWriter(str(pyz), entries, codes)
    CArchiveWriter(
        str(source / assemble.clean.EXECUTABLE_NAME),
        [("PYZ.pyz", str(pyz), False, "z")],
        "python313.dll",
    )
    return source


def mutate_db(demo, command, values=()):
    with sqlite3.connect(demo / "data/app.db") as connection:
        connection.execute(command, values)


def test_only_fixed_fictional_schema6_payload_is_accepted(demo):
    result = common.verify_payload(demo)
    assert result["schema_version"] == 6
    assert result["counts"] == common.COUNTS
    assert result["credentials_verified_without_display"] is True


def test_same_archive_build_produces_distinct_clean_and_demo_packages(built_source, demo, tmp_path):
    clean, target = tmp_path / "clean", tmp_path / "demo-release"
    clean_zip, demo_zip = tmp_path / "clean.zip", tmp_path / "demo.zip"
    result = assemble.assemble(
        built_source, demo, target, clean_destination=clean, demo_zip=demo_zip, clean_zip=clean_zip
    )
    assert result["clean"]["database"]["users"] == 0
    assert result["demo"]["database"]["counts"]["users"] == 5
    assert result["clean"]["executable_sha256"] == result["demo"]["executable_sha256"]
    assert result["demo_zip"]["contents"]["database"]["counts"]["member_cart"] == 4
    assert result["demo_zip"]["contents"]["database"]["counts"]["staff_account_terms"] == 2
    assert result["clean_zip"]["contents"]["database"]["users"] == 0
    with pytest.raises(RuntimeError):
        clean_verify.verify_directory(target)
    # The strict existing empty-database gate remains unchanged and rejects demo data.
    with pytest.raises(RuntimeError, match="空数据库"):
        clean_verify._verify_database(target / "data/app.db")
    assert (target / "DEMO_ACCOUNTS.md").read_bytes() == (demo / "DEMO_ACCOUNTS.md").read_bytes()
    assert not (clean / "DEMO_ACCOUNTS.md").exists()
    instructions = (target / "使用说明.txt").read_text(encoding="utf-8-sig")
    assert "使用演示账号前，请主动选择“本地模式”并应用，再登录。" in instructions
    assert "默认选择本地模式" not in instructions


def test_old_or_incomplete_executable_is_refused_before_output(built_source, demo, tmp_path):
    (built_source / assemble.clean.EXECUTABLE_NAME).write_bytes(b"inert stale executable fixture")
    target = tmp_path / "refused"
    with pytest.raises(RuntimeError, match="拒绝旧 EXE"):
        assemble.assemble(built_source, demo, target)
    assert not target.exists()


def test_non_synthetic_manifest_is_refused_before_output(built_source, demo, tmp_path):
    path = demo / "demo_manifest.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    content["synthetic_demo"] = False
    path.write_text(json.dumps(content))
    target = tmp_path / "refused"
    with pytest.raises(RuntimeError, match="demo_manifest"):
        assemble.assemble(built_source, demo, target)
    assert not target.exists()


@pytest.mark.parametrize(
    "command,values",
    [
        (
            "UPDATE users SET username=?,username_normalized=? WHERE username=?",
            ("real-account", "real-account", "demo-member-1"),
        ),
        ("PRAGMA user_version=5", ()),
        ("DELETE FROM member_cart WHERE rowid=(SELECT min(rowid) FROM member_cart)", ()),
        ("UPDATE member_profiles SET phone=?", ("13800000000",)),
        (
            "UPDATE life_records SET content=? WHERE id=(SELECT min(id) FROM life_records)",
            ("sk-" + "x" * 40,),
        ),
        (
            "UPDATE user_preferences SET preferences_json=?",
            ('{"api_key":"synthetic-secret-test"}',),
        ),
    ],
)
def test_rejects_account_schema_count_personal_contact_and_key_mutations(demo, command, values):
    mutate_db(demo, command, values)
    with pytest.raises(RuntimeError):
        common.verify_payload(demo)


def test_no_dpapi_or_extra_database_can_enter_payload(demo):
    (demo / "data/private.dpapi").write_bytes(b"SYNTHETIC NOT DPAPI")
    with pytest.raises(RuntimeError, match="凭据"):
        common.verify_payload(demo)


def test_manifest_hash_tampering_and_exe_mismatch_are_rejected(built_source, demo, tmp_path):
    target = tmp_path / "demo-release"
    result = assemble.assemble(built_source, demo, target)
    with pytest.raises(RuntimeError, match="同轮纯净版"):
        verify.verify_directory(target, expected_executable_sha256="0" * 64)
    (target / "assets/products/demo-1.png").write_bytes(b"SYNTHETIC TAMPER")
    with pytest.raises(RuntimeError, match="哈希不匹配"):
        verify.verify_directory(target, expected_executable_sha256=result["same_executable_sha256"])


def test_source_destination_overlap_or_existing_output_is_refused(built_source, demo, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(RuntimeError, match="已存在"):
        assemble.assemble(built_source, demo, existing)
    with pytest.raises(RuntimeError, match="互相包含"):
        assemble.assemble(built_source, demo, demo / "new")


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        common.APP_NAME + "/data/a:b",
        common.APP_NAME + "/data/a.",
        common.APP_NAME + "/data/CON",
    ],
)
def test_zip_unsafe_paths_are_rejected_before_extraction(tmp_path, name):
    archive = tmp_path / "synthetic-bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, "SYNTHETIC")
    with pytest.raises(RuntimeError):
        verify.verify_archive(archive)


def test_larger_total_archive_budget_keeps_per_member_limit():
    assert clean_verify.MAX_ARCHIVE_BYTES == 2 * 1024 * 1024 * 1024
    assert clean_verify.MAX_MEMBER_BYTES == 512 * 1024 * 1024
    info = zipfile.ZipInfo(common.APP_NAME + "/_internal/synthetic-large.dll")
    info.file_size = clean_verify.MAX_MEMBER_BYTES + 1
    with pytest.raises(RuntimeError, match="成员过大"):
        clean_verify._validate_member(info, common.APP_NAME)
