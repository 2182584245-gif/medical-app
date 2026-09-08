from __future__ import annotations

import hashlib
from copy import deepcopy
from uuid import uuid4

import pytest
from platform_test_support import PEPPER, SyntheticDatabase

from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.offline_outbox import content_version
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.services.service_management import ServiceManagementService
from server.platform_rpc import RpcDispatcher, RpcError
from server.platform_sync import EXCLUDED_FIELDS, PlatformSyncService


@pytest.fixture
def bundle():
    database = SyntheticDatabase()
    actor, other = database.account("sync-owner"), database.account("sync-other")
    services = {
        "chat": ChatService(database),
        "health": HealthService(database),
        "preferences": PreferencesService(database),
        "service_management": ServiceManagementService(database),
    }
    sync = PlatformSyncService(database, services, PEPPER)
    services["sync"] = sync
    yield database, actor, other, sync, RpcDispatcher(database, services)
    database.close()


def manifest():
    return {
        "schema_version": 1,
        "source_id": str(uuid4()),
        "source_actor_id": 987,
        "profile": {"display_name": "示例王阿姨"},
        "preferences": {"nickname": "示例昵称"},
        "life_records": [
            {
                "source_id": "31",
                "values": {
                    "category": "water",
                    "occurred_at": "2026-09-08T12:00:00+08:00",
                    "content": "示例：喝水一杯",
                    "source": "import",
                    "details": {"amount_ml": 200},
                },
            }
        ],
        "reminders": [
            {
                "source_id": "11",
                "values": {
                    "title": "示例散步",
                    "reminder_type": "custom",
                    "scheduled_at": "2026-09-09T18:30:00+08:00",
                    "enabled": False,
                },
            }
        ],
        "conversations": [
            {
                "source_id": "12",
                "title": "示例历史对话",
                "messages": [
                    {
                        "source_id": "111",
                        "role": "user",
                        "content": "历史问题",
                        "status": "complete",
                        "created_at": "2026-09-01T12:00:00+08:00",
                        "provider": None,
                        "model": None,
                    },
                    {
                        "source_id": "112",
                        "role": "assistant",
                        "content": "历史示例回答",
                        "status": "complete",
                        "created_at": "2026-09-01T12:00:03+08:00",
                        "provider": "deepseek_cloud",
                        "model": "deepseek-v4-flash",
                    },
                ],
            }
        ],
        "excluded": dict.fromkeys(EXCLUDED_FIELDS, 0),
    }


def invoke(dispatcher, actor, method, *args, request_id=None):
    payload = {
        "args": encode_rpc([actor, *args]),
        "kwargs": encode_rpc({}),
        "request_id": request_id or str(uuid4()),
    }
    return decode_rpc(dispatcher.invoke(actor, "sync", method, payload)["result"])


def test_offline_append_replay_same_uuid_and_owner_guard(bundle):
    database, actor, other, _, dispatcher = bundle
    intent = {"service": "health", "method": "add_life_record", "args": [actor],
              "kwargs": {"category": "water", "occurred_at": "2026-09-08T10:00:00+08:00",
                         "content": "合成离线喝水记录"}, "base_version": None}
    operation = str(uuid4())
    result = invoke(dispatcher, actor, "apply_queued", intent, request_id=operation)
    assert invoke(dispatcher, actor, "apply_queued", intent, request_id=operation) == result
    assert len(HealthService(database).list_life_records(actor)) == 1
    with pytest.raises(RpcError):
        invoke(dispatcher, other, "apply_queued", intent)
    assert HealthService(database).list_life_records(other) == []


def test_offline_profile_version_conflict_never_overwrites_cloud(bundle):
    database, actor, _, _, dispatcher = bundle
    health = HealthService(database)
    previous = health.get_profile(actor)
    intent = {"service": "health", "method": "save_profile", "args": [actor],
              "kwargs": {"values": {"display_name": "合成本机修改"}},
              "base_version": content_version(previous)}
    health.save_profile(actor, {"display_name": "合成已更新云端值"})
    with pytest.raises(RpcError) as caught:
        invoke(dispatcher, actor, "apply_queued", intent)
    assert caught.value.status == 409
    assert health.get_profile(actor)["display_name"] == "合成已更新云端值"


