"""Synthetic API tests. RLS enforcement is independently tested with PostgreSQL."""

import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from platform_test_support import PASSWORD, PEPPER, SyntheticDatabase
from test_platform_rpc import payload

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from server.platform_app import create_app
from server.platform_config import PlatformSettings
from server.platform_developer import ApplicationSettings
from server.platform_developer_schema import grant_ddl, policy_ddl


def schema(database):
    database.connection.executescript("""
        CREATE TABLE developer_grants(user_id INTEGER PRIMARY KEY,enabled INTEGER,granted_at TEXT);
        CREATE TABLE developer_sessions(token_digest TEXT PRIMARY KEY,user_id INTEGER,
            expires_at TEXT);
        CREATE TABLE developer_client_sessions(token_digest TEXT PRIMARY KEY,user_id INTEGER,
            revision INTEGER,captured_at TEXT);
        CREATE TABLE developer_settings_snapshots(revision INTEGER PRIMARY KEY,settings_json TEXT,
            encrypted_key TEXT,created_at TEXT,created_by INTEGER);
        CREATE TABLE developer_pending_changes(id INTEGER PRIMARY KEY,target_user_id INTEGER,
            table_name TEXT,row_id INTEGER,changes_json TEXT,base_fingerprint TEXT,state TEXT,
            created_by INTEGER,created_at TEXT);
    """)


@pytest.fixture
def context(tmp_path):
    database = SyntheticDatabase()
    schema(database)
    developer = database.account("console-developer", "operator")
    member = database.account("console-member")
    other = database.account("console-other")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO developer_grants VALUES (?,1,?)", (developer, timestamp_to_db(utc_now()))
        )
    settings = PlatformSettings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        token_pepper=PEPPER,
        allowed_hosts=["testserver"],
        require_https=True,
    )
    application = create_app(settings, database=database)
    key_path = tmp_path / "encryption.key"
    key_path.write_bytes(secrets.token_bytes(32))
    application.state.developer_service.key_path = str(key_path)
    with TestClient(
        application, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield database, application, client, developer, member, other
    database.close()


def login(client, username="console-developer", developer=True):
    path = "/v1/developer/login" if developer else "/v1/auth/login"
    result = client.post(path, json={"username": username, "password": PASSWORD})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["access_token"]}


def test_ordinary_operator_token_not_developer_capability(context):
    _, _, client, _, _, _ = context
    normal = login(client, developer=False)
    assert client.get("/v1/developer/users", headers=normal).status_code == 403
    stepped = login(client)
    response = client.get("/v1/developer/users", headers=stepped)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert "password" not in response.text
    assert (
        client.post(
            "/v1/developer/login", json={"username": "console-member", "password": PASSWORD}
        ).status_code
        == 403
    )


def test_revocation_and_expiry_take_effect_each_request(context):
    database, _, client, developer, _, _ = context
    headers = login(client)
    database.connection.execute(
        "UPDATE developer_grants SET enabled=0 WHERE user_id=?", (developer,)
    )
    assert client.get("/v1/developer/settings", headers=headers).status_code == 403
    database.connection.execute(
        "UPDATE developer_grants SET enabled=1 WHERE user_id=?", (developer,)
    )
    database.connection.execute(
        "UPDATE developer_sessions SET expires_at='2000-01-01T00:00:00.000000Z'"
    )
    assert client.get("/v1/developer/settings", headers=headers).status_code == 403


