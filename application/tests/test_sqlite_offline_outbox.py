"""Synthetic DPAPI + SQLite queue safety, no cloud or existing user state."""

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import httpx
import pytest
from test_offline_security import PASSWORD, USER, lease
from test_offline_sync_queue import coordinator

from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc
from ollama_chat_app.services.cloud_sync import CloudMirrorStore, CloudSyncService, SyncIdentity
from ollama_chat_app.services.offline_access import OfflineAccessStore
from ollama_chat_app.services.offline_outbox import OfflineOutbox
from ollama_chat_app.services.sqlite_outbox import SqliteOfflineOutbox

ENDPOINT = "https://synthetic.example.com/aliyun"
IDENTITY = SyncIdentity(ENDPOINT, "synthetic-instance-1234", 1)
VERSION = "a" * 64


def append(queue, content, identity=IDENTITY):
    return queue.enqueue(
        identity,
        "health",
        "add_life_record",
        [identity.actor_id],
        {"content": content},
        base_version=None,
    )


def test_queue_ciphertext_is_in_same_mirror_database_and_identity_isolated(tmp_path):
    queue = SqliteOfflineOutbox(tmp_path)
    result = append(queue, "SYNTHETIC-PRIVATE-FACT")
    path = queue.database_path(IDENTITY)
    assert path.name == f"cloud-{IDENTITY.cache_key}.sqlite"
    assert b"SYNTHETIC-PRIVATE-FACT" not in path.read_bytes()
    assert not list((tmp_path / "outbox").glob("*.outbox"))
    assert queue.list(IDENTITY)[0]["operation_id"] == result.operation_id
    assert "kwargs" not in queue.list(IDENTITY)[0]
    for other in (
        replace(IDENTITY, actor_id=2),
        replace(IDENTITY, endpoint=ENDPOINT + "-other"),
        replace(IDENTITY, server_instance_id="other-instance-1234"),
    ):
        assert queue.list(other) == []
        with closing(sqlite3.connect(path)) as connection:
            encrypted = connection.execute("SELECT payload FROM offline_intents").fetchone()[0]
        with closing(sqlite3.connect(queue.database_path(other))) as connection, connection:
            connection.execute("UPDATE offline_intents SET payload=?", (encrypted,))
        with pytest.raises(CloudAPIError):
            queue.list(other)


def test_legacy_import_once_retains_ciphertext_and_does_not_resurrect_ack(tmp_path):
    legacy = OfflineOutbox(tmp_path / "outbox")
    first = append(legacy, "SYNTHETIC-LEGACY")
    source = next((tmp_path / "outbox").glob("*.outbox"))
    original = source.read_bytes()
    queue = SqliteOfflineOutbox(tmp_path)
    assert queue.list(IDENTITY)[0]["operation_id"] == first.operation_id
    queue.set_status(IDENTITY, first.operation_id, "applied")
    assert source.read_bytes() == original
    assert SqliteOfflineOutbox(tmp_path).list(IDENTITY) == []
    with closing(sqlite3.connect(queue.database_path(IDENTITY))) as connection:
        assert connection.execute("SELECT count(*) FROM offline_intents").fetchone()[0] == 1


def test_two_instances_and_threads_cannot_lose_read_modify_write_updates(tmp_path):
    first, second = SqliteOfflineOutbox(tmp_path), SqliteOfflineOutbox(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(append, first if index % 2 else second, f"SYN-{index}")
            for index in range(24)
        ]
        identifiers = {future.result(timeout=20).operation_id for future in futures}
    rows = first.list(IDENTITY, include_payload=True)
    assert len(rows) == len(identifiers) == 24
    assert {row["operation_id"] for row in rows} == identifiers
    assert {row["kwargs"]["content"] for row in rows} == {f"SYN-{n}" for n in range(24)}


