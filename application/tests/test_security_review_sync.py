"""Focused synthetic regressions from the independent synchronization security review."""

from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat_personalization import personalized_system_message
from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_migration import (
    MemberMigrationPlanner,
    MemberMigrationService,
    MigrationError,
    MigrationJournal,
    MigrationPlan,
)
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.cloud_sync import (
    RESOURCE_TYPES,
    CloudMirrorStore,
    CloudSyncService,
    SyncError,
)
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.ui.data_sync_dialog import DataSyncDialog


def test_context_disabled_does_not_send_saved_city_but_preserves_requested_name():
    preferences = {"city": "合成保密城市", "preferred_name": "合成阿姨", "weather_consent": False}
    message = personalized_system_message(1, "请讲一个故事", preferences, None, share_context=False)
    assert "合成保密城市" not in message["content"]
    assert "合成阿姨" in message["content"]
    allowed = personalized_system_message(1, "请讲一个故事", preferences, None, share_context=True)
    assert "合成保密城市" in allowed["content"]


def snapshot():
    return {
        "schema_version": 1,
        "server_instance_id": "synthetic-security-instance",
        "actor_id": 4,
        "revision": "before-import",
        "complete": True,
        "resources": {key: kind() for key, kind in RESOURCE_TYPES.items()},
        "excluded": {"chat_attachments": 0, "user_files": 0, "report_files": 0},
    }


def plan():
    manifest = {
        "schema_version": 1,
        "source_id": str(uuid4()),
        "source_actor_id": 9,
        "profile": None,
        "preferences": None,
        "life_records": [],
        "reminders": [],
        "conversations": [],
        "excluded": {
            "chat_attachments": 0,
            "user_files": 0,
            "pending_messages": 0,
            "system_messages": 0,
            "orders": 0,
            "service_assignments": 0,
        },
    }
    raw = json.dumps(
        encode_rpc(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return MigrationPlan(manifest, hashlib.sha256(raw).hexdigest(), len(raw), {}, {}, "synthetic")


class Client:
    base_url = "https://example.com/aliyun"
    is_authenticated = True
    fail = False

    def rpc(self, service, method, args, kwargs, request_id=None):
        if method == "get_snapshot":
            return encode_rpc(snapshot())
        if self.fail:
            raise CloudAPIError("timeout", outcome_uncertain=True)
        return encode_rpc({"status": "imported"})


def make_sync(tmp_path):
    client = Client()
    sync = CloudSyncService(client, CloudMirrorStore(tmp_path / "mirror"))
    sync.actor_id = 4
    sync._accept(sync.cache_generation, 4, snapshot())
    return client, sync


def test_migration_dialog_success_invalidates_previous_empty_snapshot(qtbot, tmp_path, monkeypatch):
    _client, sync = make_sync(tmp_path)
    monkeypatch.setattr(sync, "sync_now", lambda: False)
    dialog = DataSyncDialog(sync)
    qtbot.addWidget(dialog)
    dialog._migration_done({"status": "imported"})
    with pytest.raises(SyncError):
        sync.mirror.snapshot()
    dialog.reject()


@pytest.mark.parametrize("uncertain", [False, True])
def test_migration_service_invalidates_on_success_and_uncertain_outcome(
    qtbot, tmp_path, monkeypatch, uncertain
):
    client, sync = make_sync(tmp_path)
    client.fail = uncertain
    monkeypatch.setattr(sync, "sync_now", lambda: False)
    migration = MemberMigrationService(
        client, MigrationJournal(tmp_path / "journal"), sync_service=sync
    )
    if uncertain:
        with pytest.raises(CloudAPIError):
            migration.execute(plan(), sync.identity)
    else:
        migration.execute(plan(), sync.identity)
    with pytest.raises(SyncError):
        sync.mirror.snapshot()


def test_snapshot_capacity_rejection_blocks_old_mirror(qtbot, tmp_path):
    _client, sync = make_sync(tmp_path)
    sync._failed(sync.cache_generation, CloudAPIError("validation", status_code=413))
    assert sync.state()["state"] == "blocked"
    assert sync.state()["fresh"] is False
    assert sync.state()["authorized"] is False
    with pytest.raises(SyncError):
        sync.mirror.snapshot()


def test_planner_rejects_1001_records_without_truncating_or_changing_source(tmp_path):
    database = Database(tmp_path / "source.db")
    member = AuthService(database).register("synthetic-source", "Synthetic-password-123!")
    HealthService(database).add_life_record(member.id, "water", "2026-09-08T08:00:00Z", "合成记录")
    with database.transaction() as connection:
        for _ in range(1000):
            connection.execute(
                "INSERT INTO life_records(user_id,category,occurred_at,local_date,"
                "timezone_offset_minutes,content,details_json,source,created_at,updated_at) "
                "SELECT user_id,category,occurred_at,local_date,timezone_offset_minutes,content,"
                "details_json,source,created_at,updated_at FROM life_records "
                "WHERE user_id=? LIMIT 1",
                (member.id,),
            )
    before = database.path.read_bytes()
    journal = MigrationJournal(tmp_path / "journal")
    oversized = MemberMigrationPlanner(database, journal).build_plan(member.id)
    assert len(oversized.manifest["life_records"]) == 1001
    assert "1000" in oversized.blocked_reason
    assert database.path.read_bytes() == before
    client, sync = make_sync(tmp_path)
    with pytest.raises(MigrationError, match="1000"):
        MemberMigrationService(client, journal, sync_service=sync).execute(oversized, sync.identity)