def test_snapshot_revision_encrypted_write_only_key_and_stale_write(context):
    database, app, client, _, _, _ = context
    headers = login(client)
    data = ApplicationSettings().model_dump()
    data["ai"]["system_prompt"] = "请使用老人熟悉的日常说法。"
    data["ai"]["knowledge"] = [
        {"title": "素材", "content": "待审内容", "source": "自有材料", "enabled": True}
    ]
    fake_key = "synthetic-test-key-" + secrets.token_hex(16)
    response = client.put(
        "/v1/developer/settings",
        headers=headers,
        json={
            "expected_revision": 0,
            "settings": data,
            "api_key": fake_key,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    assert response.json()["api_key_configured"]
    assert fake_key not in response.text
    stored = database.scalar("SELECT encrypted_key FROM developer_settings_snapshots")
    assert fake_key not in stored
    principal = app.state.platform_auth.resolve_token(headers["Authorization"][7:])
    assert app.state.developer_service.key_for_revision(principal, 1) == fake_key
    assert (
        client.put(
            "/v1/developer/settings",
            headers=headers,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 409
    )
    member_headers = login(client, "console-member", developer=False)
    public = client.get("/v1/client-settings", headers=member_headers)
    assert public.status_code == 200
    assert fake_key not in public.text and stored not in public.text
    assert public.json()["settings"]["ai"]["knowledge"][0]["reviewed"] is False


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("runtime", "database_url", "postgres://forbidden"),
        ("runtime", "shell_command", "arbitrary command"),
        ("ai", "python_code", "print(1)"),
        ("ai", "max_tokens", 999999),
    ],
)
def test_settings_reject_unbounded_and_arbitrary_controls(context, section, field, value):
    _, _, client, _, _, _ = context
    headers = login(client)
    data = ApplicationSettings().model_dump()
    data[section][field] = value
    result = client.put(
        "/v1/developer/settings", headers=headers, json={"expected_revision": 0, "settings": data}
    )
    assert result.status_code == 422
    assert "print(1)" not in result.text


def _conversation(database, member):
    return database.connection.execute(
        "SELECT id,title FROM conversations WHERE user_id=?", (member,)
    ).fetchone()


def test_business_edits_staged_until_target_start_and_not_another_user(context):
    database, _, client, _, member, _ = context
    headers = login(client)
    original = _conversation(database, member)
    changed = client.patch(
        f"/v1/developer/users/{member}/records/conversations/{original[0]}",
        headers=headers,
        json={"changes": {"title": "新的记录标题"}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["state"] == "pending"
    assert _conversation(database, member)[1] == original[1]
    other_headers = login(client, "console-other", developer=False)
    assert (
        client.post("/v1/client-start", headers=other_headers).json()["activation"]["applied"] == 0
    )
    assert _conversation(database, member)[1] == original[1]
    member_headers = login(client, "console-member", developer=False)
    assert client.get("/v1/client-settings", headers=member_headers).status_code == 200
    assert _conversation(database, member)[1] == original[1]
    started = client.post("/v1/client-start", headers=member_headers)
    assert started.status_code == 200, started.text
    assert started.json()["activation"] == {"applied": 1, "conflicts": 0}
    assert _conversation(database, member)[1] == "新的记录标题"
    assert (
        client.post("/v1/client-start", headers=member_headers).json()["activation"]["applied"] == 0
    )


def test_pending_conflict_does_not_overwrite_recent_user_changes(context):
    database, _, client, _, member, _ = context
    headers = login(client)
    original = _conversation(database, member)
    path = f"/v1/developer/users/{member}/records/conversations/{original[0]}"
    assert (
        client.patch(path, headers=headers, json={"changes": {"title": "管理修改"}}).status_code
        == 200
    )
    database.connection.execute(
        "UPDATE conversations SET title='用户新修改' WHERE id=?", (original[0],)
    )
    result = client.post(
        "/v1/client-start", headers=login(client, "console-member", developer=False)
    )
    assert result.json()["activation"] == {"applied": 0, "conflicts": 1}
    assert _conversation(database, member)[1] == "用户新修改"


def test_record_ownership_system_fields_and_secret_json_rejected(context):
    database, _, client, _, member, other = context
    headers = login(client)
    original = _conversation(database, member)
    path = f"/v1/developer/users/{member}/records/conversations/{original[0]}"
    assert (
        client.patch(path, headers=headers, json={"changes": {"user_id": other}}).status_code == 422
    )
    wrong = f"/v1/developer/users/{other}/records/conversations/{original[0]}"
    assert (
        client.patch(wrong, headers=headers, json={"changes": {"title": "wrong"}}).status_code
        == 404
    )
    assert (
        client.patch(
            f"/v1/developer/users/{member}/records/platform_sessions/1",
            headers=headers,
            json={"changes": {"token": "bad"}},
        ).status_code
        == 422
    )


def test_password_reset_is_write_only_hashes_and_revokes_sessions(context):
    database, _, client, _, member, _ = context
    headers = login(client)
    old_session = login(client, "console-member", developer=False)
    new_password = "synthetic-new-passphrase-123"
    result = client.patch(
        f"/v1/developer/users/{member}", headers=headers, json={"password": new_password}
    )
    assert result.status_code == 200, result.text
    assert new_password not in result.text
    stored = database.scalar("SELECT password_hash FROM users WHERE id=?", (member,))
    assert stored.startswith("$argon2id$") and new_password not in stored
    assert client.get("/v1/client-settings", headers=old_session).status_code == 401
    details = client.get(f"/v1/developer/users/{member}", headers=headers)
    assert details.status_code == 200, details.text
    assert stored not in details.text
    assert "password_hash" not in details.text


def test_rls_migration_has_no_grant_escalation_or_bypass():
    policies = "\n".join(policy_ddl())
    grants = "\n".join(grant_ddl())
    assert "FORCE ROW LEVEL SECURITY" in policies
    assert "current_setting('medical_app.developer'" in policies
    assert "developer_sessions" in policies
    assert "developer_activation" in policies
    assert "GRANT SELECT ON medical_app_platform.developer_grants" in grants
    assert "INSERT ON medical_app_platform.developer_grants" not in grants
    assert "UPDATE ON medical_app_platform.developer_grants" not in grants
    assert "BYPASSRLS" not in grants
    assert "CREATE ROLE" not in grants
    assert "SECURITY DEFINER" not in policies


def test_invalid_credentials_and_input_never_echo_password(context):
    _, _, client, _, _, _ = context
    fake_password = "synthetic-wrong-sensitive-password"
    result = client.post(
        "/v1/developer/login", json={"username": "console-developer", "password": fake_password}
    )
    assert result.status_code == 401
    assert fake_password not in result.text
    result = client.post(
        "/v1/developer/login",
        json={
            "username": "console-developer",
            "password": fake_password,
            "sql": "DROP TABLE users",
        },
    )
    assert result.status_code == 422
    assert fake_password not in result.text and "DROP TABLE" not in result.text


def test_proxy_without_revision_header_uses_current_policy_and_rejects_rollback(context):
    _, app, client, _, _, _ = context
    developer = login(client)
    data = ApplicationSettings().model_dump()
    data["features"]["ai"] = False
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 200
    )
    member = login(client, "console-member", developer=False)
    payload = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "你好"}]}
    blocked = client.post("/v1/ai/chat", headers=member, json=payload)
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"] == "ai_disabled_for_application_session"
    spoofed = client.post(
        "/v1/ai/chat", headers={**member, "X-Application-Settings-Revision": "0"}, json=payload
    )
    assert spoofed.status_code == 409, spoofed.text
    principal = app.state.platform_auth.resolve_token(member["Authorization"][7:])
    assert app.state.developer_service.capture_for_ai(principal)["revision"] == 1


