from __future__ import annotations

import hashlib

import pytest
from test_cloud_sync_migration import ENDPOINT, INSTANCE, snapshot
from test_offline_security import lease

from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.cloud_sync import CloudMirrorStore, CloudSyncService, SyncIdentity
from ollama_chat_app.services.offline_outbox import QueuedOperation
from ollama_chat_app.services.sync_files import CHUNK_BYTES, SyncFileCache
from ollama_chat_app.services.sync_protocol import (
    RESOURCE_KINDS,
    decode_large_snapshot,
    encode_large_snapshot,
)


class IntentClient:
    base_url = ENDPOINT
    is_authenticated = True
    is_online_authenticated = True
    offline_store = object()
    offline_opt_in = True
    offline_lease = None

    def __init__(self):
        self.calls = []
        self.fail = None

    def me(self):
        return {"id": 1, "role_code": "member"}

    def rpc(self, service, method, args, kwargs, **options):
        self.calls.append((service, method, args, options))
        if self.fail:
            raise self.fail
        return encode_rpc(901)


def coordinator(tmp_path):
    client = IntentClient()
    client.offline_lease = lease(server_instance_id=INSTANCE)
    sync = CloudSyncService(client, CloudMirrorStore(tmp_path))
    sync.actor_id, sync.actor_role = 1, "member"
    sync.identity = SyncIdentity(ENDPOINT, INSTANCE, 1)
    sync.mirror.activate(sync.identity, client.offline_lease)
    sync.mirror.apply_snapshot(snapshot())
    sync._state = "offline"
    return sync


def test_offline_queue_has_no_remote_write_and_replay_keeps_uuid(qtbot, tmp_path):
    sync = coordinator(tmp_path)
    result = sync.submit_intent("health", "add_life_record", [1], {"content": "synthetic"})
    assert isinstance(result, QueuedOperation) and sync.client.calls == []
    assert len(sync.pending_operations()) == 1
    sync._state = "online"
    sync.client.fail = CloudAPIError("timeout", outcome_uncertain=True)
    with pytest.raises(CloudAPIError):
        sync._replay_pending(1)
    assert sync.pending_operations()[0]["status"] == "uncertain"
    with pytest.raises(CloudAPIError):
        sync.discard_pending(result.operation_id)
    sync.client.fail = None
    sync._replay_pending(1)
    assert {call[3]["request_id"] for call in sync.client.calls} == {result.operation_id}
    assert sync.pending_operations() == []
    assert sync.mirror._fresh is False


def test_conflict_preserved_until_explicit_rebase_and_identity_guard(qtbot, tmp_path):
    sync = coordinator(tmp_path)
    queued = sync.submit_intent("health", "save_profile", [1], {"values": {"display_name": "x"}})
    sync._state = "online"
    sync.client.fail = CloudAPIError("conflict", status_code=409, conflict_kind="base_version")
    sync._replay_pending(1)
    assert sync.get_pending(queued.operation_id)["status"] == "conflict"
    rebased = sync.rebase_pending(queued.operation_id)
    assert rebased.operation_id != queued.operation_id
    assert sync.get_pending(queued.operation_id) is None
    sync.actor_id = 2
    with pytest.raises(CloudAPIError):
        sync.pending_operations()


def test_remote_appointment_queue_flattens_only_variadic_keywords(qtbot, tmp_path):
    from ollama_chat_app.services.remote_services import (
        RemoteAppointmentService,
        RemoteServiceManagementService,
    )
    from ollama_chat_app.services.synced_remote import SyncedRemoteService

    sync = coordinator(tmp_path)
    wrapped = SyncedRemoteService(RemoteServiceManagementService(sync.client), sync)
    appointment = RemoteAppointmentService(sync.client, management=wrapped)
    result = appointment.create_request(1, "合成上门服务", notes="合成备注")
    assert isinstance(result, QueuedOperation)
    item = sync.get_pending(result.operation_id)
    assert "values" not in item["kwargs"]
    assert item["kwargs"]["service_type"] == "合成上门服务"
    assert item["args"] == [1] and not sync.client.calls


