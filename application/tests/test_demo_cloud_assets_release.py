"""Only fixed fictional product illustrations; inert EXE and temporary releases."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy

import pytest

from tests import test_demo_release as fixtures

master_demo = fixtures.master_demo
demo = fixtures.demo
built_source = fixtures.built_source
assemble, verify = fixtures.assemble, fixtures.verify
assets = fixtures.script("_demo_cloud_assets")


def mapping(namespace="a" * 32):
    return [
        {
            "source_path": name,
            "sha256": checksum,
            "bytes": size,
            "destination": f"transfer-assets/{namespace}/{checksum}.png",
        }
        for name, (checksum, size) in assets.APPROVED_IMAGES.items()
    ]


def write_plan(path, value=None):
    path.write_text(json.dumps(value if value is not None else mapping()), encoding="utf-8")
    return path


@pytest.fixture
def original_images(demo):
    # Image bytes are the only real artifact read: fixed synthetic illustrations,
    # never a source database or DEMO_ACCOUNTS plaintext credentials.
    source = fixtures.ROOT / "outputs" / "synthetic-demo-20260909"
    if not source.exists():
        pytest.skip("the separately verified frozen six-picture artifact is required")
    assets.validate_originals(source, mapping())
    for name in assets.APPROVED_IMAGES:
        shutil.copyfile(source / name, demo / name)
    return demo


def test_normalizes_only_six_verified_mappings_with_five_unique_aliases(tmp_path):
    path = write_plan(tmp_path / "review.json", list(reversed(mapping())))
    assert assets.load(path) == mapping()
    assert assets.metadata(mapping())["mapping_count"] == 6
    assert assets.metadata(mapping())["file_count"] == 5


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.pop(),
        lambda p: p.append(deepcopy(p[0])),
        lambda p: p[0].update(source_path="data/app.db"),
        lambda p: p[0].update(source_path="../assets/products/demo-1.png"),
        lambda p: p[0].update(source_path="C:/private.png"),
        lambda p: p[0].update(source_path=p[1]["source_path"]),
        lambda p: p[0].update(sha256="0" * 64),
        lambda p: p[0].update(bytes=True),
        lambda p: p[0].update(bytes=p[0]["bytes"] + 1),
        lambda p: p[0].update(api_key="SYNTHETIC-NEVER-ACCEPT"),
        lambda p: p[0].update(destination="../escape.png"),
        lambda p: p[0].update(destination="/absolute.png"),
        lambda p: p[0].update(destination="C:/absolute.png"),
        lambda p: p[0].update(destination="https://untrusted.invalid/picture.png"),
        lambda p: p[0].update(destination=p[0]["destination"].replace("/", "\\")),
        lambda p: p[0].update(destination=p[0]["destination"].replace("a" * 32, "b" * 32)),
        lambda p: p[0].update(destination=p[0]["destination"].upper()),
        lambda p: p[0].update(destination=p[0]["destination"] + ":payload"),
        lambda p: p[0].update(destination=p[1]["destination"]),
        lambda p: p[0].update(destination=p[0]["destination"].replace(".png", ".exe")),
        lambda p: p[0].update(destination=p[0]["destination"].replace(".png", ".png/child")),
    ],
)
def test_rejects_count_paths_namespaces_other_files_hashes_and_secrets(tmp_path, mutation):
    value = mapping()
    mutation(value)
    with pytest.raises(RuntimeError):
        assets.load(write_plan(tmp_path / "rejected.json", value))


def test_rejects_full_plan_duplicate_json_and_oversized_inputs(tmp_path):
    path = tmp_path / "rejected.json"
    for raw in (
        json.dumps({"external_assets": mapping()}),
        json.dumps(mapping()).replace(
            '"source_path":', '"source_path":"duplicate", "source_path":', 1
        ),
        " " * (assets.MAX_PLAN_BYTES + 1),
    ):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(RuntimeError):
            assets.load(path)


def test_checked_aliases_are_extra_files_not_source_db_or_exe_changes(
    built_source, original_images, tmp_path
):
    before = {
        name: assemble.clean._sha256(original_images / name)
        for name in fixtures.common.PAYLOAD_FILES
    }
    plan = write_plan(tmp_path / "review.json")
    target, archive = tmp_path / "new-release", tmp_path / "new-release.zip"
    result = assemble.assemble(
        built_source, original_images, target, demo_zip=archive, cloud_assets_plan=plan
    )
    assert result["demo"]["cloud_assets"] == assets.metadata(mapping())
    assert result["demo_zip"]["contents"]["cloud_assets"]["file_count"] == 5
    assert (
        {name: assemble.clean._sha256(target / name) for name in before}
        == before
        == {name: assemble.clean._sha256(original_images / name) for name in before}
    )
    assert assemble.clean._sha256(target / assemble.clean.EXECUTABLE_NAME) == (
        assemble.clean._sha256(built_source / assemble.clean.EXECUTABLE_NAME)
    )
    assert "不是动态云商品图片下载功能" in (target / "使用说明.txt").read_text(encoding="utf-8-sig")
    manifest = (target / "SHA256SUMS.txt").read_text()
    assert all(item["destination"] in manifest for item in mapping())
    assert "cloud-assets-plan.json" in manifest
    with pytest.raises(RuntimeError, match="精确白名单"):
        verify.verify_directory(target)
    with pytest.raises(RuntimeError):
        verify.verify_archive(archive)
    with pytest.raises(RuntimeError, match="包内计划自证"):
        verify.verify_directory(target, cloud_assets_plan=target / assets.MAPPING_FILE)
    with pytest.raises(RuntimeError):
        fixtures.clean_verify.verify_directory(target)
    with pytest.raises(RuntimeError, match="空数据库"):
        fixtures.clean_verify._verify_database(target / "data/app.db")
    with pytest.raises(RuntimeError):
        verify.verify_directory(
            target, cloud_assets_plan=write_plan(tmp_path / "other.json", mapping("b" * 32))
        )
    with pytest.raises(RuntimeError, match="已存在"):
        assets.stage(target, mapping())


@pytest.mark.parametrize(
    "tamper", ["extra_png", "extra_namespace", "empty_dir", "changed_png", "hardlink"]
)
def test_rehashing_manifest_cannot_whitelist_extra_or_tampered_aliases(
    built_source, original_images, tmp_path, tamper
):
    import os

    plan = write_plan(tmp_path / "review.json")
    target = tmp_path / "new-release"
    assemble.assemble(built_source, original_images, target, cloud_assets_plan=plan)
    namespace = target / "transfer-assets" / ("a" * 32)
    if tamper == "extra_png":
        (namespace / "unapproved.png").write_bytes(b"SYNTHETIC NOT AN IMAGE")
    elif tamper == "extra_namespace":
        (target / "transfer-assets" / ("b" * 32)).mkdir()
    elif tamper == "empty_dir":
        (namespace / "unapproved").mkdir()
    elif tamper == "hardlink":
        os.link(target / mapping()[0]["destination"], tmp_path / "external-alias.png")
    else:
        (target / mapping()[0]["destination"]).write_bytes(b"SYNTHETIC TAMPER")
    assemble._hash_manifest(target)
    with pytest.raises(RuntimeError):
        verify.verify_directory(target, cloud_assets_plan=plan)


def test_bad_plan_or_changed_original_refused_before_output(
    built_source, original_images, tmp_path
):
    plan = write_plan(tmp_path / "review.json")
    # A valid but different approved PNG passes the generic image format check;
    # the alias-specific fixed source/hash contract must still reject it.
    shutil.copyfile(
        original_images / mapping()[1]["source_path"],
        original_images / mapping()[0]["source_path"],
    )
    target = tmp_path / "not-created"
    with pytest.raises(RuntimeError):
        assemble.assemble(built_source, original_images, target, cloud_assets_plan=plan)
    assert not target.exists()


def test_plan_hardlink_is_rejected(tmp_path):
    import os

    source = write_plan(tmp_path / "review.json")
    duplicate = tmp_path / "hardlink.json"
    os.link(source, duplicate)
    with pytest.raises(RuntimeError):
        assets.load(duplicate)


def test_assembly_refuses_source_pack_self_plan_before_output(
    built_source, original_images, tmp_path
):
    inside = write_plan(original_images / "self-plan.json")
    target = tmp_path / "not-created"
    with pytest.raises(RuntimeError, match="包内计划自证"):
        assemble.assemble(built_source, original_images, target, cloud_assets_plan=inside)
    assert not target.exists()
