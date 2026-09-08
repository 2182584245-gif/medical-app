from __future__ import annotations

import inspect
import threading
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.cloud_sync import (
    RESOURCE_TYPES,
    CloudMirrorStore,
    CloudSyncService,
    SyncError,
)
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.remote_services import (
    RemoteChatService,
    RemoteHealthService,
    RemoteServiceManagementService,
)
from ollama_chat_app.services.synced_remote import SyncedRemoteService

ENDPOINT = "https://synthetic.example/aliyun"
INSTANCE = "synthetic-cache-instance-001"


@pytest.fixture
def mirror_context(qtbot, tmp_path):
    database = Database(tmp_path / "synthetic-domain.sqlite")
    user = AuthService(database).register("mirror-member", "synthetic-password-only")
    health = HealthService(database)
    health.save_profile(user.id, {"display_name": "合成称呼"})
    for index, when in enumerate(
        (
            "2026-09-06T15:59:59+00:00",
            "2026-09-06T16:00:00+00:00",
            "2026-09-07T15:59:59+00:00",
            "2026-09-07T16:00:00+00:00",
            "2026-09-07T08:00:00-07:00",
        )
    ):
        health.add_life_record(user.id, "water" if index % 2 else "diet", when, f"合成记录{index}")
    chat = ChatService(database)
    first = chat.get_default_conversation(user.id)
    second = chat.create_conversation(user.id, "第二合成对话")
    for index in range(3):
        exchange = chat.begin_message(user.id, f"合成提问{index}", conversation_id=second.id)
        chat.complete_message(user.id, exchange.assistant_message.id, f"合成回答{index}")
    resources = {name: kind() for name, kind in RESOURCE_TYPES.items()}
    resources.update(
        profile=health.get_profile(user.id),
        life_records=health.list_life_records(user.id),
        reminders=health.list_reminders(user.id),
        service_summary=health.get_service_summary(user.id),
        conversations=chat.list_conversations(user.id),
        messages={
            str(row.id): chat.list_messages(user.id, conversation_id=row.id)
            for row in chat.list_conversations(user.id)
        },
        members=[{"id": user.id, "advisor_user_id": 4}],
    )
    snapshot = {
        "schema_version": 1,
        "server_instance_id": INSTANCE,
        "actor_id": user.id,
        "revision": "one",
        "complete": True,
        "resources": resources,
        "excluded": {"chat_attachments": 0, "user_files": 0, "report_files": 0},
    }

    class Client:
        base_url = ENDPOINT
        is_authenticated = True
        response = CloudAPIError("network")

        def __init__(self):
            self.calls = []
            self.snapshot = snapshot

        def rpc(self, service, method, args, kwargs, *, request_id=None):
            self.calls.append((service, method, decode_rpc(args), decode_rpc(kwargs), request_id))
            if service == "sync":
                return encode_rpc(deepcopy(self.snapshot))
            if isinstance(self.response, Exception):
                raise self.response
            return encode_rpc(self.response)

    client = Client()
    sync = CloudSyncService(client, CloudMirrorStore(tmp_path / "mirror"))
    sync.actor_id, sync.actor_role = user.id, "member"
    sync._accept(sync.cache_generation, user.id, snapshot)
    context = {
        "database": database,
        "user": user,
        "health": health,
        "chat": chat,
        "first": first,
        "second": second,
        "snapshot": snapshot,
        "sync": sync,
        "client": client,
        "remote": SyncedRemoteService(RemoteHealthService(client), sync),
    }
    yield context
    sync.stop()
    qtbot.waitUntil(lambda: not sync.is_syncing)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"category": "water"},
        {"limit": 2},
        {"start_date": "2026-09-07", "end_date": "2026-09-07"},
        {"start_date": date(2026, 9, 7)},
        {"end_date": "2026-09-07", "category": "diet", "limit": 1},
        {"start_date": datetime(2026, 9, 6, 16, tzinfo=UTC), "end_date": date(2026, 9, 7)},
    ],
)
def test_life_snapshot_matches_existing_service_beijing_boundaries(mirror_context, options):
    c = mirror_context
    assert c["remote"].list_life_records(c["user"].id, **options) == c["health"].list_life_records(
        c["user"].id, **options
    )
    assert c["client"].calls == []