@pytest.mark.parametrize("error", [
    CloudAPIError("conflict", status_code=409, conflict_kind="idempotency"),
    CloudAPIError("conflict", status_code=409, conflict_kind="base_version"),
    CloudAPIError("conflict", status_code=409),
    CloudAPIError("validation", status_code=422),
    CloudAPIError("not_found", status_code=404),
])
def test_uncertain_never_becomes_rebasable_after_later_failure(qtbot, tmp_path, error):
    sync = coordinator(tmp_path)
    queued = sync.submit_intent("health", "add_life_record", [1], {"content": "synthetic"})
    sync.outbox.set_status(sync.identity, queued.operation_id, "uncertain", "timeout")
    sync._state, sync.client.fail = "online", error
    sync._replay_pending(1)
    assert sync.get_pending(queued.operation_id)["status"] == "uncertain"
    with pytest.raises(CloudAPIError):
        sync.rebase_pending(queued.operation_id)
    with pytest.raises(CloudAPIError):
        sync.discard_pending(queued.operation_id)
    assert len(sync.pending_operations()) == 1


def test_unknown_journal_conflict_is_uncertain_even_before_ack_was_recorded(qtbot, tmp_path):
    sync = coordinator(tmp_path)
    queued = sync.submit_intent("health", "add_life_record", [1], {"content": "synthetic"})
    sync._state = "online"
    sync.client.fail = CloudAPIError("conflict", status_code=409, conflict_kind="idempotency")
    sync._replay_pending(1)
    assert sync.get_pending(queued.operation_id)["status"] == "uncertain"
    with pytest.raises(CloudAPIError):
        sync.rebase_pending(queued.operation_id)


@pytest.mark.parametrize("code,status", [("conflict", 409), ("validation", 422),
                                         ("not_found", 404), ("protocol", 200)])
def test_nontransport_snapshot_failure_locks_old_scope(qtbot, tmp_path, code, status):
    from ollama_chat_app.services.cloud_sync import SyncError

    sync = coordinator(tmp_path)
    lost = []
    sync.authorization_lost.connect(lambda: lost.append(True))
    sync._failed(sync.cache_generation, CloudAPIError(code, status_code=status))
    assert not sync.mirror.authorized and not sync.mirror._fresh
    assert sync.authorization_blocked and len(lost) == 1
    with pytest.raises((SyncError, CloudAPIError)):
        sync.authorized_snapshot(1, ENDPOINT)


