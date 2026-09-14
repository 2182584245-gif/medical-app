"""Strict demo copy-upgrade only; all inputs are newly generated fictional data."""

import json
import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.data.experience_schema import migrate_experience
from tools.upgrade_synthetic_demo_v7 import contract, rows, sha256, upgrade_copy


@pytest.fixture(scope="module")
def legacy(tmp_path_factory):
    from tools.generate_synthetic_demo import build_demo

    path = tmp_path_factory.mktemp("strict-fictional-old") / "legacy"
    with patch.dict(os.environ, {}):
        build_demo(path, screenshots=False)
    # Build genuine frozen schema6 in a NEW synthetic file, including exact indexes.
    # Retain the generated v7 source outside this payload; no fixture file is deleted.
    source = path / "data/app.db"
    saved = path.parent / "generated-v7.db"
    source.rename(saved)
    with closing(sqlite3.connect(saved)) as old, closing(sqlite3.connect(source)) as connection:
        Database._create_schema_v5(connection)
        migrate_experience(connection)
        for table in contract().LEGACY_COUNTS:
            records = old.execute(f'SELECT * FROM "{table}"').fetchall()
            if records:
                fields = [row[1] for row in old.execute(f'PRAGMA table_info("{table}")')]
                columns = ",".join('"' + field + '"' for field in fields)
                placeholders = ",".join("?" for _ in fields)
                connection.executemany(
                    f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', records
                )
        connection.execute("PRAGMA user_version=6")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    return path


def test_preserves_every_original_row_and_password_without_mutating_source(legacy, tmp_path):
    common = contract()
    source_hash = {name: sha256(legacy / name) for name in common.LEGACY_PAYLOAD_FILES}
    with closing(sqlite3.connect(legacy / "data/app.db")) as connection:
        before = rows(connection, common.LEGACY_COUNTS)
    destination = tmp_path / "copy-v7"
    result = upgrade_copy(legacy, destination)
    assert result["all_original_rows_identical"] and result["original_payload_unchanged"]
    assert result["cloud_imported"] is False
    assert common.verify_payload(destination)["counts"] == common.COUNTS
    assert common.credentials(destination) == common.credentials(legacy)
    with closing(sqlite3.connect(destination / "data/app.db")) as connection:
        after = rows(connection, common.COUNTS)
        for table, values in before.items():
            assert after[table][: len(values)] == values
        assert connection.execute(
            "SELECT count(*) FROM life_records WHERE local_date=?", (result["experience_day"],)
        ).fetchone() == (12,)
        assert connection.execute(
            "SELECT count(*) FROM life_records WHERE category='medical'"
        ).fetchone() == (6,)
    assert {name: sha256(legacy / name) for name in source_hash} == source_hash
    hashes = (destination / "PAYLOAD_SHA256SUMS.txt").read_text().splitlines()
    assert len(hashes) == len(common.PAYLOAD_FILES)
    assert all(
        sha256(destination / line.partition("  ")[2]).upper() == line[:64] for line in hashes
    )
    with pytest.raises(RuntimeError, match="新输出"):
        upgrade_copy(legacy, destination)


def test_release_rejects_old_five_category_seed(legacy):
    common = contract()
    assert common.verify_legacy_seed(legacy)["counts"]["life_records"] == 140
    with pytest.raises(RuntimeError):
        common.verify_payload(legacy)


def test_unknown_manifest_refused_before_output(legacy, tmp_path):
    import shutil

    bad = tmp_path / "not-approved"
    shutil.copytree(legacy, bad)
    path = bad / "demo_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["synthetic_demo"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "never-created"
    with pytest.raises(RuntimeError, match="demo_manifest"):
        upgrade_copy(bad, target)
    assert not target.exists()