def test_other_health_reads_are_owned_detached_snapshots(mirror_context):
    c = mirror_context
    remote, actor = c["remote"], c["user"].id
    profile = remote.get_profile(actor)
    profile["display_name"] = "不可修改镜像"
    assert remote.get_profile(actor)["display_name"] == "合成称呼"
    assert remote.list_reminders(actor) == c["health"].list_reminders(actor)
    assert remote.get_service_summary(actor) == c["health"].get_service_summary(actor)
    assert c["client"].calls == []


@pytest.mark.parametrize("limit", [None, -1, 0, 1, 3, 99])
def test_chat_snapshots_match_conversation_and_last_n_message_contract(mirror_context, limit):
    c = mirror_context
    remote = SyncedRemoteService(RemoteChatService(c["client"]), c["sync"])
    actor = c["user"].id
    assert remote.list_conversations(actor) == c["chat"].list_conversations(actor)
    assert remote.get_default_conversation(actor) == c["first"]
    assert remote.get_conversation(actor) == c["first"]
    assert remote.get_conversation(actor, c["second"].id) == c["chat"].get_conversation(
        actor, c["second"].id
    )
    assert remote.list_messages(actor, conversation_id=c["second"].id, limit=limit) == c[
        "chat"
    ].list_messages(actor, conversation_id=c["second"].id, limit=limit)
    assert c["client"].calls == []


def test_foreign_actor_and_conversation_never_use_cache_or_remote(mirror_context):
    c = mirror_context
    with pytest.raises(CloudAPIError, match="权限"):
        c["remote"].get_profile(c["user"].id + 1)
    remote = SyncedRemoteService(RemoteChatService(c["client"]), c["sync"])
    with pytest.raises(CloudAPIError):
        remote.list_messages(c["user"].id, conversation_id=9999)
    assert c["client"].calls == []


def test_unknown_filters_and_incomplete_mirrors_fall_back_remote(mirror_context):
    c = mirror_context
    with pytest.raises(CloudAPIError) as error:
        c["remote"].get_record_statistics(c["user"].id)
    assert error.value.code == "network"
    c["sync"].mirror.mark_stale()
    with pytest.raises(CloudAPIError):
        c["remote"].get_profile(c["user"].id)
    assert [call[1] for call in c["client"].calls] == ["get_record_statistics", "get_profile"]


def test_wrapped_methods_keep_public_signatures_and_endpoint_scope(mirror_context):
    c = mirror_context
    assert inspect.signature(c["remote"].list_life_records) == inspect.signature(
        RemoteHealthService(c["client"]).list_life_records
    )
    assert c["remote"].list_life_records.__name__ == "list_life_records"
    assert c["remote"].client is c["client"] and c["remote"].uses_network
    assert c["remote"].service_name == "health"
    c["client"].base_url = "https://synthetic.example/supabase"
    with pytest.raises(CloudAPIError):
        c["remote"].get_profile(c["user"].id)


def test_successful_remote_write_invalidates_before_return_and_never_writes_local(mirror_context):
    c = mirror_context
    c["client"].response = 456
    before = c["health"].list_life_records(c["user"].id)
    assert (
        c["remote"].add_life_record(
            c["user"].id, "water", "2026-09-07T08:00:00+08:00", "仅云端新记录"
        )
        == 456
    )
    assert c["health"].list_life_records(c["user"].id) == before
    assert c["sync"].mirror.authorized  # Freshness is separate from login authorization.
    with pytest.raises(SyncError):
        c["sync"].mirror.snapshot()
    c["client"].response = CloudAPIError("network")
    with pytest.raises(CloudAPIError):
        c["remote"].get_profile(c["user"].id)


def test_worker_write_queues_refresh_on_owner_thread(mirror_context, qtbot, monkeypatch):
    c = mirror_context
    c["client"].response = None
    owner = threading.get_ident()
    called = []
    monkeypatch.setattr(c["sync"], "sync_now", lambda: called.append(threading.get_ident()))
    worker = threading.Thread(
        target=lambda: c["remote"].save_profile(c["user"].id, {"display_name": "云端"})
    )
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert called == []
    qtbot.waitUntil(lambda: bool(called))
    assert called == [owner]