def test_committed_lost_ack_scope_change_cannot_duplicate_via_rebase(qtbot, tmp_path):
    """Exercise the real journal, not only a mocked generic HTTP conflict."""
    import sys
    from pathlib import Path

    from ollama_chat_app.services.chat import ChatService
    from ollama_chat_app.services.cloud_rpc_codec import decode_rpc
    from ollama_chat_app.services.health import HealthService
    from ollama_chat_app.services.preferences import PreferencesService
    from ollama_chat_app.services.service_management import ServiceManagementService

    # Reuse a fully isolated :memory: schema, never the active desktop Database.
    sys.path.insert(0, str(Path(__file__).parent / "server"))
    try:
        from platform_test_support import PEPPER, SyntheticDatabase

        from server.platform_rpc import RpcDispatcher, RpcError
        from server.platform_sync import PlatformSyncService
    finally:
        sys.path.pop(0)
    database = SyntheticDatabase()
    try:
        actor = database.account("synthetic-lost-ack")
        advisor = database.account("synthetic-new-binding", role="advisor")
        services = {"health": HealthService(database), "preferences": PreferencesService(database),
                    "chat": ChatService(database),
                    "service_management": ServiceManagementService(database)}
        services["sync"] = PlatformSyncService(database, services, PEPPER)
        dispatcher = RpcDispatcher(database, services)
        sync = coordinator(tmp_path)
        assert actor == 1
        queued = sync.submit_intent("health", "add_life_record", [actor], {
            "category": "water", "occurred_at": "2026-09-08T10:00:00+08:00",
            "content": "synthetic exactly once",
        })

        def rpc(service, method, args, kwargs, **options):
            try:
                return dispatcher.invoke(actor, service, method, {
                    "args": args, "kwargs": kwargs, "request_id": options["request_id"],
                })["result"]
            except RpcError as error:
                raise CloudAPIError("conflict", status_code=error.status,
                                     conflict_kind="idempotency") from None

        item = sync.get_pending(queued.operation_id)
        intent = {key: item[key] for key in ("service", "method", "args", "kwargs", "base_version")}
        decode_rpc(rpc("sync", "apply_queued", encode_rpc([actor, intent]), {},
                       request_id=queued.operation_id))
        sync.outbox.set_status(sync.identity, queued.operation_id, "uncertain", "timeout")
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO advisor_bindings(member_user_id,advisor_user_id,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?)",
                (actor, advisor, "active", "2026-09-08T00:00:00+00:00",
                 "2026-09-08T00:00:00+00:00"),
            )
        sync.client.rpc = rpc
        sync._state = "online"
        sync._replay_pending(actor)
        assert sync.get_pending(queued.operation_id)["status"] == "uncertain"
        with pytest.raises(CloudAPIError):
            sync.rebase_pending(queued.operation_id)
        assert len(services["health"].list_life_records(actor)) == 1
    finally:
        database.close()


def test_large_encrypted_snapshot_encoding_does_not_reuse_whole_rpc_budget(tmp_path):
    resources = {name: [] if kind == "list" else {} for name, kind in RESOURCE_KINDS.items()}
    resources["life_records"] = [{"id": index, "user_id": 1, "content": "x" * 9000}
                                  for index in range(1005)]
    value = {"schema_version": 2, "actor_id": 1, "server_instance_id": INSTANCE,
             "revision": "synthetic-large", "complete": True, "resources": resources,
             "excluded": {}}
    encoded = encode_large_snapshot(value)
    assert decode_large_snapshot(encoded) == value
    mirror = CloudMirrorStore(tmp_path)
    mirror.activate(SyncIdentity(ENDPOINT, INSTANCE, 1))
    mirror.apply_snapshot(value)
    assert len(mirror.snapshot()["resources"]["life_records"]) == 1005


@pytest.mark.parametrize("tamper", [False, True])
def test_chunks_encrypted_and_never_publish_partial_or_corrupt_files(tmp_path, tamper):
    content = b"synthetic-not-real-attachment-" * 5000
    digest = hashlib.sha256(content).hexdigest()
    metadata = {"id": 1, "sha256": digest, "size_bytes": len(content)}
    identity = SyncIdentity(ENDPOINT, INSTANCE, 1)
    resources = {"user_files": [metadata], "chat_attachments": []}

    class Client:
        def rpc(self, service, method, args, kwargs):
            offset = args[-1]
            block = content[offset:offset + CHUNK_BYTES]
            if tamper:
                block = b"x" + block[1:]
            return encode_rpc({"kind": "user_files", "id": 1, "sha256": digest,
                               "size_bytes": len(content), "offset": offset,
                               "next_offset": offset + len(block), "content": block})

    cache = SyncFileCache(tmp_path)
    if tamper:
        with pytest.raises(ValueError):
            cache.fetch(Client(), identity, resources)
        assert cache.read(identity, "user_files", metadata) is None
        assert not list(tmp_path.rglob("*.attachment"))
    else:
        cache.fetch(Client(), identity, resources)
        assert cache.read(identity, "user_files", metadata) == content
        assert b"synthetic-not-real" not in next(tmp_path.rglob("*.attachment")).read_bytes()
        other = SyncIdentity(ENDPOINT, INSTANCE, 2)
        assert cache.read(other, "user_files", metadata) is None
