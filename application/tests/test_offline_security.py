from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError
from ollama_chat_app.services.cloud_sync import CloudMirrorStore, SyncError, SyncIdentity
from ollama_chat_app.services.offline_access import (
    OfflineAccessError,
    OfflineAccessStore,
    validate_lease,
)
from ollama_chat_app.services.offline_outbox import OfflineOutbox, content_version

ENDPOINT = "https://synthetic.example.com/aliyun"
USER = {"id": 1, "username": "synthetic-user", "username_normalized": "synthetic-user",
        "created_at": "2026-09-08T00:00:00Z", "last_login_at": None,
        "role_code": "member", "account_status": "active", "updated_at": None}
PASSWORD = "Synthetic-Only-Password-2026!"


def lease(now=None, **patch):
    now = now or datetime.now(UTC)
    return {"version": 1, "actor_id": 1, "role_code": "member",
            "server_instance_id": "synthetic-instance-1234", "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=2)).isoformat(), **patch}


def test_offline_verifier_is_encrypted_scoped_and_revocable(tmp_path):
    now = datetime.now(UTC)
    store = OfflineAccessStore(tmp_path, clock=lambda: now)
    store.enroll(ENDPOINT, USER, PASSWORD, lease(now))
    raw = next(tmp_path.glob("*.offline")).read_bytes()
    for forbidden in (PASSWORD.encode(), b"$argon2", USER["username"].encode()):
        assert forbidden not in raw
    assert store.authenticate(ENDPOINT, USER["username"], PASSWORD)[0] == USER
    with pytest.raises(OfflineAccessError):
        store.authenticate(ENDPOINT.replace("aliyun", "supabase"), USER["username"], PASSWORD)
    store.revoke(ENDPOINT, USER["username"])
    with pytest.raises(OfflineAccessError):
        store.authenticate(ENDPOINT, USER["username"], PASSWORD)


def test_offline_expiry_equality_clock_rollback_and_password_lockout(tmp_path):
    now = datetime.now(UTC)
    clock = [now]
    store = OfflineAccessStore(tmp_path, clock=lambda: clock[0])
    store.enroll(ENDPOINT, USER, PASSWORD, lease(now))
    for _ in range(5):
        with pytest.raises(OfflineAccessError):
            store.authenticate(ENDPOINT, USER["username"], "wrong")
    with pytest.raises(OfflineAccessError):
        store.authenticate(ENDPOINT, USER["username"], PASSWORD)
    clock[0] += timedelta(minutes=6)
    store.authenticate(ENDPOINT, USER["username"], PASSWORD)
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(OfflineAccessError):
        store.authenticate(ENDPOINT, USER["username"], PASSWORD)
    clock[0] = now + timedelta(hours=2)
    with pytest.raises(OfflineAccessError):
        store.authenticate(ENDPOINT, USER["username"], PASSWORD)


@pytest.mark.parametrize("patch", [
    {"version": True}, {"actor_id": True}, {"actor_id": 2},
    {"role_code": "admin"}, {"server_instance_id": "x/" * 10},
    {"expires_at": (datetime.now(UTC) + timedelta(hours=13)).isoformat()},
])
def test_lease_rejects_invalid_metadata(patch):
    with pytest.raises(OfflineAccessError):
        validate_lease(lease(**patch), actor_id=1)


def test_network_only_offline_fallback_has_no_online_bearer(tmp_path, monkeypatch):
    monkeypatch.setattr(CloudAPIClient, "_on_gui_thread", staticmethod(lambda: False))
    store = OfflineAccessStore(tmp_path)
    now = datetime.now(UTC)
    store.enroll(ENDPOINT, USER, PASSWORD, lease(now))
    status = ["network"]

    def handler(request):
        if status[0] == "network":
            raise httpx.ConnectError("synthetic failure", request=request)
        return httpx.Response(401, json={"detail": "not authorized"})

    with CloudAPIClient(ENDPOINT, offline_store=store,
                        transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CloudAPIError):
            client.login(USER["username"], PASSWORD)
        client.login(USER["username"], PASSWORD, allow_offline=True)
        assert client.is_authenticated and client.is_offline_session
        assert not client.is_online_authenticated and client._token is None
        with pytest.raises(CloudAPIError) as caught:
            client.me()
        assert caught.value.code == "offline"
        status[0] = "401"
        with pytest.raises(CloudAPIError) as caught:
            client.login(USER["username"], PASSWORD, allow_offline=True)
        assert caught.value.code == "authentication"
        assert not client.is_authenticated
        with pytest.raises(OfflineAccessError):
            store.authenticate(ENDPOINT, USER["username"], PASSWORD)


def test_outbox_ciphertext_ownership_and_uncertain_uuid_is_preserved(tmp_path):
    identity = SyncIdentity(ENDPOINT, "synthetic-instance-1234", 1)
    outbox = OfflineOutbox(tmp_path)
    queued = outbox.enqueue(identity, "health", "add_life_record", [1],
                            {"content": "SYNTHETIC-PRIVATE-INTENT"}, base_version=None)
    assert b"SYNTHETIC-PRIVATE-INTENT" not in next(tmp_path.glob("*.outbox")).read_bytes()
    assert "kwargs" not in outbox.list(identity)[0]
    assert outbox.list(SyncIdentity(ENDPOINT, identity.server_instance_id, 2)) == []
    outbox.set_status(identity, queued.operation_id, "uncertain", "timeout")
    with pytest.raises(CloudAPIError):
        outbox.discard(identity, queued.operation_id)
    assert outbox.list(identity)[0]["operation_id"] == queued.operation_id
    outbox.set_status(identity, queued.operation_id, "conflict")
    outbox.discard(identity, queued.operation_id)
    assert outbox.list(identity) == []


@pytest.mark.parametrize("service,method,args,kwargs,base", [
    ("health", "delete_life_record", [1, 2], {}, None),
    ("health", "add_life_record", [2], {}, None),
    ("preferences", "update_preferences", [1], {"patch": {"ai_context_consent": True}}, "0"*64),
    ("preferences", "update_preferences", [1], {"patch": {"api_key": "private"}}, "0"*64),
    ("health", "save_profile", [1], {}, None),
])
def test_queue_allowlist_identity_consent_and_version(
    service, method, args, kwargs, base, tmp_path
):
    with pytest.raises(CloudAPIError):
        OfflineOutbox(tmp_path).enqueue(SyncIdentity(ENDPOINT, "synthetic-instance-1234", 1),
                                       service, method, args, kwargs, base_version=base)


def test_content_versions_are_canonical_and_detect_changes():
    assert content_version({"b": 2, "a": 1}) == content_version({"a": 1, "b": 2})
    assert content_version({"a": 1}) != content_version({"a": 2})


def test_mirror_absolute_expiry_and_lease_match(tmp_path):
    from test_cloud_sync_migration import snapshot

    value = lease()
    identity = SyncIdentity(ENDPOINT, value["server_instance_id"], 1)
    store = CloudMirrorStore(tmp_path)
    store.activate(identity, value)
    store.apply_snapshot(snapshot(instance=identity.server_instance_id))
    assert b"display_name" not in store.path.read_bytes()
    reopened = CloudMirrorStore(tmp_path)
    reopened.authorize_offline(identity, value)
    assert reopened.snapshot()["actor_id"] == 1
    altered = json.loads(json.dumps(value))
    altered["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with pytest.raises(SyncError):
        CloudMirrorStore(tmp_path).authorize_offline(identity, altered)
    reopened._expires_at = datetime.now(UTC)
    with pytest.raises(SyncError):
        reopened.snapshot()
    assert not reopened.authorized
