"""Private-refresh acceptance uses fictional databases and inert executable fixtures only."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from tests import test_demo_release as release_fixtures
from tools import refresh_private_desktop as upgrade


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def create_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            "PRAGMA user_version=7; CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT);"
            "INSERT INTO notes VALUES (1, 'fictional original record');"
        )
        connection.commit()


@pytest.fixture
def folders(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    private = desktop / "私有应用"
    private.mkdir()
    (private / "_internal").mkdir()
    (private / "_internal/obsolete.dll").write_bytes(b"old runtime must not survive")
    (private / upgrade.EXECUTABLE).write_bytes(b"inert old exe")
    (private / upgrade.PRIVATE_README).write_text("本机私有资料，不可公开。", encoding="utf-8")
    (private / "使用说明.txt").write_text("保留使用者自己的说明", encoding="utf-8")
    (private / "PUBLIC_PACKAGE_SHA256SUMS.txt").write_text("old provenance only")
    (private / "assets").mkdir()
    (private / "assets/owner-photo.bin").write_bytes(b"fictional photo")
    create_database(private / "data/app.db")
    write_json(private / "portable.json", {
        "application": upgrade.APP_NAME, "version": "1.7.0",
        "database_state": "private_owner_copy", "database_included": True,
        "synthetic": False, "database_schema_version": 7,
        "executable_sha256": "old fictional exe hash",
        "contains_private_developer_authorization": True,
        "owner_custom_setting": "preserve this metadata",
    })
    runtime = tmp_path / "public-runtime"
    runtime.mkdir()
    (runtime / "_internal").mkdir()
    (runtime / "_internal/current.dll").write_bytes(b"inert updated runtime")
    (runtime / upgrade.EXECUTABLE).write_bytes(b"inert new exe")
    write_json(runtime / "portable.json", {
        "version": "1.7.0", "database_schema_version": 7,
        "database_state": "synthetic_demo", "synthetic": True,
    })
    create_database(runtime / "data/app.db")
    (runtime / "data/public-only.bin").write_bytes(b"must not replace private data")
    (runtime / "使用说明.txt").write_text("public instructions must not overwrite private ones")
    (runtime / "SHA256SUMS.txt").write_text("\n".join(
        f"{upgrade._digest(path).upper()}  {path.relative_to(runtime).as_posix()}"
        for path in runtime.rglob("*") if path.is_file()
    ) + "\n", encoding="utf-8")
    monkeypatch.setattr(upgrade, "_verify_public", lambda root, plan: {
        "executable_sha256": upgrade._digest(root / upgrade.EXECUTABLE).upper(),
    })
    return runtime, private, private.with_name(private.name + "-验收更新")


def read_notes(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT text FROM notes ORDER BY id").fetchall()


def test_preserves_private_data_metadata_readme_without_public_seed(folders):
    runtime, private, destination = folders
    before = {str(p.relative_to(private)): upgrade._digest(p)
              for p in private.rglob("*") if p.is_file()}
    result = upgrade.refresh(runtime, private, destination)
    assert result["status"] == "private_copy_created_not_activated"
    assert result["public_distribution_allowed"] is False
    assert result["cloud_connected"] is False
    assert (destination / upgrade.EXECUTABLE).read_bytes() == b"inert new exe"
    assert not (destination / "_internal/obsolete.dll").exists()
    assert (destination / "_internal/current.dll").exists()
    assert not (destination / "data/public-only.bin").exists()
    assert (destination / upgrade.PRIVATE_README).read_bytes() == (
        private / upgrade.PRIVATE_README
    ).read_bytes()
    assert (destination / "使用说明.txt").read_bytes() == (private / "使用说明.txt").read_bytes()
    assert (destination / "assets/owner-photo.bin").read_bytes() == b"fictional photo"
    metadata = json.loads((destination / "portable.json").read_text(encoding="utf-8"))
    assert metadata["owner_custom_setting"] == "preserve this metadata"
    assert metadata["database_state"] == "private_owner_copy"
    assert metadata["synthetic"] is False
    assert metadata["contains_private_developer_authorization"] is True
    assert metadata["private_refresh"]["public_distribution_allowed"] is False
    assert read_notes(destination / "data/app.db") == read_notes(private / "data/app.db")
    assert not (destination / upgrade.INCOMPLETE).exists()
    assert not (destination / upgrade.PENDING_EXE).exists()
    assert before == {str(p.relative_to(private)): upgrade._digest(p)
                      for p in private.rglob("*") if p.is_file()}
    assert not list(destination.parent.glob("*.zip"))


def test_backup_includes_committed_wal_and_preserves_open_writer(folders):
    runtime, private, destination = folders
    with closing(sqlite3.connect(private / "data/app.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO notes VALUES (2, 'fictional new record held in WAL')")
        writer.commit()
        assert (private / "data/app.db-wal").stat().st_size > 0
        upgrade.refresh(runtime, private, destination)
        assert len(read_notes(destination / "data/app.db")) == 2
        assert not (destination / "data/app.db-wal").exists()
        assert not (destination / "data/app.db-shm").exists()
        writer.execute("INSERT INTO notes VALUES (3, 'written after snapshot stays in old folder')")
        writer.commit()
        assert len(read_notes(private / "data/app.db")) == 3
        assert len(read_notes(destination / "data/app.db")) == 2


def test_multiple_sqlite_mirrors_use_backup_not_raw_copy(folders):
    runtime, private, destination = folders
    create_database(private / "data/mirrors/cloud.sqlite3")
    result = upgrade.refresh(runtime, private, destination)
    assert set(result["database_snapshots"]) == {"data/app.db", "data/mirrors/cloud.sqlite3"}
    assert read_notes(destination / "data/mirrors/cloud.sqlite3") == [
        ("fictional original record",)
    ]


@pytest.mark.parametrize("kind", ["existing", "zip", "not-sibling", "wrong-prefix", "relative"])
def test_target_must_be_explicit_new_private_sibling(folders, kind):
    runtime, private, destination = folders
    if kind == "existing":
        destination.mkdir()
        (destination / "preserve.txt").write_text("keep")
    elif kind == "zip":
        destination = destination.with_suffix(".zip")
    elif kind == "not-sibling":
        destination = runtime.parent / destination.name
    elif kind == "wrong-prefix":
        destination = destination.with_name("public-release")
    else:
        destination = Path("relative-private-upgrade")
    with pytest.raises(RuntimeError):
        upgrade.refresh(runtime, private, destination)
    if kind == "existing":
        assert (destination / "preserve.txt").read_text() == "keep"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("marker", ["directory", "worktree-file"])
def test_private_payload_never_enters_git(folders, marker):
    runtime, private, destination = folders
    marker_path = private.parent / ".git"
    if marker == "directory":
        marker_path.mkdir()
    else:
        marker_path.write_text("gitdir: fictional worktree")
    with pytest.raises(RuntimeError, match="Git"):
        upgrade.refresh(runtime, private, destination)
    assert not destination.exists()


@pytest.mark.parametrize("kind", ["public", "wrong-schema", "wrong-version", "missing-readme"])
def test_private_identity_and_schema_are_required(folders, kind):
    runtime, private, destination = folders
    metadata = json.loads((private / "portable.json").read_text(encoding="utf-8"))
    if kind == "public":
        metadata.update(synthetic=True, database_state="synthetic_demo")
    elif kind == "wrong-schema":
        metadata["database_schema_version"] = 8
    elif kind == "wrong-version":
        metadata["version"] = "1.6.0"
    else:
        (private / upgrade.PRIVATE_README).unlink()
    write_json(private / "portable.json", metadata)
    with pytest.raises(RuntimeError):
        upgrade.refresh(runtime, private, destination)
    assert not destination.exists()


def test_hardlinks_are_refused(folders):
    runtime, private, destination = folders
    os.link(private / "assets/owner-photo.bin", private / "assets/hardlink.bin")
    with pytest.raises(RuntimeError, match="硬链接"):
        upgrade.refresh(runtime, private, destination)
    assert not destination.exists()


def test_links_or_junctions_are_refused(folders):
    runtime, private, destination = folders
    link = private / "external-link"
    try:
        link.symlink_to(runtime, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # Windows junctions do not need Developer Mode or symlink privileges.
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"  # noqa: E731
        subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(runtime)}",
        ], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(RuntimeError, match="链接|联接"):
            upgrade.refresh(runtime, private, destination)
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            # Non-recursive removal unlinks only this test-created junction.
            link.rmdir()
    assert not destination.exists()


def test_attachment_mutation_fails_without_runnable_successor(folders, monkeypatch):
    runtime, private, destination = folders
    original = shutil.copy2

    def mutate(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        if Path(source) == private / "assets/owner-photo.bin":
            Path(source).write_bytes(b"owner changed attachment during snapshot")
        return result

    monkeypatch.setattr(shutil, "copy2", mutate)
    with pytest.raises(RuntimeError, match="复制中变化"):
        upgrade.refresh(runtime, private, destination)
    assert (destination / upgrade.INCOMPLETE).exists()
    assert not (destination / upgrade.EXECUTABLE).exists()
    assert (private / upgrade.EXECUTABLE).read_bytes() == b"inert old exe"


def test_committed_concurrent_write_is_detected_not_discarded(folders, monkeypatch):
    runtime, private, destination = folders
    original = upgrade._backup

    def backup_and_write(source, target, deadline):
        result = original(source, target, deadline)
        with closing(sqlite3.connect(private / "data/app.db")) as writer:
            writer.execute("INSERT INTO notes VALUES (2, 'concurrent owner record')")
            writer.commit()
        return result

    with closing(sqlite3.connect(private / "data/app.db")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        monkeypatch.setattr(upgrade, "_backup", backup_and_write)
        with pytest.raises(RuntimeError, match="旧应用"):
            upgrade.refresh(runtime, private, destination)
        assert len(read_notes(private / "data/app.db")) == 2
        assert (destination / upgrade.INCOMPLETE).exists()
        assert not (destination / upgrade.EXECUTABLE).exists()


def test_corrupt_database_is_refused_without_source_change(folders):
    runtime, private, destination = folders
    (private / "data/app.db").write_bytes(b"not sqlite, preserve for manual recovery")
    digest = upgrade._digest(private / "data/app.db")
    with pytest.raises(RuntimeError, match="格式"):
        upgrade.refresh(runtime, private, destination)
    assert upgrade._digest(private / "data/app.db") == digest
    assert not destination.exists()


def test_public_runtime_tampering_fails_and_does_not_activate(folders):
    runtime, private, destination = folders
    (runtime / "_internal/current.dll").write_bytes(b"tampered after review")
    with pytest.raises(RuntimeError, match="运行文件"):
        upgrade.refresh(runtime, private, destination)
    assert not (destination / upgrade.EXECUTABLE).exists()


def test_real_public_gate_rejects_private_payload(folders, monkeypatch):
    _runtime, private, destination = folders
    monkeypatch.undo()
    with pytest.raises(RuntimeError):
        upgrade._verify_public(private, None)
    assert not destination.exists()


def test_real_verified_public_package_preserves_private_database(
    folders, monkeypatch, tmp_path, tmp_path_factory
):
    _runtime, private, destination = folders
    monkeypatch.undo()
    public = tmp_path / "verified-public-fixture"
    built = release_fixtures.built_source.__wrapped__(tmp_path)
    demo = release_fixtures.master_demo.__wrapped__(tmp_path_factory)
    release_fixtures.assemble.assemble(built, demo, public)
    original_public = {str(p.relative_to(public)): upgrade._digest(p)
                       for p in public.rglob("*") if p.is_file()}
    result = upgrade.refresh(public, private, destination)
    assert result["private_only"] is True
    assert read_notes(destination / "data/app.db") == [("fictional original record",)]
    with pytest.raises(RuntimeError):
        upgrade._verify_public(destination, None)
    assert original_public == {str(p.relative_to(public)): upgrade._digest(p)
                               for p in public.rglob("*") if p.is_file()}


def test_actual_database_schema_mismatch_does_not_activate(folders):
    runtime, private, destination = folders
    with closing(sqlite3.connect(private / "data/app.db")) as connection:
        connection.execute("PRAGMA user_version=6")
    with pytest.raises(RuntimeError, match="实际结构版本"):
        upgrade.refresh(runtime, private, destination)
    assert (destination / upgrade.INCOMPLETE).exists()
    assert not (destination / upgrade.EXECUTABLE).exists()


def test_unknown_sqlite_sidecar_is_refused_before_copy(folders):
    runtime, private, destination = folders
    (private / "data/unknown.db-wal").write_bytes(b"unrecognized WAL fixture")
    with pytest.raises(RuntimeError, match="附属文件"):
        upgrade.refresh(runtime, private, destination)
    assert not destination.exists()


@pytest.mark.parametrize("seconds", [0, -1, 121])
def test_backup_deadline_is_bounded(folders, seconds):
    runtime, private, destination = folders
    with pytest.raises(RuntimeError, match="超时"):
        upgrade.refresh(runtime, private, destination, timeout_seconds=seconds)
    assert not destination.exists()


def test_backup_progress_deadline_retains_original(folders, monkeypatch):
    runtime, private, destination = folders
    original = upgrade._backup

    def expired(source, target, _deadline):
        return original(source, target, -1.0)

    monkeypatch.setattr(upgrade, "_backup", expired)
    with pytest.raises(RuntimeError, match="超时"):
        upgrade.refresh(runtime, private, destination)
    assert read_notes(private / "data/app.db") == [("fictional original record",)]
    assert not (destination / upgrade.EXECUTABLE).exists()
    assert (destination / upgrade.INCOMPLETE).exists()
