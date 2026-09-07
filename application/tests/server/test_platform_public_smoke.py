from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from platform_test_support import PEPPER, SyntheticDatabase

from ollama_chat_app.services.cloud_client import CloudAPIClient
from ollama_chat_app.services.health import HealthService
from server import platform_public_smoke as smoke
from server.platform_app import create_app
from server.platform_config import PlatformSettings

ORIGIN = "https://synthetic-public-check.onrender.com"
RAILWAY_ORIGIN = "https://synthetic-public-check.up.railway.app"
SECRET_ERROR = "NEVER-PRINT-token-password-SQL-response-https://private.invalid/?key=secret"


class SqliteManagement:
    """Translate only syntax for synthetic tests; this does not simulate PostgreSQL RLS."""

    def __init__(self, database):
        self.database = database
        self.statements = []
        self.before_buckets = None
        self.after_buckets = None
        self.manifest = None
        self.fail_count = False

    def execute(self, query, parameters=()):
        self.statements.append((query, parameters))
        translated = query.replace(smoke.PLATFORM_SCHEMA + ".", "").replace("%s", "?")
        translated = translated.replace(" FOR UPDATE", "")
        if self.fail_count and translated.startswith("SELECT COUNT(*) FROM messages"):
            return SimpleNamespace(fetchone=lambda: (1,))
        return self.database.connection.execute(translated, parameters)

    def preflight(self, manifest):
        self.manifest = manifest
        with self.database.transaction():
            smoke._preflight(self, manifest)

    def cleanup(self, manifest):
        self.before_buckets = list(self.database.connection.execute(
            "SELECT * FROM auth_rate_buckets ORDER BY bucket_id"))
        with self.database.transaction():
            result = smoke._cleanup_transaction(self, manifest)
        self.after_buckets = list(self.database.connection.execute(
            "SELECT * FROM auth_rate_buckets ORDER BY bucket_id"))
        return result


@pytest.fixture(params=[ORIGIN, RAILWAY_ORIGIN], ids=["render", "railway"])
def context(request):
    origin = request.param
    database = SyntheticDatabase()
    # The shared test fixture omits CASCADE on its extra auth/RPC tables. Match
    # the reviewed production cascade here, using only a disposable in-memory DB.
    database.connection.execute("DROP TABLE platform_sessions")
    database.connection.execute("DROP TABLE rpc_requests")
    database.connection.execute(
        "CREATE TABLE platform_sessions (token_digest TEXT PRIMARY KEY, "
        "user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, created_at TEXT, "
        "expires_at TEXT, revoked_at TEXT)"
    )
    database.connection.execute(
        "CREATE TABLE rpc_requests (user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, "
        "request_id TEXT, request_digest TEXT, response_json TEXT, created_at TEXT, "
        "PRIMARY KEY(user_id,request_id))"
    )
    settings = PlatformSettings(
        env="test", database_url="sqlite+pysqlite:///:memory:", token_pepper=PEPPER,
        allowed_hosts=[urlsplit(origin).hostname], require_https=True,
    )
    app = create_app(settings, database=database)
    management = SqliteManagement(database)
    seen, cloud_clients = [], []
    with TestClient(app, base_url=origin, raise_server_exceptions=False) as api:
        def send(request):
            assert request.url.scheme == "https"
            assert str(request.url).startswith(origin + "/")
            assert not request.url.path.startswith("/v1/rpc/ai/")
            seen.append(request)
            response = api.request(request.method, request.url.path, content=request.content,
                                   headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content,
                                  headers=response.headers)

        def factory(origin):
            client = CloudAPIClient(origin, transport=httpx.MockTransport(send))
            cloud_clients.append(client)
            return client

        yield SimpleNamespace(database=database, management=management, factory=factory,
                              send=send, seen=seen, clients=cloud_clients, app=app, origin=origin)
    database.close()


def execute(context, **kwargs):
    return smoke.run(base_url=context.origin, confirm=smoke.PLATFORM_SCHEMA,
                     client_factory=kwargs.pop("client_factory", context.factory),
                     management=kwargs.pop("management", context.management), **kwargs)


