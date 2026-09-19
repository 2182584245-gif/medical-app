from __future__ import annotations

import copy
import json

import httpx
import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthenticationError, AuthService
from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError
from ollama_chat_app.services.developer import (
    GRANT_KEY,
    LocalDeveloperService,
    RemoteDeveloperService,
    provision_local_developer,
)
from ollama_chat_app.services.developer_settings import (
    DeveloperError,
    default_snapshot,
    validate_settings,
)


class MemorySecrets:
    def __init__(self):
        self.keys = {"deepseek_cloud": "sk-test-private-key"}
        self.persistent = {}

    def get_default_api_key(self, provider="deepseek_cloud"):
        return self.keys.get(provider)

    def set_default_api_key(self, key, provider="deepseek_cloud"):
        self.keys[provider] = key

    def get_api_key(self, *args):
        return None

    def get_persistent_api_key(self, user, provider="deepseek_cloud"):
        return self.persistent.get((user, provider))

    def save_persistent_api_key(self, user, key, provider="deepseek_cloud"):
        self.persistent[(user, provider)] = key


@pytest.fixture
def local(tmp_path):
    database = Database(tmp_path / "developer-test.sqlite3")
    auth = AuthService(database)
    operator = auth.bootstrap_operator("test_developer", "TestDeveloper@92026")
    member = auth.register("test_member", "TestMember@92026")
    secrets = MemorySecrets()
    provision_local_developer(database, operator.id)
    service = LocalDeveloperService(database, secrets)
    service.authenticate("test_developer", "TestDeveloper@92026")
    return service, database, auth, operator, member, secrets


def test_no_name_or_operator_backdoor(tmp_path):
    database = Database(tmp_path / "noprivilege.sqlite3")
    auth = AuthService(database)
    user = auth.bootstrap_operator("test_developer", "TestDeveloper@92026")
    service = LocalDeveloperService(database, MemorySecrets())
    with pytest.raises(DeveloperError, match="权限"):
        service.authenticate("test_developer", "TestDeveloper@92026")
    with pytest.raises(DeveloperError):
        service.list_users("member")
    provision_local_developer(database, user.id)
    assert service.authenticate("test_developer", "TestDeveloper@92026")["id"] == user.id


def test_grant_rechecked_and_session_closed(local):
    service, database, _auth, operator, _member, _secrets = local
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM app_settings WHERE setting_key = ? AND user_id = ?",
            (GRANT_KEY, operator.id),
        )
    with pytest.raises(DeveloperError):
        service.settings()


def test_read_all_known_tables_but_no_password_or_key(local):
    service, _database, _auth, _operator, member, _secrets = local
    details = service.user_details(member.id)
    raw = json.dumps(details)
    assert "password_hash" not in raw
    assert "sk-test" not in raw
    assert "conversations" in details["tables"]
    assert "user_preferences" in details["tables"]
    assert "messages" in details["tables"]


def test_password_reset_is_one_way_and_immediate(local):
    service, _database, auth, _operator, member, _secrets = local
    service.update_user(member.id, {"password": "ReplacedPassword@92026"})
    with pytest.raises(AuthenticationError):
        auth.authenticate("test_member", "TestMember@92026")
    assert auth.authenticate("test_member", "ReplacedPassword@92026").id == member.id
    with pytest.raises(DeveloperError):
        service.update_user(member.id, {"role_code": "operator"})


def test_settings_snapshot_and_default_key_freeze_until_new_service(local):
    service, database, _auth, _operator, _member, secrets = local
    initial = service.startup_snapshot
    settings = copy.deepcopy(initial["settings"])
    settings["ai"]["system_prompt"] = "使用清楚亲切的语言。"
    settings["features"]["commerce"] = False
    result = service.publish_settings(0, settings, "sk-another-test-private-key")
    assert result["revision"] == 1
    assert "api_key" not in result["settings"]["ai"]
    assert service.startup_snapshot == initial
    assert service.launch_secret_store.get_default_api_key() == "sk-test-private-key"
    next_launch = LocalDeveloperService(database, secrets)
    assert next_launch.startup_snapshot["settings"]["features"]["commerce"] is False
    assert next_launch.launch_secret_store.get_default_api_key() == "sk-another-test-private-key"
    with pytest.raises(DeveloperError, match="另一位"):
        service.publish_settings(0, settings)


def test_staged_chat_edit_activates_next_launch_and_never_changes_owner(local):
    service, database, _auth, _operator, member, secrets = local
    conversation = service.user_details(member.id)["tables"]["conversations"][0]
    result = service.update_record(
        member.id, "conversations", conversation["id"], {"title": "新标题"}
    )
    assert result["state"] == "pending"
    before = service.user_details(member.id)
    assert before["tables"]["conversations"][0]["title"] != "新标题"
    assert len(before["pending_changes"]) == 1
    next_launch = LocalDeveloperService(database, secrets)
    next_launch.authenticate("test_developer", "TestDeveloper@92026")
    assert next_launch.user_details(member.id)["tables"]["conversations"][0]["title"] == "新标题"
    with pytest.raises(DeveloperError):
        service.update_record(member.id, "conversations", conversation["id"], {"user_id": 100})
    with pytest.raises(DeveloperError):
        service.update_record(member.id + 999, "conversations", conversation["id"], {"title": "no"})


def test_staged_record_preserves_conflicting_member_edit(local):
    service, database, _auth, _operator, member, secrets = local
    row = service.user_details(member.id)["tables"]["conversations"][0]
    service.update_record(member.id, "conversations", row["id"], {"title": "开发者改名"})
    with database.transaction() as connection:
        connection.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", ("会员自行改名", row["id"])
        )
    next_launch = LocalDeveloperService(database, secrets)
    next_launch.authenticate("test_developer", "TestDeveloper@92026")
    details = next_launch.user_details(member.id)
    assert details["tables"]["conversations"][0]["title"] == "会员自行改名"
    assert details["pending_changes"][0]["state"] == "conflict"


