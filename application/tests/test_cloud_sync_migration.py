from __future__ import annotations

import hashlib
import json
import os
import threading
from copy import deepcopy

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_migration import (
    MAX_MANIFEST_BYTES,
    MemberMigrationPlanner,
    MemberMigrationService,
    MigrationError,
    MigrationJournal,
)
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.cloud_sync import (
    RESOURCE_TYPES,
    CloudMirrorStore,
    CloudSyncService,
    SyncError,
    SyncIdentity,
)
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.time_utils import beijing_now
from ollama_chat_app.ui.data_sync_dialog import DataSyncDialog, MirrorViewDialog

INSTANCE = "synthetic-instance-00001"
ENDPOINT = "https://example.com/aliyun"


def snapshot(actor=1, revision="1", instance=INSTANCE):
    resources = {key: kind() for key, kind in RESOURCE_TYPES.items()}
    resources["profile"] = {"display_name": f"合成用户{actor}"}
    return {
        "schema_version": 1,
        "server_instance_id": instance,
        "actor_id": actor,
        "revision": revision,
        "complete": True,
        "resources": resources,
        "excluded": {"chat_attachments": 0, "user_files": 0, "report_files": 0},
    }


def test_mirror_isolated_atomic_and_cannot_read_without_authorization(tmp_path):
    mirror = CloudMirrorStore(tmp_path)
    first = SyncIdentity(ENDPOINT, INSTANCE, 1)
    second = SyncIdentity(ENDPOINT, INSTANCE, 2)
    mirror.activate(first)
    mirror.apply_snapshot(snapshot())
    original_path = mirror.path
    partial = snapshot(revision="2")
    partial["complete"] = False
    with pytest.raises(SyncError):
        mirror.apply_snapshot(partial)
    assert mirror.snapshot()["revision"] == "1"
    unsafe = snapshot(revision="2")
    unsafe["resources"]["preferences"]["api_key"] = "synthetic-secret"
    with pytest.raises(SyncError):
        mirror.apply_snapshot(unsafe)
    mirror.revoke()
    with pytest.raises(SyncError):
        mirror.read_resource("profile")
    mirror.activate(second)
    assert mirror.path != original_path
    with pytest.raises(SyncError):
        mirror.snapshot()
    mirror.apply_snapshot(snapshot(2))
    assert mirror.read_resource("profile")["display_name"] == "合成用户2"
    other_target = SyncIdentity("https://example.com/supabase", INSTANCE, 2)
    assert other_target.cache_key != second.cache_key


def test_sync_offline_is_readonly_and_auth_failure_revokes(qtbot, tmp_path):
    class Client:
        base_url = ENDPOINT
        is_authenticated = True
        response = snapshot()

        def rpc(self, *_args, **_kwargs):
            if isinstance(self.response, Exception):
                raise self.response
            return encode_rpc(self.response)

    client = Client()
    service = CloudSyncService(client, CloudMirrorStore(tmp_path))
    service.start(1)
    qtbot.waitUntil(lambda: not service.is_syncing)
    assert service.state()["state"] == "online"
    client.response = CloudAPIError("network")
    service.sync_now()
    qtbot.waitUntil(lambda: not service.is_syncing)
    assert service.state()["state"] == "offline"
    assert service.state()["read_only"] is True
    assert service.mirror.read_resource("profile")["display_name"] == "合成用户1"
    client.response = CloudAPIError("permission", status_code=403)
    service.sync_now()
    qtbot.waitUntil(lambda: not service.is_syncing)
    assert service.state()["authorized"] is False
    assert not service.timer.isActive()
    assert service.sync_now() is False
    with pytest.raises(SyncError):
        service.mirror.snapshot()
    service.stop()


def test_account_change_discards_late_result_and_never_overlaps_workers(qtbot, tmp_path):
    release = threading.Event()
    entered = threading.Event()

    class Client:
        base_url = ENDPOINT
        is_authenticated = True
        active = 0
        max_active = 0

        def rpc(self, service, method, args, kwargs):
            actor = decode_rpc(args)[0]
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if actor == 1:
                entered.set()
                release.wait(3)
            self.active -= 1
            return encode_rpc(snapshot(actor))

    client = Client()
    service = CloudSyncService(client, CloudMirrorStore(tmp_path))
    service.start(1)
    qtbot.waitUntil(entered.is_set)
    service.start(2)
    assert not service.sync_now()
    release.set()
    qtbot.waitUntil(lambda: not service.is_syncing and service.state()["state"] == "online")
    assert service.identity.actor_id == 2
    assert service.mirror.read_resource("profile")["display_name"] == "合成用户2"
    assert client.max_active == 1
    assert len(list(tmp_path.glob("cloud-*.sqlite"))) == 1
    service.stop()


@pytest.fixture
def migration_context(tmp_path):
    database = Database(tmp_path / "synthetic-local.db")
    auth = AuthService(database)
    first = auth.register("source-member", "synthetic-password-123")
    second = auth.register("unrelated-member", "synthetic-password-123")
    health = HealthService(database)
    health.add_life_record(
        first.id,
        category="water",
        content="本人合成记录",
        occurred_at=beijing_now(),
        details={"amount_ml": 300},
    )
    health.add_life_record(
        second.id,
        category="water",
        content="其他账户记录禁止迁入",
        occurred_at=beijing_now(),
        details={"amount_ml": 200},
    )
    PreferencesService(database).update(
        first.id,
        {"nickname": "合成昵称", "avatar_path": "private-path.png", "weather_consent": True},
    )
    chat = ChatService(database)
    pending = chat.begin_message(first.id, "合成对话", "synthetic", "model")
    chat.complete_message(first.id, pending.assistant_message.id, "合成回复")
    chat.begin_message(first.id, "未完成对话", "synthetic", "model")
    journal = MigrationJournal(tmp_path / "journal.sqlite")
    planner = MemberMigrationPlanner(database, journal)
    return database, first, second, journal, planner