def assert_safe_report(result, context):
    serialized = json.dumps(result)
    assert SECRET_ERROR not in serialized
    for request in context.seen:
        authorization = request.headers.get("authorization")
        if authorization:
            assert authorization not in serialized
            assert authorization[7:] not in serialized
        if request.url.path.startswith("/v1/auth/") and request.content:
            body = json.loads(request.content)
            if "password" in body:
                assert body["password"] not in serialized
    assert all(not client.is_authenticated and client._closed for client in context.clients)


def test_full_offline_flow_uses_real_client_and_facades_cleans_only_own_records(context):
    unrelated_id = context.database.account("unrelated-synthetic-user")
    life_id = HealthService(context.database).add_life_record(
        unrelated_id, "water", "2026-09-07T09:00:00+08:00", "unrelated synthetic record",
        details={"amount_ml": 150},
    )
    result = execute(context)
    assert result["status"] == "passed", result
    assert result["mode"] == "offline_transport"
    assert result["ai_called"] is False
    assert result["checks_passed"] == [
        "health_live", "health_ready", "unauthenticated_401", "registration_role_injection_422",
        "register_two_members", "independent_clients_same_account",
        "multiple_conversations_idempotency", "small_image_message_idempotency",
        "different_account_isolation", "forged_actor_403", "beijing_health_time_roundtrip",
        "file_upload_download_hash_isolation", "logout_only_current_token",
    ]
    assert result["cleanup"] == {
        "status": "verified", "accounts_removed": 2, "synthetic_rows_remaining": 0,
        "shared_rate_buckets_modified": False,
    }
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 1
    assert context.database.scalar("SELECT id FROM life_records") == life_id
    assert context.database.scalar("SELECT COUNT(*) FROM user_files") == 0
    assert context.database.scalar("SELECT COUNT(*) FROM messages") == 0
    assert context.database.scalar("SELECT COUNT(*) FROM chat_attachments") == 0
    assert context.database.scalar("SELECT COUNT(*) FROM rpc_requests") == 0
    assert context.database.scalar("SELECT COUNT(*) FROM platform_sessions") == 0
    assert context.management.before_buckets == context.management.after_buckets
    assert context.management.before_buckets  # Auth really exercised its shared buckets.
    deletes = [query for query, _ in context.management.statements if query.startswith("DELETE")]
    assert len(deletes) == 4
    assert all("audit_logs WHERE actor_user_id=%s" in query or
               "users WHERE id=%s AND username=%s AND username_normalized=%s" in query
               for query in deletes)
    assert not any("LIKE" in query or "auth_rate_buckets" in query
                   for query, _ in context.management.statements)
    assert len(context.clients) == 3 and len({id(client) for client in context.clients}) == 3
    assert_safe_report(result, context)


@pytest.mark.parametrize("value", [
    "http://safe.onrender.com", "https://onrender.com", "https://evil.example",
    "https://safe.onrender.com.evil.example", "https://safe.onrender.com:444",
    "https://safe.onrender.com/api", "https://secret@safe.onrender.com",
    "https://safe.onrender.com/?password=secret", "https://safe.onrender.com/#secret",
    "https://localhost", "https://a.b.onrender.com", " https://safe.onrender.com",
    "http://safe.up.railway.app", "https://up.railway.app", "https://safe.railway.app",
    "https://a.b.up.railway.app", "https://safe.up.railway.app.evil.example",
    "https://safe.up.railway.app:444", "https://safe.up.railway.app/api",
    "https://secret@safe.up.railway.app", "https://safe.up.railway.app/?secret=value",
    "https://safe.up.railway.app/#secret", "https://-bad.up.railway.app",
    "https://bad-.up.railway.app", "https://" + "a" * 64 + ".up.railway.app",
])
def test_origin_and_confirmation_guards_have_zero_side_effects(value):
    class ForbiddenManagement:
        def preflight(self, _manifest):
            pytest.fail("configuration guard must run before management access")

    def forbidden_client(_origin):
        pytest.fail("configuration guard must run before any client construction")

    result = smoke.run(base_url=value, confirm=smoke.PLATFORM_SCHEMA,
                       client_factory=forbidden_client, management=ForbiddenManagement())
    assert result["code"] == "configuration"
    assert result["cleanup"]["status"] == "not_required"
    assert value not in json.dumps(result)