def test_process_lock_serializes_independent_desktop_instances(tmp_path):
    script = """
import sys
from ollama_chat_app.services.cloud_sync import SyncIdentity
from ollama_chat_app.services.sqlite_outbox import SqliteOfflineOutbox
queue = SqliteOfflineOutbox(sys.argv[1])
identity = SyncIdentity('https://synthetic.example.com/aliyun', 'synthetic-instance-1234', 1)
for index in range(6):
    queue.enqueue(identity, 'health', 'add_life_record', [1],
                  {'content': sys.argv[2] + str(index)}, base_version=None)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(number)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        for number in range(2)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert not stdout
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()  # Only a still-running synthetic helper created above.
                process.wait(timeout=5)
    assert len(SqliteOfflineOutbox(tmp_path).list(IDENTITY)) == 12


@pytest.mark.parametrize("method", ["save_profile", "update_preferences"])
def test_never_attempted_updates_coalesce_but_attempted_payload_never_mutates(tmp_path, method):
    queue = SqliteOfflineOutbox(tmp_path)
    service, field = ("health", "values") if method == "save_profile" else ("preferences", "patch")
    first = queue.enqueue(
        IDENTITY,
        service,
        method,
        [1],
        {field: {"nickname": "first"}},
        base_version=VERSION,
        coalesce=True,
    )
    merged = queue.enqueue(
        IDENTITY,
        service,
        method,
        [1],
        {field: {"preferred_name": "second"}},
        base_version=VERSION,
        coalesce=True,
    )
    assert first.operation_id == merged.operation_id
    queue.mark_attempted(IDENTITY, first.operation_id)
    before = queue.list(IDENTITY, include_payload=True)[0]
    third = queue.enqueue(
        IDENTITY,
        service,
        method,
        [1],
        {field: {"nickname": "third"}},
        base_version=VERSION,
        coalesce=True,
    )
    assert third.operation_id != first.operation_id
    assert queue.list(IDENTITY, include_payload=True)[0] == before


def test_attempted_queued_operation_cannot_discard_an_inflight_uuid(tmp_path):
    queue = SqliteOfflineOutbox(tmp_path)
    item = append(queue, "SYNTHETIC-INFLIGHT")
    queue.mark_attempted(IDENTITY, item.operation_id)
    with pytest.raises(CloudAPIError):
        queue.discard(IDENTITY, item.operation_id)
    assert queue.list(IDENTITY)[0]["operation_id"] == item.operation_id
    queue.set_status(IDENTITY, item.operation_id, "conflict", "conflict")
    queue.discard(IDENTITY, item.operation_id)  # Definitive refusal is safe to abandon.
    assert not queue.list(IDENTITY)


def test_replay_atomically_claims_latest_coalesced_payload(qtbot, tmp_path, monkeypatch):
    sync = coordinator(tmp_path)
    item = sync.submit_intent(
        "preferences", "update_preferences", [1], {"patch": {"nickname": "original"}}
    )
    other = SqliteOfflineOutbox(tmp_path)
    claim = sync.outbox.mark_attempted

    def competing_edit_then_claim(identity, identifier):
        pending = sync.get_pending(identifier)
        changed = other.enqueue(
            identity,
            "preferences",
            "update_preferences",
            [1],
            {"patch": {"nickname": "newer"}},
            base_version=pending["base_version"],
            coalesce=True,
        )
        assert changed.operation_id == item.operation_id
        return claim(identity, identifier)

    monkeypatch.setattr(sync.outbox, "mark_attempted", competing_edit_then_claim)
    sync._state = "online"
    sync._send_intent(sync.identity, {"operation_id": item.operation_id})
    sent_intent = decode_rpc(sync.client.calls[-1][2])[1]
    assert sent_intent["kwargs"]["patch"]["nickname"] == "newer"
    assert not sync.pending_operations()


@pytest.mark.parametrize(
    ("service", "method", "args", "kwargs"),
    [
        ("health", "add_life_record", [2], {"content": "wrong actor"}),
        ("health", "delete_life_record", [1, 2], {}),
        ("health", "add_life_record", [1], {"password": "never persisted"}),
        ("preferences", "update_preferences", [1], {"patch": {"ai_context_consent": True}}),
    ],
)
def test_new_storage_preserves_permission_and_sensitive_field_gates(
    tmp_path, service, method, args, kwargs
):
    queue = SqliteOfflineOutbox(tmp_path)
    with pytest.raises(CloudAPIError):
        queue.enqueue(IDENTITY, service, method, args, kwargs, base_version=VERSION)
    assert not queue.list(IDENTITY)


def test_current_online_lease_enables_queue_without_persistent_offline_signin(
    qtbot, tmp_path, monkeypatch
):
    from test_cloud_sync_migration import snapshot

    from ollama_chat_app.services.offline_outbox import QueuedOperation

    monkeypatch.setattr(CloudAPIClient, "_on_gui_thread", staticmethod(lambda: False))
    private = tmp_path / "offline-login"
    store = OfflineAccessStore(private)

    def handler(request):
        assert request.url.path == "/aliyun/v1/auth/login"
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-token-123456789",
                "token_type": "bearer",
                "user": USER,
                "offline_lease": lease(),
                "sync_protocol": 2,
            },
        )

    with CloudAPIClient(
        ENDPOINT, offline_store=store, transport=httpx.MockTransport(handler)
    ) as client:
        client.login(USER["username"], PASSWORD, remember_offline=False)
        assert client.offline_lease is not None
        assert not client.offline_opt_in and not private.exists()
        sync = CloudSyncService(client, CloudMirrorStore(tmp_path / "mirror"))
        assert sync.offline_enabled
        sync.actor_id, sync.actor_role = 1, "member"
        sync.identity = IDENTITY
        sync.mirror.activate(IDENTITY, client.offline_lease)
        sync.mirror.apply_snapshot(snapshot(instance=IDENTITY.server_instance_id))
        sync._state = "offline"
        queued = sync.submit_intent(
            "health", "add_life_record", [1], {"content": "SYNTHETIC-CURRENT-SESSION"}
        )
        assert isinstance(queued, QueuedOperation)
        assert len(sync.pending_operations()) == 1 and not private.exists()
        client.clear_session()
        assert not sync.offline_enabled and not client.is_authenticated
        with pytest.raises(CloudAPIError):
            sync.submit_intent("health", "add_life_record", [1], {"content": "MUST-NOT-QUEUE"})
        assert len(sync.outbox.list(IDENTITY)) == 1
        assert not private.exists()


@pytest.mark.parametrize(
    "later_error",
    [
        None,
        CloudAPIError("conflict", status_code=409, conflict_kind="base_version"),
        CloudAPIError("validation", status_code=422),
        CloudAPIError("not_found", status_code=404),
    ],
)
def test_lost_local_ack_after_cloud_success_replays_same_uuid_after_restart(
    qtbot, tmp_path, monkeypatch, later_error
):
    from ollama_chat_app.services.cloud_rpc_codec import encode_rpc

    sync = coordinator(tmp_path)
    queued = sync.submit_intent("health", "add_life_record", [1], {"content": "SYNTHETIC-ONCE"})
    journal, calls, executed = {}, [], []

    def idempotent_server(_service, _method, args, _kwargs, *, request_id):
        intent = decode_rpc(args)[1]
        calls.append((request_id, intent))
        if len(calls) == 2 and later_error is not None:
            # The server may no longer have the outcome journal. A later
            # refusal is not proof the first operation never committed.
            raise later_error
        if request_id not in journal:
            executed.append(intent)
            journal[request_id] = encode_rpc(901)
        return journal[request_id]

    original_status = sync.outbox.set_status

    def crash_before_local_ack(identity, identifier, status, error_code=None):
        if status == "applied":
            raise RuntimeError("synthetic process loss after server commit")
        return original_status(identity, identifier, status, error_code)

    monkeypatch.setattr(sync.client, "rpc", idempotent_server)
    monkeypatch.setattr(sync.outbox, "set_status", crash_before_local_ack)
    sync._state = "online"
    with pytest.raises(RuntimeError, match="synthetic process loss"):
        sync._replay_pending(1)
    retained = sync.get_pending(queued.operation_id)
    assert retained["attempted"] is True and retained["status"] == "queued"
    restarted = coordinator(tmp_path)
    monkeypatch.setattr(restarted.client, "rpc", idempotent_server)
    restarted._state = "online"
    restarted._replay_pending(1)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0] == queued.operation_id and len(executed) == 1
    if later_error is None:
        assert restarted.pending_operations() == []
    else:
        assert restarted.get_pending(queued.operation_id)["status"] == "uncertain"
        with pytest.raises(CloudAPIError):
            restarted.rebase_pending(queued.operation_id)
        with pytest.raises(CloudAPIError):
            restarted.discard_pending(queued.operation_id)


def test_corrupt_queue_never_becomes_empty_or_reimports_legacy(tmp_path):
    legacy = OfflineOutbox(tmp_path / "outbox")
    append(legacy, "SYNTHETIC-RECOVERY")
    queue = SqliteOfflineOutbox(tmp_path)
    queue.list(IDENTITY)
    with closing(sqlite3.connect(queue.database_path(IDENTITY))) as connection, connection:
        connection.execute("UPDATE offline_intents SET payload='corrupt synthetic marker'")
    with pytest.raises(CloudAPIError):
        SqliteOfflineOutbox(tmp_path).list(IDENTITY)
    with closing(sqlite3.connect(queue.database_path(IDENTITY))) as connection:
        assert connection.execute("SELECT payload FROM offline_intents").fetchone()[0] == (
            "corrupt synthetic marker"
        )
    assert list((tmp_path / "outbox").glob("*.outbox"))


def test_queue_rejects_existing_hardlink_instead_of_touching_another_file(tmp_path):
    from ollama_chat_app.security.private_payload import PrivateStateError

    queue = SqliteOfflineOutbox(tmp_path)
    append(queue, "SYNTHETIC-HARDLINK")
    database = queue.database_path(IDENTITY)
    alias = tmp_path / "owned-synthetic-alias.sqlite"
    os.link(database, alias)
    before = alias.read_bytes()
    with pytest.raises(PrivateStateError):
        append(queue, "MUST-NOT-BE-WRITTEN")
    assert alias.read_bytes() == before