def test_settings_reject_unknown_runtime_and_bad_json():
    settings = default_snapshot()["settings"]
    settings["runtime"]["command"] = "arbitrary shell"
    with pytest.raises(DeveloperError):
        validate_settings(settings)


def test_cloud_developer_token_separate_no_offline_enrollment():
    requests = []
    user = {
        "id": 1,
        "username": "developer",
        "role_code": "operator",
        "account_status": "active",
        "created_at": "2026-09-19T00:00:00Z",
        "username_normalized": "developer",
        "last_login_at": None,
        "updated_at": "2026-09-19T00:00:00Z",
    }

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "developer": True,
                "access_token": "x" * 32,
                "token_type": "bearer",
                "user": user,
            },
        )

    client = CloudAPIClient("https://example.org", transport=httpx.MockTransport(handle))
    try:
        assert client.developer_login("developer", "private")["id"] == 1
        assert requests[0].url.path == "/v1/developer/login"
        assert "authorization" not in requests[0].headers
        assert not client.is_offline_session
        assert client.offline_lease is None
        client.clear_session()
        assert not client.is_authenticated
    finally:
        client.close()


def test_cloud_cannot_accept_normal_login_as_developer():
    client = CloudAPIClient(
        "https://example.org",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"access_token": "x" * 32})
        ),
    )
    try:
        with pytest.raises(CloudAPIError):
            client.developer_login("ordinary", "private")
        assert not client.is_authenticated
    finally:
        client.close()


def test_remote_capture_once_per_authenticated_session():
    class AppClient:
        base_url = "https://example.org"
        _user = {"id": 1}
        session_serial = 0
        calls = 0

        def request(self, method, path):
            assert (method, path) == ("POST", "/v1/client-start")
            self.calls += 1
            result = default_snapshot()
            result["revision"] = self.calls
            return result

    app = AppClient()
    remote = RemoteDeveloperService(app, client_factory=lambda _: object())
    assert remote.load_startup_snapshot()["revision"] == 1
    assert remote.load_startup_snapshot()["revision"] == 1
    assert app.calls == 1
    app._user = {"id": 2}
    assert remote.load_startup_snapshot()["revision"] == 2
    assert app.calls == 2
    app.session_serial += 1
    assert remote.load_startup_snapshot()["revision"] == 3
    assert app.calls == 3


def test_feature_disabled_is_not_account_authorization_loss(monkeypatch):
    client = CloudAPIClient(
        "https://example.org",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"detail": "feature_disabled_today"})
        ),
    )
    client._token = "synthetic-authenticated-token"
    revoked = []
    monkeypatch.setattr(client, "revoke_offline_access", lambda: revoked.append(True))
    try:
        with pytest.raises(CloudAPIError) as error:
            client.request("POST", "/v1/rpc/sync/apply_queued", json={})
        assert error.value.code == "feature_disabled"
        assert not revoked
        assert client.is_online_authenticated
    finally:
        client.close()


def test_unknown_forbidden_code_still_revokes_offline_authorization(monkeypatch):
    client = CloudAPIClient(
        "https://example.org",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"detail": "unknown private server message"})
        ),
    )
    client._token = "synthetic-authenticated-token"
    revoked = []
    monkeypatch.setattr(client, "revoke_offline_access", lambda: revoked.append(True))
    try:
        with pytest.raises(CloudAPIError) as error:
            client.request("POST", "/v1/rpc/sync/apply_queued", json={})
        assert error.value.code == "permission"
        assert revoked == [True]
        assert "private server" not in str(error.value)
    finally:
        client.close()


def test_feature_disabled_keeps_queue_and_relogin_can_resume(qtbot, tmp_path):
    from test_offline_sync_queue import coordinator

    sync = coordinator(tmp_path)
    queued = sync.submit_intent("health", "add_life_record", [1], {"content": "需要保留的记录"})
    sync._state = "online"
    sync.client.fail = CloudAPIError("feature_disabled", status_code=403)
    sync._replay_pending(1)
    item = sync.get_pending(queued.operation_id)
    assert item["status"] == "queued"
    assert item["error_code"] == "feature_disabled"
    assert not sync.authorization_blocked
    calls = len(sync.client.calls)
    sync._replay_pending(1)
    assert len(sync.client.calls) == calls  # No repeated calls during this session.
    sync.client.fail = None
    sync._feature_paused_operations.clear()  # start() clears this after fresh login.
    sync._replay_pending(1)
    assert not sync.pending_operations()
    assert {call[3]["request_id"] for call in sync.client.calls} == {queued.operation_id}


def test_expired_advisor_term_can_be_restored_before_login(local):
    service, database, auth, operator, _member, _secrets = local
    advisor = auth.create_staff(operator.id, "expired_advisor", "AdvisorPassword@92026", "advisor")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO staff_account_terms(user_id,starts_at,ends_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (
                advisor.id,
                "2000-01-01T00:00:00Z",
                "2001-01-01T00:00:00Z",
                "2000-01-01T00:00:00Z",
                "2000-01-01T00:00:00Z",
            ),
        )
    with pytest.raises(AuthenticationError):
        auth.authenticate("expired_advisor", "AdvisorPassword@92026")
    result = service.update_record(
        advisor.id, "staff_account_terms", advisor.id, {"ends_at": "2099-01-01T00:00:00Z"}
    )
    assert result["state"] == "applied"
    assert result["security_effect"] == "immediate"
    assert auth.authenticate("expired_advisor", "AdvisorPassword@92026").id == advisor.id
    assert not service.user_details(advisor.id)["pending_changes"]