def test_wrong_confirmation_never_loads_admin_configuration(monkeypatch):
    def forbidden():
        pytest.fail("must not load real management settings")

    monkeypatch.setattr(smoke, "ManagementCleanup", forbidden)
    result = smoke.run(base_url=ORIGIN, confirm="medical_app_private")
    assert result["code"] == "configuration"


@pytest.mark.parametrize("path", [
    "/health/live", "/health/ready", "/v1/auth/login",
    "/v1/rpc/chat/create_conversation", "/v1/rpc/chat/begin_message",
    "/v1/rpc/health/add_life_record", "/v1/rpc/files/upload_bytes",
])
def test_network_failure_stops_and_cleans_registered_accounts_without_error_text(context, path):
    def send(request):
        if request.url.path == path:
            return httpx.Response(503, json={"detail": SECRET_ERROR})
        return context.send(request)

    def factory(origin):
        client = CloudAPIClient(origin, transport=httpx.MockTransport(send))
        context.clients.append(client)
        return client

    result = execute(context, client_factory=factory)
    assert result["status"] == "failed" and result["code"] == "unavailable"
    assert result["cleanup"]["status"] == "verified"
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 0
    assert context.database.scalar("SELECT COUNT(*) FROM messages") == 0
    assert context.management.before_buckets == context.management.after_buckets
    assert_safe_report(result, context)


def test_registration_commit_then_transport_timeout_resolves_exact_account(context):
    def send(request):
        response = context.send(request)
        if request.url.path == "/v1/auth/register" and response.status_code == 201:
            raise httpx.ReadTimeout(SECRET_ERROR)
        return response

    def factory(origin):
        client = CloudAPIClient(origin, transport=httpx.MockTransport(send))
        context.clients.append(client)
        return client

    result = execute(context, client_factory=factory)
    assert result["status"] == "failed" and result["code"] == "timeout"
    assert result["cleanup"]["accounts_removed"] == 1
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 0
    assert len(context.management.manifest.known_ids) == 1
    assert_safe_report(result, context)


def test_registration_timeout_without_visible_row_is_not_falsely_claimed_clean(context):
    def send(request):
        if (request.url.path == "/v1/auth/register"
                and "role_code" not in json.loads(request.content)):
            # The server could still commit later; never report absence as proof.
            raise httpx.ReadTimeout(SECRET_ERROR)
        return context.send(request)

    def factory(origin):
        client = CloudAPIClient(origin, transport=httpx.MockTransport(send))
        context.clients.append(client)
        return client

    result = execute(context, client_factory=factory)
    assert result["status"] == "failed" and result["code"] == "cleanup_unconfirmed"
    assert result["cleanup"]["status"] == "failed"
    assert "synthetic_rows_remaining" not in result["cleanup"]
    assert_safe_report(result, context)