def test_migration_plan_reads_only_owned_data_and_does_not_modify_source(migration_context):
    database, first, second, journal, planner = migration_context
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    plan = planner.build_plan(first.id)
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before
    raw = json.dumps(plan.manifest, ensure_ascii=False)
    assert "本人合成记录" in raw
    assert "其他账户记录禁止迁入" not in raw
    assert "password" not in raw
    assert "private-path.png" not in raw
    assert "weather_consent" not in raw
    assert plan.excluded["pending_messages"] == 1
    assert plan.counts["messages"] == 3
    assert plan.manifest["life_records"][0]["values"]["source"] == "import"
    assert planner.build_plan(first.id).manifest["source_id"] == plan.manifest["source_id"]
    assert planner.build_plan(second.id).manifest["source_id"] != plan.manifest["source_id"]


def test_migration_uncertain_retry_keeps_request_id_and_rejects_changed_manifest(migration_context):
    database, first, _second, journal, planner = migration_context
    plan = planner.build_plan(first.id)
    target = SyncIdentity(ENDPOINT, INSTANCE, 101)

    class Client:
        base_url = ENDPOINT
        is_authenticated = True
        requests = []
        uncertain = True

        def rpc(self, service, method, args, kwargs, request_id=None):
            if method == "get_snapshot":
                return encode_rpc(snapshot(101))
            self.requests.append(request_id)
            if self.uncertain:
                raise CloudAPIError("timeout", outcome_uncertain=True)
            return encode_rpc({"status": "completed", "id_mapping": {"life_records": {"1": 500}}})

    client = Client()
    migration = MemberMigrationService(client, journal)
    with pytest.raises(CloudAPIError):
        migration.execute(plan, target)
    assert journal.job(target, plan)["state"] == "uncertain"
    HealthService(database).add_life_record(
        first.id, "water", beijing_now(), "发送不确定后新增的本机记录"
    )
    recovered = planner.prepare_for_target(first.id, target)
    assert recovered.resumed is True
    assert recovered.manifest_hash == plan.manifest_hash
    assert "发送不确定后新增" not in json.dumps(recovered.manifest, ensure_ascii=False)
    client.uncertain = False
    result = migration.execute(plan, target)
    assert result["status"] == "completed"
    assert len(client.requests) == 2
    assert client.requests[0] == client.requests[1]
    assert migration.execute(plan, target) == result
    assert len(client.requests) == 2
    mutated = deepcopy(plan)
    mutated.manifest["life_records"][0]["values"]["content"] = "预览后被改变"
    with pytest.raises(MigrationError, match="清单已改变"):
        migration.execute(mutated, target)


def test_manifest_size_cap_blocks_whole_plan_without_truncation(migration_context):
    database, first, _second, _journal, planner = migration_context
    with database.transaction() as connection:
        for _ in range(100):
            connection.execute(
                "INSERT INTO life_records(user_id,category,occurred_at,local_date,"
                "timezone_offset_minutes,content,details_json,source,created_at,updated_at) "
                "SELECT user_id,category,occurred_at,local_date,timezone_offset_minutes,?,"
                "details_json,source,created_at,updated_at FROM life_records "
                "WHERE user_id=? LIMIT 1",
                ("x" * 9900, first.id),
            )
    plan = planner.build_plan(first.id)
    assert MAX_MANIFEST_BYTES == 900 * 1024
    assert plan.payload_bytes > MAX_MANIFEST_BYTES
    assert plan.blocked_reason and "未截断" in plan.blocked_reason
    assert len(plan.manifest["life_records"]) == 101


def test_dialog_authenticates_local_source_only_after_explicit_preview(
    qtbot, tmp_path, migration_context
):
    _database, first, _second, _journal, planner = migration_context

    class Client:
        base_url = ENDPOINT
        is_authenticated = True

    service = CloudSyncService(Client(), CloudMirrorStore(tmp_path / "mirror"))
    calls = []

    def authenticate():
        calls.append("explicit")
        return first.id

    dialog = DataSyncDialog(service, planner=planner, source_authenticator=authenticate)
    qtbot.addWidget(dialog)
    assert calls == []
    assert dialog.local_actor_id is None
    assert dialog.preview_button.isEnabled()
    dialog.preview()
    qtbot.waitUntil(lambda: dialog._task is None)
    assert calls == ["explicit"]
    assert dialog.plan.manifest["source_actor_id"] == first.id
    assert not dialog.execute_button.isEnabled()
    dialog.reject()


def test_open_mirror_view_clears_on_stop_without_exposing_previous_account(qtbot, tmp_path):
    class Client:
        base_url = ENDPOINT
        is_authenticated = True

    mirror = CloudMirrorStore(tmp_path)
    identity = SyncIdentity(ENDPOINT, INSTANCE, 1)
    mirror.activate(identity)
    mirror.apply_snapshot(snapshot())
    service = CloudSyncService(Client(), mirror)
    service.actor_id, service.identity = 1, identity
    dialog = MirrorViewDialog(service)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 1
    service.stop()
    assert dialog.table.rowCount() == 0
    assert service.state()["last_success"] is None
    dialog.reject()