def test_offline_journal_does_not_bypass_disabled_account(bundle):
    database, actor, _, _, dispatcher = bundle
    intent = {"service": "health", "method": "add_life_record", "args": [actor],
              "kwargs": {"category": "water", "occurred_at": "2026-09-08T10:00:00+08:00",
                         "content": "合成数据"}, "base_version": None}
    operation = str(uuid4())
    invoke(dispatcher, actor, "apply_queued", intent, request_id=operation)
    with database.transaction() as connection:
        connection.execute("UPDATE users SET account_status='disabled' WHERE id=?", (actor,))
    with pytest.raises(RpcError) as caught:
        invoke(dispatcher, actor, "apply_queued", intent, request_id=operation)
    assert caught.value.status == 403


def test_paged_snapshot_exceeds_legacy_account_limit_without_truncation(bundle):
    from ollama_chat_app.services.sync_protocol import fetch_paged_snapshot

    database, actor, _, _, dispatcher = bundle
    HealthService(database).add_life_record(
        actor, "water", "2026-09-08T10:00:00+08:00", "s" * 9000)
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO life_records (user_id,category,occurred_at,local_date,"
            "timezone_offset_minutes,content,details_json,source,created_at,updated_at) "
            "SELECT user_id,category,occurred_at,local_date,timezone_offset_minutes,content,"
            "details_json,source,created_at,updated_at FROM life_records WHERE id=?",
            [(1,)] * 1004,
        )

    class Client:
        def rpc(self, service, method, args, kwargs):
            return dispatcher.invoke(actor, service, method,
                                     {"args": args, "kwargs": kwargs,
                                      "request_id": str(uuid4())})["result"]

    with pytest.raises(RpcError) as caught:
        invoke(dispatcher, actor, "get_snapshot")
    assert caught.value.status == 413
    result = fetch_paged_snapshot(Client(), actor)
    assert result["complete"] and result["schema_version"] == 2
    assert len(result["resources"]["life_records"]) == 1005
    assert sum(len(item["content"]) for item in result["resources"]["life_records"]) > 8*1024*1024


def test_paged_scope_and_changed_content_fail_closed(bundle):
    database, actor, other, _, dispatcher = bundle
    health = HealthService(database)
    health.add_life_record(actor, "water", "2026-09-08T10:00:00+08:00", "own")
    health.add_life_record(other, "water", "2026-09-08T10:00:00+08:00", "other-private")
    description = invoke(dispatcher, actor, "get_sync_manifest")["resources"]["life_records"]
    page = invoke(dispatcher, actor, "get_sync_page", "life_records", description["version"], 0)
    assert len(page["items"]) == 1 and page["items"][0]["user_id"] == actor
    with pytest.raises(RpcError):
        invoke(dispatcher, other, "get_sync_page", "life_records", description["version"], 0)
    health.add_life_record(actor, "water", "2026-09-08T10:01:00+08:00", "new")
    with pytest.raises(RpcError) as caught:
        invoke(dispatcher, actor, "get_sync_page", "life_records", description["version"], 0)
    assert caught.value.status == 409


def test_attachment_chunks_recheck_owner_and_hash(bundle):
    database, actor, other, _, dispatcher = bundle
    exchange = ChatService(database).begin_message(actor, "synthetic", "mock", "mock")
    content = b"synthetic-attachment" * 5000
    digest = hashlib.sha256(content).hexdigest()
    with database.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO chat_attachments(message_id,original_name,media_type,content,"
            "extracted_text,extraction_method,sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (exchange.user_message.id, "synthetic.txt", "text/plain", content, "synthetic",
             "text", digest, "2026-09-08T00:00:00+00:00"),
        )
        identifier = cursor.lastrowid
    first = invoke(dispatcher, actor, "get_file_chunk", "chat_attachments", identifier, digest, 0)
    assert len(first["content"]) == 65536
    last = invoke(dispatcher, actor, "get_file_chunk", "chat_attachments",
                  identifier, digest, 65536)
    assert first["content"] + last["content"] == content
    with pytest.raises(RpcError) as caught:
        invoke(dispatcher, other, "get_file_chunk", "chat_attachments", identifier, digest, 0)
    assert caught.value.status == 404
    with pytest.raises(RpcError) as caught:
        invoke(dispatcher, actor, "get_file_chunk", "chat_attachments", identifier, "0"*64, 0)
    assert caught.value.status == 409


def test_offline_appointment_intent_uses_normal_service_validation(bundle):
    _, actor, _, _, dispatcher = bundle
    intent = {"service": "service_management", "method": "request_appointment",
              "args": [actor], "kwargs": {"service_type": "合成上门服务", "notes": "合成备注"},
              "base_version": None}
    identifier = invoke(dispatcher, actor, "apply_queued", intent)
    assert type(identifier) is int and identifier > 0


