"""Offline fixtures only: archive containers here are not runnable applications."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter

PROJECT = Path(__file__).resolve().parents[1]


def load_script(name):
    specification = importlib.util.spec_from_file_location(
        "synthetic_packaging_" + name, PROJECT / "packaging" / (name + ".py"),
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


embedded = load_script("verify_embedded_source")
build = load_script("verify_build")
release = load_script("verify_release")
assemble = load_script("assemble_release")


def synthetic_pyz(path, *, missing=(), mismatch=None):
    entries, codes = [], {}
    for name in sorted(embedded.REQUIRED_APP_MODULES - set(missing)):
        module_path = PROJECT.joinpath("src", *name.split("."))
        source = (module_path / "__init__.py" if module_path.is_dir()
                  else module_path.with_suffix(".py"))
        body = source.read_bytes() if name != mismatch else b"SYNTHETIC_STALE_BUILD = True\n"
        codes[name] = compile(body, str(source), "exec", dont_inherit=True, optimize=0)
        entries.append((name, str(source), "PYMODULE"))
    ZlibArchiveWriter(str(path), entries, codes)
    return path


def synthetic_artifact(tmp_path, kind, **kwargs):
    pyz = synthetic_pyz(tmp_path / "synthetic.pyz", **kwargs)
    if kind == "pyz":
        return pyz
    container = tmp_path / "synthetic-not-runnable.exe"
    CArchiveWriter(str(container), [("PYZ.pyz", str(pyz), False, "z")], "python313.dll")
    return container


@pytest.mark.parametrize("kind", ["pyz", "exe"])
def test_required_cloud_and_entry_modules_are_read_from_real_archive_formats(tmp_path, kind):
    artifact = synthetic_artifact(tmp_path, kind)
    result = embedded.verify(artifact)
    assert result["status"] == "passed", result
    assert result["kind"] == ("exe_embedded_pyz" if kind == "exe" else "standalone_pyz")
    assert result["module_count"] == len(embedded.REQUIRED_APP_MODULES)
    assert result["missing_modules"] == result["mismatches"] == result["errors"] == []
    assert set(result["checked_modules"]) == embedded.REQUIRED_APP_MODULES
    assert result["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


@pytest.mark.parametrize("kind", ["pyz", "exe"])
@pytest.mark.parametrize("missing", sorted(embedded.REQUIRED_APP_MODULES))
def test_missing_any_required_module_must_fail_even_if_remaining_bytecode_matches(
    tmp_path, kind, missing,
):
    artifact = synthetic_artifact(tmp_path, kind, missing=[missing])
    result = embedded.verify(artifact)
    assert result["status"] == "failed"
    assert result["missing_modules"] == [missing]
    assert result["mismatches"] == []
    assert result["module_count"] > 0  # The old bool(checked) condition would pass.


@pytest.mark.parametrize("kind", ["pyz", "exe"])
def test_stale_public_endpoint_module_fails_source_parity(tmp_path, kind):
    name = "ollama_chat_app.cloud_config"
    artifact = synthetic_artifact(tmp_path, kind, mismatch=name)
    result = embedded.verify(artifact)
    assert result["status"] == "failed"
    assert result["missing_modules"] == []
    assert result["mismatches"] == [name]


@pytest.mark.parametrize("count", [0, 2])
def test_final_container_requires_exactly_one_embedded_pyz(tmp_path, count):
    pyz = synthetic_pyz(tmp_path / "synthetic.pyz")
    container = tmp_path / "synthetic-not-runnable.exe"
    entries = [(f"PYZ-{index}.pyz", str(pyz), False, "z") for index in range(count)]
    CArchiveWriter(str(container), entries, "python313.dll")
    result = embedded.verify(container)
    assert result["status"] == "failed"
    assert result["errors"] == ["archive_unreadable_or_ambiguous"]


def test_external_good_pyz_cannot_make_incomplete_final_container_pass(tmp_path):
    missing = "ollama_chat_app.workers.cloud_bridge"
    container = synthetic_artifact(tmp_path, "exe", missing=[missing])
    # Replace the adjacent development archive with a complete one. EXE mode
    # must still read its own embedded bytes, not discover this file by name.
    pyz = synthetic_pyz(tmp_path / "synthetic.pyz")
    assert embedded.verify(pyz)["status"] == "passed"
    result = embedded.verify(container)
    assert result["status"] == "failed" and result["missing_modules"] == [missing]


def test_unreadable_archive_returns_a_failure_without_running_anything(tmp_path):
    invalid = tmp_path / "not-an-executable.exe"
    invalid.write_bytes(b"offline invalid archive fixture")
    assert embedded.verify(invalid)["status"] == "failed"


def test_mutation_during_archive_validation_is_rejected(tmp_path, monkeypatch):
    artifact = synthetic_artifact(tmp_path, "pyz")
    digests = iter(["before", "after"])
    monkeypatch.setattr(embedded, "_sha256", lambda _path: next(digests))
    result = embedded.verify(artifact)
    assert result["status"] == "failed"
    assert result["errors"] == ["artifact_changed_during_verification"]


def test_cli_accepts_final_exe_and_missing_module_exits_nonzero(tmp_path, capsys):
    artifact = synthetic_artifact(tmp_path, "exe", missing=["ollama_chat_app.cloud_config"])
    assert embedded.main([str(artifact)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "exe_embedded_pyz"
    assert result["missing_modules"] == ["ollama_chat_app.cloud_config"]


def test_analysis_verifier_requires_the_same_five_cloud_modules(tmp_path):
    toc = [()] * 20
    toc_path = tmp_path / "Analysis-00.toc"
    dist = tmp_path / "synthetic-empty-dist"
    dist.mkdir()
    toc_path.write_text(repr(tuple(toc)), encoding="utf-8")
    result = build.verify(toc_path, dist)
    error = next(item for item in result["errors"] if "缺少必要 Python 模块" in item)
    assert all(name in error for name in embedded.REQUIRED_CLOUD_MODULES)
    toc[14] = [(name, "synthetic.py", "PYMODULE") for name in embedded.REQUIRED_CLOUD_MODULES]
    toc_path.write_text(repr(tuple(toc)), encoding="utf-8")
    result = build.verify(toc_path, dist)
    error = next(item for item in result["errors"] if "缺少必要 Python 模块" in item)
    assert not any(name in error for name in embedded.REQUIRED_CLOUD_MODULES)


def test_release_instructions_and_verifier_agree_about_local_cloud_boundaries(tmp_path):
    source, destination = tmp_path / "synthetic-source", tmp_path / "synthetic-release"
    source.mkdir()
    (source / "_internal").mkdir()
    # The release verifier checks manifest/empty-DB semantics. Real PE and PYZ
    # validation are separate gates, not claimed by this deliberately inert file.
    (source / assemble.EXECUTABLE_NAME).write_bytes(b"synthetic non-executable fixture")
    assemble.assemble(source, destination)
    result = release.verify_directory(destination)
    assert result["database"]["users"] == result["database"]["life_records"] == 0
    instructions = (destination / "使用说明.txt").read_text(encoding="utf-8-sig")
    assert "没有开启 Supabase/Render 云端同步" not in instructions
    assert "云端能力需先部署并验证可用的服务" in instructions
    assert "程序启动默认为本地模式" in instructions
    assert "本地和云端不会自动合并" in instructions
    assert "两种模式的账号相互独立" in instructions
    assert "云端登录令牌只存在内存" in instructions


def test_build_security_document_has_current_version_and_no_result_claim():
    instructions = (PROJECT / "packaging" / "BUILD_SECURITY.md").read_text(encoding="utf-8")
    assert "1.2.0" in instructions and "1.1.0" not in instructions
    assert "不是本次成品已构建、已扫描或已通过公网验收的证明" in instructions
    assert "最终 EXE" in instructions and "缺模块" in instructions