def test_captured_settings_stay_fixed_until_explicit_restart(context):
    _, app, client, _, _, _ = context
    developer = login(client)
    member = login(client, "console-member", developer=False)
    start = client.post("/v1/client-start", headers=member)
    assert start.json()["revision"] == 0
    data = ApplicationSettings().model_dump()
    data["runtime"]["announcement"] = "下次打开应用显示"
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 200
    )
    principal = app.state.platform_auth.resolve_token(member["Authorization"][7:])
    assert app.state.developer_service.capture_for_ai(principal)["revision"] == 0
    assert client.post("/v1/client-start", headers=member).json()["revision"] == 1
    assert app.state.developer_service.capture_for_ai(principal)["revision"] == 1


def test_actual_server_status_is_safe_and_stepup_only(context):
    _, _, client, _, _, _ = context
    result = client.get("/v1/developer/server-status", headers=login(client))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "ready"
    assert result.json()["database"] == "sqlite"
    assert result.json()["server_utc"].endswith("Z")
    assert "password" not in result.text and "database_url" not in result.text
    normal = login(client, developer=False)
    assert client.get("/v1/developer/server-status", headers=normal).status_code == 403


@pytest.mark.parametrize(
    "feature,service,method,extra",
    [
        ("commerce", "commerce", "list_products", ()),
        ("ai", "chat", "create_conversation", ("新对话",)),
        ("today", "health", "delete_life_record", (123,)),
        ("appointments", "service_management", "list_appointments", ()),
    ],
)
def test_disabled_services_are_blocked_on_rpc_not_only_hidden_ui(
    context, feature, service, method, extra
):
    _, _, client, _, member_id, _ = context
    developer = login(client)
    data = ApplicationSettings().model_dump()
    data["features"][feature] = False
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 200
    )
    member = login(client, "console-member", developer=False)
    blocked = client.post(
        f"/v1/rpc/{service}/{method}", headers=member, json=payload(member_id, *extra)
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"] == "feature_disabled_" + feature


def test_disabled_record_writes_do_not_acknowledge_or_delete_offline_intents(context):
    database, _, client, _, member_id, _ = context
    developer = login(client)
    data = ApplicationSettings().model_dump()
    data["features"]["today"] = False
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 200
    )
    member = login(client, "console-member", developer=False)
    request = payload(member_id, {"service": "health", "method": "add_life_record"})
    result = client.post("/v1/rpc/sync/apply_queued", headers=member, json=request)
    assert result.status_code == 403, result.text
    assert result.json()["detail"] == "feature_disabled_today"
    assert database.scalar("SELECT count(*) FROM rpc_requests") == 0


