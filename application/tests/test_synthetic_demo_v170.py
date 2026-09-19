"""Expanded fictional personas remain isolated, attributable and medically bounded."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from ollama_chat_app.security.passwords import verify_password
from ollama_chat_app.services.health import HealthService
from tools.generate_synthetic_demo import (
    EXPANDED_ACCOUNTS,
    EXPANDED_ANCHOR,
    EXPANDED_DAYS,
    EXPANDED_PROFILE,
    build_demo,
    expanded_life_records,
)
from tools.upgrade_synthetic_demo_v7 import contract, upgrade_copy


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def rich_fixture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("rich-fictional-personas")
    legacy = directory / "legacy"
    source = directory / "old-release"
    destination = directory / "expanded"
    with patch.dict(os.environ, {}):
        build_demo(legacy, screenshots=False)
        upgrade_copy(legacy, source)
        source_hash = _sha(source / "data/app.db")
        passwords = contract().credentials(source)
        result = build_demo(destination, profile=EXPANDED_PROFILE, credential_seed=source)
    assert _sha(source / "data/app.db") == source_hash
    return destination, source, result, passwords


def test_fixed_content_has_six_categories_210_days_and_realistic_unit_variation():
    rows = list(expanded_life_records(EXPANDED_ANCHOR))
    assert rows == list(expanded_life_records(EXPANDED_ANCHOR))
    assert len(rows) == 4632
    assert {item[3] for item in rows} == {
        "diet",
        "water",
        "activity",
        "sleep",
        "environment",
        "medical",
    }
    for person in (0, 1):
        selected = [item for item in rows if item[0] == person]
        days = {item[1] for item in selected}
        assert len(days) == EXPANDED_DAYS
        assert min(days) == EXPANDED_ANCHOR - timedelta(days=EXPANDED_DAYS - 1)
        assert max(days) == EXPANDED_ANCHOR
        assert len({item[4] for item in selected if item[3] == "diet"}) >= 25
        assert len({item[5]["duration_hours"] for item in selected if item[3] == "sleep"}) >= 5
        assert len({item[5]["temperature_c"] for item in selected if item[3] == "environment"}) > 25
        drinks = Counter()
        for _, day, _, category, _, detail in selected:
            HealthService._validate_record_details(category, detail)
            if category == "water":
                assert 200 <= detail["amount_ml"] <= 350
                drinks[day] += detail["amount_ml"]
        assert max(drinks.values()) <= 1750  # No duplicated all-day amount per cup.
        assert len({item[2] for item in selected}) > 8


def test_expanded_payload_keeps_same_five_fictional_logins_and_internal_provenance(rich_fixture):
    root, _source, result, passwords = rich_fixture
    assert result["synthetic_demo"] is True
    assert result["profile"] == EXPANDED_PROFILE
    assert result["cloud_imported"] is False
    assert result["passwords_preserved"] is True
    assert result["ai_calls"] == 0
    assert set(result["account_provenance"]) == set(EXPANDED_ACCOUNTS)
    assert result["counts"]["users"] == 5
    assert result["counts"]["life_records"] == 4632
    assert result["counts"]["messages"] == 72
    assert result["counts"]["conversations"] == 17
    with closing(sqlite3.connect(root / "data/app.db")) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        users = connection.execute("SELECT username,password_hash,role_code FROM users").fetchall()
        assert {row[0] for row in users} == set(EXPANDED_ACCOUNTS)
        assert {row[2] for row in users} == {"operator", "advisor", "member"}
        assert all(verify_password(row[1], passwords[row[0]]) for row in users)
        for detail, source in connection.execute("SELECT details_json,source FROM life_records"):
            assert source == "import"
            provenance = json.loads(detail)
            assert provenance["synthetic_demo"] is True
            assert provenance["synthetic_profile"] == EXPANDED_PROFILE
            assert provenance["measured"] is False
            assert provenance["generated_by_ai"] is False
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE model='synthetic-authored' AND provider='fixture'"
        ).fetchone() == (72,)
    for file in ("DEMO_ACCOUNTS.md", "DEMO_ACCOUNTS.txt"):
        content = (root / file).read_text(encoding="utf-8-sig")
        assert "虚构" in content and "不是现场调用 DeepSeek" in content
        assert all(password in content for password in passwords.values())


def test_visible_records_and_persona_names_have_no_fixture_badges(rich_fixture):
    root, *_ = rich_fixture
    with closing(sqlite3.connect(root / "data/app.db")) as connection:
        for table, fields in {
            "member_profiles": ["display_name", "living_situation", "medical_notes"],
            "advisor_profiles": ["display_name", "organization", "specialty", "bio"],
            "life_records": ["content"],
            "reminders": ["title"],
            "memberships": ["plan_code"],
            "visit_tasks": ["title", "notes"],
            "visit_records": ["summary"],
            "products": ["name", "brand", "description"],
            "orders": ["product_name_snapshot"],
            "conversations": ["title"],
            "messages": ["content"],
        }.items():
            for field in fields:
                assert connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{field}" LIKE ?', ("%示例%",)
                ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM member_profiles WHERE phone IS NOT NULL AND phone<>''"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM member_profiles WHERE emergency_contact_phone IS NOT NULL "
            "AND emergency_contact_phone<>''"
        ).fetchone() == (0,)


def test_dates_contacts_and_medical_records_do_not_imply_live_measurements(rich_fixture):
    root, _source, manifest, _passwords = rich_fixture
    with closing(sqlite3.connect(root / "data/app.db")) as connection:
        dates = connection.execute(
            "SELECT MIN(local_date),MAX(local_date),COUNT(DISTINCT local_date) FROM life_records"
        ).fetchone()
        assert dates == (manifest["first_record_day"], manifest["experience_day"], 210)
        assert connection.execute(
            "SELECT COUNT(*) FROM life_records WHERE occurred_at < "
            "(SELECT created_at FROM users WHERE users.id=life_records.user_id)"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE status<>'complete'"
        ).fetchone() == (0,)
        clinical = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT details_json FROM life_records WHERE category='medical'"
            )
        ]
        assert len(clinical) == 60
        assert {row["event_type"] for row in clinical} == {"consultation", "medication", "other"}
        for row in clinical:
            assert not set(row) & {"dose", "dose_mg", "drug_name", "prescription", "diagnosis"}
        assert connection.execute("SELECT COUNT(*) FROM visit_records").fetchone() == (14,)
        assert connection.execute("SELECT COUNT(*) FROM advisor_bindings").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone() == (16,)


def test_refuses_overwrite_and_unverified_password_sources_before_writing(rich_fixture, tmp_path):
    root, *_ = rich_fixture
    old_hash = _sha(root / "data/app.db")
    with pytest.raises(FileExistsError):
        build_demo(root, profile=EXPANDED_PROFILE)
    assert _sha(root / "data/app.db") == old_hash
    unknown = tmp_path / "unknown-input"
    unknown.mkdir()
    (unknown / "demo_manifest.json").write_text('{"synthetic_demo":true}', encoding="utf-8")
    output = tmp_path / "must-not-exist"
    with pytest.raises(RuntimeError):
        build_demo(output, profile=EXPANDED_PROFILE, credential_seed=unknown)
    assert not output.exists()
    with pytest.raises(ValueError):
        build_demo(output, profile="legacy", anchor=date(2026, 9, 19))
    assert not output.exists()


def test_no_api_secrets_or_plaintext_passwords_inside_database(rich_fixture):
    root, _source, _manifest, passwords = rich_fixture
    raw = (root / "data/app.db").read_bytes()
    assert b"sk-" not in raw and b"postgresql://" not in raw
    assert all(password.encode() not in raw for password in passwords.values())


def test_release_verifier_accepts_only_reviewed_expanded_payload(rich_fixture):
    root, _source, _manifest, passwords = rich_fixture
    verifier = contract().sibling("_demo_v170")
    before = _sha(root / "data/app.db")
    result = verifier.verify_payload(root)
    assert verifier.PROFILE_ID == EXPANDED_PROFILE
    assert result["counts"]["life_records"] == 4632
    assert result["business_fingerprint"] == verifier.BUSINESS_SHA256
    assert result["authored_chat_not_live_ai"] is True
    assert result["credentials_verified_without_display"] is True
    assert verifier.credentials(root) == passwords
    assert _sha(root / "data/app.db") == before


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE member_profiles SET medical_notes='有人提供的真实医疗记录' WHERE user_id=4",
        "UPDATE life_records SET content='替换成同数量的另一份记录' WHERE id=1",
        "UPDATE messages SET model='deepseek-v4-flash' WHERE id=2",
        "UPDATE advisor_profiles SET bio='替换其他个人简介' WHERE user_id=2",
        "UPDATE life_records SET details_json=json_remove(details_json,'$.synthetic_demo') "
        "WHERE id=1",
        "UPDATE user_preferences SET preferences_json=json_set(preferences_json,"
        "'$.ai_context_consent',json('true')) WHERE user_id=4",
        "UPDATE orders SET order_no='REAL-PAYMENT-001' WHERE id=1",
    ],
)
def test_release_verifier_rejects_same_count_unreviewed_data(rich_fixture, tmp_path, sql):
    source, *_ = rich_fixture
    destination = tmp_path / "unreviewed-copy"
    shutil.copytree(source, destination)
    with closing(sqlite3.connect(destination / "data/app.db")) as connection:
        connection.execute(sql)
        connection.commit()
    with pytest.raises(RuntimeError):
        contract().sibling("_demo_v170").verify_payload(destination)


def test_release_verifier_requires_explicit_provenance_and_matching_credential_guide(
    rich_fixture, tmp_path
):
    source, *_ = rich_fixture
    destination = tmp_path / "mismatched-copy"
    shutil.copytree(source, destination)
    path = destination / "demo_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["message_provenance"] = "live-model-output"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError):
        contract().sibling("_demo_v170").verify_payload(destination)
    manifest["message_provenance"] = "authored-dialogue-not-live-model-output"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    guide = destination / "DEMO_ACCOUNTS.txt"
    guide.write_text(guide.read_text(encoding="utf-8-sig") + "额外账号说明", encoding="utf-8-sig")
    with pytest.raises(RuntimeError, match="不一致"):
        contract().sibling("_demo_v170").verify_payload(destination)