def test_snapshot_self_only_and_revision_detects_delete(bundle):
    database, actor, other, sync, dispatcher = bundle
    health = HealthService(database)
    health.add_life_record(other, "water", "2026-09-08T10:00:00+08:00", "other-private")
    before = invoke(dispatcher, actor, "get_snapshot")
    assert before["complete"] and not before["resources"]["life_records"]
    assert [item["id"] for item in before["resources"]["members"]] == [actor]
    assert "password_hash" not in str(before)
    assert "$argon2" not in str(before)
    key = health.add_life_record(actor, "water", "2026-09-08T10:00:00+08:00", "own")
    after = invoke(dispatcher, actor, "get_snapshot")
    assert len(after["resources"]["life_records"]) == 1
    assert before["revision"] != after["revision"]
    health.delete_life_record(actor, key)
    deleted = invoke(dispatcher, actor, "get_snapshot")
    assert deleted["resources"]["life_records"] == []
    assert after["server_instance_id"] == sync.instance_id


@pytest.mark.parametrize("role", ["advisor", "operator"])
def test_staff_snapshot_does_not_request_member_health_resources(bundle, role):
    database, _, _, _, dispatcher = bundle
    staff = database.account("snapshot-" + role, role=role)
    snapshot = invoke(dispatcher, staff, "get_snapshot")
    assert snapshot["complete"] is True
    assert snapshot["resources"]["profile"] == {}
    assert snapshot["resources"]["life_records"] == []
    if role == "advisor":
        assert snapshot["resources"]["work_statistics"] == {}
    assert snapshot["resources"]["advisors"] == []


def test_first_migration_remaps_owned_rows_and_retries_once(bundle):
    database, actor, other, _, dispatcher = bundle
    source = manifest()
    operation = str(uuid4())
    result = invoke(dispatcher, actor, "import_member", source, request_id=operation)
    again = invoke(dispatcher, actor, "import_member", source, request_id=operation)
    assert result == again and result["source_preserved"] is True
    assert database.scalar("SELECT COUNT(*) FROM life_records WHERE user_id=?", (actor,)) == 1
    assert database.scalar("SELECT COUNT(*) FROM life_records WHERE user_id=?", (other,)) == 0
    assert database.scalar("SELECT status FROM reminders WHERE user_id=?", (actor,)) == "disabled"
    assert database.scalar("SELECT COUNT(*) FROM messages") == 2
    assert result["id_mapping"]["life_records"]["31"] != 31
    with pytest.raises(RpcError) as error:
        invoke(dispatcher, actor, "import_member", source)
    assert error.value.status == 409


def test_invalid_late_row_rolls_back_profile_and_earlier_records(bundle):
    database, actor, _, _, dispatcher = bundle
    source = manifest()
    bad = deepcopy(source["life_records"][0])
    bad["source_id"], bad["values"]["category"] = "32", "unknown-kind"
    source["life_records"].append(bad)
    with pytest.raises(ValueError):
        invoke(dispatcher, actor, "import_member", source)
    assert database.scalar("SELECT COUNT(*) FROM life_records") == 0
    assert database.scalar("SELECT COUNT(*) FROM member_profiles") == 0
    assert database.scalar("SELECT COUNT(*) FROM rpc_requests") == 0


@pytest.mark.parametrize(
    "patch",
    [{"api_key": "not-a-real-key"}, {"ai_context_consent": True}, {"avatar_path": "C:/secret"}],
)
def test_migration_rejects_credentials_consent_and_paths(bundle, patch):
    _, actor, _, _, dispatcher = bundle
    source = manifest()
    source["preferences"] = patch
    with pytest.raises(ValueError):
        invoke(dispatcher, actor, "import_member", source)


def test_existing_profile_conflict_never_overwrites(bundle):
    database, actor, _, _, dispatcher = bundle
    HealthService(database).save_profile(actor, {"display_name": "Existing"})
    with pytest.raises(RpcError):
        invoke(dispatcher, actor, "import_member", manifest())
    assert HealthService(database).get_profile(actor)["display_name"] == "Existing"


def test_bad_identity_and_duplicate_message_ids_fail_before_mutation(bundle):
    database, actor, _, _, dispatcher = bundle
    source = manifest()
    source["conversations"][0]["messages"][1]["source_id"] = "111"
    with pytest.raises(ValueError):
        invoke(dispatcher, actor, "import_member", source)
    assert database.scalar("SELECT COUNT(*) FROM life_records") == 0