def test_inflight_snapshot_from_before_write_cannot_restore_stale_cache(
    mirror_context, qtbot, monkeypatch
):
    c = mirror_context
    entered, release = threading.Event(), threading.Event()
    original = deepcopy(c["snapshot"])
    new = deepcopy(original)
    new["revision"] = "two"
    new["resources"]["profile"]["display_name"] = "最新称呼"
    fetched = []

    def fetch(_actor):
        fetched.append(True)
        if len(fetched) == 1:
            entered.set()
            release.wait(3)
            return original
        return new

    monkeypatch.setattr(c["sync"], "_fetch_snapshot", fetch)
    c["sync"].sync_now()
    qtbot.waitUntil(entered.is_set)
    c["client"].response = None
    c["remote"].save_profile(c["user"].id, {"display_name": "最新称呼"})
    accepted = []
    c["sync"].snapshot_changed.connect(lambda value: accepted.append(value["revision"]))
    release.set()
    qtbot.waitUntil(lambda: accepted == ["two"] and not c["sync"].is_syncing)
    assert c["remote"].get_profile(c["user"].id)["display_name"] == "最新称呼"


def test_remote_permission_error_immediately_revokes_all_wrapped_cache(mirror_context):
    c = mirror_context
    c["client"].response = CloudAPIError("permission", status_code=403)
    with pytest.raises(CloudAPIError):
        c["remote"].save_profile(c["user"].id, {"display_name": "无权限"})
    assert not c["sync"].mirror.authorized
    with pytest.raises(CloudAPIError):
        c["remote"].get_profile(c["user"].id)
    assert len(c["client"].calls) == 1


def test_management_filters_preserve_scope_and_order(mirror_context):
    c = mirror_context
    actor = c["user"].id
    c["sync"].actor_role = "operator"
    value = deepcopy(c["snapshot"])
    value["resources"]["members"] = [{"id": 9}, {"id": 10}]
    value["resources"]["advisors"] = [{"id": 3}]
    value["resources"]["appointments"] = [
        {"id": 4, "member_user_id": 9, "status": "pending"},
        {"id": 1, "member_user_id": 10, "status": "disabled"},
        {"id": 5, "member_user_id": 9, "status": "pending"},
    ]
    c["sync"]._accept(c["sync"].cache_generation, actor, value)
    remote = SyncedRemoteService(RemoteServiceManagementService(c["client"]), c["sync"])
    assert remote.list_advisors(actor) == [{"id": 3}]
    assert remote.list_members(actor) == [{"id": 9}, {"id": 10}]
    assert [
        row["id"]
        for row in remote.list_visit_tasks(actor, member_user_id=9, status="pending", limit=1)
    ] == [4]
    with pytest.raises(CloudAPIError) as error:
        remote.list_visit_tasks(actor, member_user_id=999)
    assert error.value.code == "network"  # Must ask server, not claim a permitted empty result.


def test_operator_target_ids_are_not_mistaken_for_the_authenticated_actor(mirror_context):
    c = mirror_context
    c["sync"].actor_role = "operator"
    c["client"].response = 777
    remote = SyncedRemoteService(RemoteServiceManagementService(c["client"]), c["sync"])
    assert remote.bind_advisor(c["user"].id, 17, 23) == 777
    assert c["client"].calls[-1][2] == [c["user"].id, 17, 23]


def test_future_read_filter_is_not_silently_ignored_by_mirror(mirror_context):
    c = mirror_context

    class ExtendedRemote(RemoteHealthService):
        def get_profile(self, user_id, *, future_filter=None):
            assert future_filter == "remote-only"
            return {"user_id": user_id, "future_filter": future_filter}

    remote = SyncedRemoteService(ExtendedRemote(c["client"]), c["sync"])
    assert (
        remote.get_profile(c["user"].id, future_filter="remote-only")["future_filter"]
        == "remote-only"
    )


def test_uncertain_remote_write_also_stops_stale_snapshot_reads(mirror_context):
    c = mirror_context
    c["client"].response = CloudAPIError("network", outcome_uncertain=True)
    with pytest.raises(CloudAPIError):
        c["remote"].save_profile(c["user"].id, {"display_name": "不确定是否已提交"})
    with pytest.raises(SyncError):
        c["sync"].mirror.snapshot()


def test_advisor_cannot_read_rows_from_a_different_binding_in_mirror(mirror_context):
    c = mirror_context
    c["sync"].actor_role = "advisor"
    remote = SyncedRemoteService(RemoteServiceManagementService(c["client"]), c["sync"])
    # The fixture member belongs to advisor 4, not the current advisor identity 1.
    with pytest.raises(CloudAPIError) as error:
        remote.list_members(c["user"].id)
    assert error.value.code == "permission"
    assert not c["sync"].mirror.authorized and c["client"].calls == []