@pytest.mark.parametrize("tamper", ["wrong_id", "old_time", "unattempted", "bad_marker"])
def test_cleanup_refuses_identity_mismatch_before_any_delete(context, tamper):
    manifest = smoke.SyntheticRun()
    context.management.preflight(manifest)
    name = manifest.usernames[0]
    user_id = context.database.account(name)
    manifest.attempted.add(name)
    manifest.known_ids[name] = user_id
    if tamper == "wrong_id":
        manifest.known_ids[name] = user_id + 10
    elif tamper == "old_time":
        context.database.connection.execute("UPDATE users SET created_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), user_id))
    elif tamper == "unattempted":
        manifest.attempted.clear()
        manifest.known_ids.clear()
    else:
        manifest.marker = "existing-user"
    with pytest.raises(smoke.PublicSmokeError, match="cleanup_refused"):
        context.management.cleanup(manifest)
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 1
    assert not any(query.startswith("DELETE") for query, _ in context.management.statements)


def test_preflight_rejects_preexisting_exact_names(context):
    manifest = smoke.SyntheticRun()
    context.database.account(manifest.usernames[0])
    with pytest.raises(smoke.PublicSmokeError):
        context.management.preflight(manifest)
    assert manifest.preflight_verified is False


def test_cleanup_refuses_unexpected_cross_account_relationship_before_deletion(context):
    manifest = smoke.SyntheticRun()
    context.management.preflight(manifest)
    name = manifest.usernames[0]
    user_id = context.database.account(name)
    advisor = context.database.account("unrelated-synthetic-advisor", role="advisor")
    now = datetime.now(UTC).isoformat()
    context.database.connection.execute(
        "INSERT INTO advisor_bindings (member_user_id,advisor_user_id,status,started_at,"
        "created_at,updated_at) VALUES (?,?,'active',?,?,?)", (user_id, advisor, now, now, now),
    )
    manifest.attempted.add(name)
    manifest.known_ids[name] = user_id
    with pytest.raises(smoke.PublicSmokeError, match="cleanup_refused"):
        context.management.cleanup(manifest)
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 2
    assert context.database.scalar("SELECT COUNT(*) FROM advisor_bindings") == 1
    assert not any(query.startswith("DELETE") for query, _ in context.management.statements)


def test_cleanup_verification_failure_rolls_back_and_never_reports_pass(context):
    context.management.fail_count = True
    result = execute(context)
    assert result["status"] == "failed" and result["code"] == "cleanup_unconfirmed"
    assert result["cleanup"]["status"] == "failed"
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 2
    assert context.database.scalar("SELECT COUNT(*) FROM messages") == 2
    assert context.database.scalar("SELECT COUNT(*) FROM audit_logs") > 0
    assert_safe_report(result, context)


def test_management_exception_is_sanitized_without_clients(context):
    class Broken:
        def preflight(self, _manifest):
            raise sqlite3.OperationalError(SECRET_ERROR)

    result = execute(context, management=Broken())
    assert result["status"] == "failed" and result["code"] == "management_unavailable"
    assert result["cleanup"]["status"] == "not_required"
    assert context.clients == []
    assert SECRET_ERROR not in json.dumps(result)


def test_cli_requires_explicit_arguments_and_hides_unrecognized_inputs(capsys):
    with pytest.raises(SystemExit) as error:
        smoke.main(["--unexpected-password=" + SECRET_ERROR])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert SECRET_ERROR not in output.err + output.out
    assert "arguments_required_or_invalid" in output.err


def test_cli_report_and_exit_status(monkeypatch, capsys):
    def fake_run(**values):
        assert values == {"base_url": ORIGIN, "confirm": smoke.PLATFORM_SCHEMA,
                          "expected_host": urlsplit(ORIGIN).hostname}
        return {"status": "failed", "code": "test_only"}

    monkeypatch.setattr(smoke, "run", fake_run)
    assert smoke.main(["--base-url", ORIGIN, "--expected-host", urlsplit(ORIGIN).hostname,
                       "--confirm", smoke.PLATFORM_SCHEMA]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "code": "test_only"}


@pytest.mark.parametrize("origin", [ORIGIN, RAILWAY_ORIGIN])
def test_public_origin_pins_expected_service(origin):
    host = urlsplit(origin).hostname
    assert smoke.validate_public_origin(origin, expected_host=host) == origin
    assert smoke.validate_public_origin(origin + "/", expected_host=host) == origin
    assert smoke.validate_public_origin(origin + ":443", expected_host=host) == origin + ":443"
    for expected in ["", "another.up.railway.app", "*", "https://" + host, host + "/"]:
        with pytest.raises(smoke.PublicSmokeError, match="configuration"):
            smoke.validate_public_origin(origin, expected_host=expected)


def test_legacy_render_validator_does_not_widen_implicitly():
    assert smoke.validate_render_origin(ORIGIN) == ORIGIN
    with pytest.raises(smoke.PublicSmokeError, match="configuration"):
        smoke.validate_render_origin(RAILWAY_ORIGIN)


@pytest.mark.parametrize("expected", [None, "another.up.railway.app", ""])
def test_live_run_requires_matching_host_before_management_or_network(monkeypatch, expected):
    def forbidden(*_args, **_kwargs):
        pytest.fail("unconfirmed service must not load credentials or create network clients")

    monkeypatch.setattr(smoke, "ManagementCleanup", forbidden)
    monkeypatch.setattr(smoke, "CloudAPIClient", forbidden)
    result = smoke.run(base_url=RAILWAY_ORIGIN, expected_host=expected,
                       confirm=smoke.PLATFORM_SCHEMA)
    assert result["code"] == "configuration"
    assert result["cleanup"]["status"] == "not_required"