def test_maintenance_keeps_operator_recovery_and_client_configuration_available(context):
    _, _, client, developer_id, member_id, _ = context
    developer = login(client)
    data = ApplicationSettings().model_dump()
    data["runtime"]["maintenance_enabled"] = True
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 200
    )
    member = login(client, "console-member", developer=False)
    assert client.post("/v1/client-start", headers=member).status_code == 200
    blocked = client.post("/v1/rpc/health/get_profile", headers=member, json=payload(member_id))
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "application_maintenance"
    assert client.get("/v1/developer/server-status", headers=developer).status_code == 200
    operator = client.post("/v1/rpc/auth/get_user", headers=developer, json=payload(developer_id))
    assert operator.status_code == 200, operator.text


def test_expired_advisor_validity_extension_is_immediate_and_revokes_old_sessions(context):
    database, _, client, _, _, _ = context
    advisor = database.account("expired-advisor", "advisor")
    now = utc_now()
    encoded_now = timestamp_to_db(now)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO staff_account_terms VALUES(?,?,?,?,?)",
            (
                advisor,
                timestamp_to_db(now - timedelta(days=60)),
                timestamp_to_db(now + timedelta(days=1)),
                encoded_now,
                encoded_now,
            ),
        )
    previous = login(client, "expired-advisor", developer=False)
    database.connection.execute(
        "UPDATE staff_account_terms SET ends_at=? WHERE user_id=?",
        (
            timestamp_to_db(now - timedelta(days=1)),
            advisor,
        ),
    )
    failed = client.post(
        "/v1/auth/login", json={"username": "expired-advisor", "password": PASSWORD}
    )
    assert failed.status_code == 401
    developer = login(client)
    extended = timestamp_to_db(now + timedelta(days=90))
    result = client.patch(
        f"/v1/developer/users/{advisor}/records/staff_account_terms/{advisor}",
        headers=developer,
        json={"changes": {"ends_at": extended}},
    )
    assert result.status_code == 200, result.text
    assert result.json()["security_effect"] == "immediate"
    assert (
        database.scalar("SELECT ends_at FROM staff_account_terms WHERE user_id=?", (advisor,))
        == extended
    )
    assert database.scalar("SELECT count(*) FROM developer_pending_changes") == 0
    assert client.get("/v1/client-settings", headers=previous).status_code == 401
    assert login(client, "expired-advisor", developer=False)
    assert (
        database.scalar(
            "SELECT count(*) FROM audit_logs WHERE action='developer.account.validity_updated'"
        )
        == 1
    )
