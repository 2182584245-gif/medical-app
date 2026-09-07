from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy.engine import make_url

from server import cloud_runtime as runtime
from server.cloud_connection import CloudConfigurationError, connection_payload
from server.local_secret_store import LocalSecretStoreError

REF = "rvtjveplqnelgwnpuqtn"
URI = f"postgresql://postgres.{REF}:unused@aws-0-test.pooler.supabase.com:5432/postgres"


def admin_payload():
    return connection_payload(URI, "synthetic-admin@password-only", None)


def payload(state="ready"):
    admin = make_url(admin_payload()["migration_database_url"])
    uri = admin.set(username=f"medical_app_runtime.{REF}", password="ab" * 32)
    return {
        "project_url": runtime.project_url(),
        "purpose": runtime.PURPOSE,
        "runtime_database_url": uri.render_as_string(hide_password=False),
        "token_pepper": bytes(range(32)).hex(),
        "provision_id": "cd" * 16,
        "state": state,
    }


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    monkeypatch.setattr(runtime.psycopg, "connect", lambda **_k: pytest.fail("no cloud calls"))
    monkeypatch.setattr(runtime, "load_secret_payload", lambda _p: pytest.fail("no real secrets"))
    monkeypatch.setattr(
        runtime, "save_secret_payload", lambda *_a, **_k: pytest.fail("no real writes")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"purpose": "migration-readonly-preflight"},
        {"state": "pending"},
        {"state": "unknown"},
        {"project_url": "https://" + "a" * 20 + ".supabase.co"},
        {"provision_id": "bad"},
        {"token_pepper": "ab" * 32},
        {"unexpected": "secret"},
    ],
)
def test_runtime_rejects_wrong_profile_without_secret_echo(monkeypatch, mutation):
    value = {**payload(), **mutation}
    monkeypatch.setattr(runtime, "load_secret_payload", lambda _p: value)
    with pytest.raises(CloudConfigurationError) as error:
        runtime.load_runtime_payload()
    assert value["runtime_database_url"] not in str(error.value)
    assert value["token_pepper"] not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        {"username": f"postgres.{REF}"},
        {"username": "medical_app_runtime"},
        {"host": "evil.invalid"},
        {"port": 6543},
        {"password": "short"},
        {"query": {"sslmode": "require"}},
        {"query": {"sslmode": "verify-full"}},
        {"query": {"sslmode": "verify-full", "hostaddr": "127.0.0.1"}},
    ],
)
def test_runtime_rejects_admin_wrong_project_and_weak_tls(monkeypatch, change):
    value = payload()
    value["runtime_database_url"] = (
        make_url(value["runtime_database_url"]).set(**change).render_as_string(hide_password=False)
    )
    monkeypatch.setattr(runtime, "load_secret_payload", lambda _p: value)
    with pytest.raises(CloudConfigurationError):
        runtime.load_runtime_payload()


def test_settings_factory_is_local_only_and_secret_repr_safe(monkeypatch):
    value = payload()
    monkeypatch.setattr(runtime, "load_secret_payload", lambda _p: value)
    monkeypatch.setenv("SERVER_ALLOWED_HOSTS", '["*"]')
    monkeypatch.setenv("SERVER_REQUIRE_HTTPS", "true")
    settings = runtime.load_runtime_settings(test_client=True)
    assert settings.env == "test"
    assert settings.allowed_hosts == ["localhost", "127.0.0.1", "testserver"]
    assert settings.require_https is False
    assert "ab" * 32 not in repr(settings)
    assert value["token_pepper"] not in repr(settings)
    assert make_url(settings.database_url.get_secret_value()).query["sslmode"] == "verify-full"


def test_direct_runtime_username_is_bound_to_exact_project(monkeypatch):
    value = payload()
    value["runtime_database_url"] = (
        make_url(value["runtime_database_url"])
        .set(host=f"db.{REF}.supabase.co", username=runtime.RUNTIME_ROLE)
        .render_as_string(hide_password=False)
    )
    monkeypatch.setattr(runtime, "load_secret_payload", lambda _p: value)
    assert runtime.load_runtime_payload() == value


class FakeConnection:
    def __init__(self):
        self.queries = []
        self.pgconn = SimpleNamespace(
            ssl_in_use=True,
            encrypt_password=lambda *_a: b"SCRAM-SHA-256$4096:synthetic$verifier:key",
        )
        self.commit_error = False
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, kind, *_args):
        if kind is None:
            if self.commit_error:
                raise psycopg.OperationalError("synthetic-sensitive-commit-error")
            self.committed = True
        return False

    def execute(self, statement, params=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        self.queries.append((text, params))
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])


@contextmanager
def setup_provision(monkeypatch, tmp_path, *, existing=None, saved=None):
    admin_path = tmp_path / "admin.json"
    target = tmp_path / "runtime.json"
    store = {} if saved is None else {target: copy.deepcopy(saved)}
    if saved is not None:
        target.touch()
    connection = FakeConnection()
    events = []
    role = [existing]

    def load(path):
        return admin_payload() if path == admin_path else copy.deepcopy(store[path])

    def save(path, value, *, replace=False):
        if path in store and not replace:
            raise LocalSecretStoreError("已有记录，未覆盖")
        events.append(("save", value["state"]))
        store[path] = copy.deepcopy(value)
        target.touch()

    def create(conn, value):
        assert store[target]["state"] == "pending"
        assert value == store[target]
        events.append(("create", "role"))
        role[0] = (runtime._marker(value), True, False, False, False, False, False, False, False)

    monkeypatch.setattr(runtime, "load_secret_payload", load)
    monkeypatch.setattr(runtime, "save_secret_payload", save)
    monkeypatch.setattr(runtime, "_connect", lambda _u: connection)
    monkeypatch.setattr(runtime, "_check_schema", lambda _c: events.append(("check", "schema")))
    monkeypatch.setattr(runtime, "_role_row", lambda _c: role[0])
    monkeypatch.setattr(runtime, "_create_role", create)
    monkeypatch.setattr(runtime, "_verify_role", lambda _c, _p: events.append(("verify", "role")))
    yield SimpleNamespace(
        admin=admin_path,
        target=target,
        store=store,
        connection=connection,
        events=events,
        role=role,
    )


def run(ctx):
    return runtime.provision(
        confirm=runtime.PRIVATE_SCHEMA, admin_profile=ctx.admin, runtime_profile=ctx.target
    )


def test_confirmation_before_any_secret_read_or_connection():
    with pytest.raises(CloudConfigurationError):
        runtime.provision(confirm="yes")


def test_pending_saved_before_create_and_ready_only_after_commit(monkeypatch, tmp_path):
    with setup_provision(monkeypatch, tmp_path) as ctx:
        report = run(ctx)
        assert ctx.connection.committed
        assert ctx.events == [
            ("check", "schema"),
            ("save", "pending"),
            ("create", "role"),
            ("verify", "role"),
            ("save", "ready"),
        ]
        assert report["status"] == "runtime_role_created"
        assert report["business_rows_changed"] == 0
        assert len(make_url(ctx.store[ctx.target]["runtime_database_url"]).password) == 64
        assert ctx.store[ctx.target]["token_pepper"] not in json.dumps(report)


def test_unknown_role_refused_without_saving_or_altering(monkeypatch, tmp_path):
    with setup_provision(monkeypatch, tmp_path, existing=("unknown",)) as ctx:
        with pytest.raises(CloudConfigurationError, match="拒绝接管"):
            run(ctx)
        assert ctx.events == [("check", "schema")]
        assert not ctx.store


def test_pending_missing_role_reuses_password_and_pepper(monkeypatch, tmp_path):
    original = payload("pending")
    with setup_provision(monkeypatch, tmp_path, saved=original) as ctx:
        run(ctx)
        assert ctx.store[ctx.target] == {**original, "state": "ready"}
        assert ("save", "pending") not in ctx.events


def test_ready_missing_role_never_recreated(monkeypatch, tmp_path):
    with setup_provision(monkeypatch, tmp_path, saved=payload()) as ctx:
        with pytest.raises(CloudConfigurationError, match="未重建"):
            run(ctx)
        assert ("create", "role") not in ctx.events


@pytest.mark.parametrize("state", ["pending", "ready"])
def test_retry_existing_owned_role_no_rotation_or_ddl(monkeypatch, tmp_path, state):
    original = payload(state)
    with setup_provision(monkeypatch, tmp_path, saved=original, existing=("owned",)) as ctx:
        result = run(ctx)
        assert result["status"] == "runtime_role_verified"
        assert result["password_rotated"] is False
        assert ("create", "role") not in ctx.events
        assert ctx.store[ctx.target]["runtime_database_url"] == original["runtime_database_url"]


def test_commit_uncertainty_keeps_pending_credentials(monkeypatch, tmp_path):
    with setup_provision(monkeypatch, tmp_path) as ctx:
        ctx.connection.commit_error = True
        with pytest.raises(psycopg.OperationalError):
            run(ctx)
        assert ctx.store[ctx.target]["state"] == "pending"
        assert ("save", "ready") not in ctx.events


def test_local_journal_failure_prevents_role_creation(monkeypatch, tmp_path):
    with setup_provision(monkeypatch, tmp_path) as ctx:

        def failed(*_a, **_k):
            raise LocalSecretStoreError("模拟磁盘不可写")

        monkeypatch.setattr(runtime, "save_secret_payload", failed)
        with pytest.raises(LocalSecretStoreError):
            run(ctx)
        assert ("create", "role") not in ctx.events
        assert not ctx.connection.committed


def test_create_uses_scram_and_only_specific_private_grants():
    connection = FakeConnection()
    value = payload()
    runtime._create_role(connection, value)
    text = "\n".join(q for q, _p in connection.queries)
    assert "ab" * 32 not in text
    assert "SCRAM-SHA-256$" in text
    assert "NOBYPASSRLS NOINHERIT" in text
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in text
    assert 'GRANT SELECT, INSERT ON TABLE "medical_app_private"."pilot_messages"' in text
    assert "GRANT ALL" not in text
    assert "alembic_version" not in text
    assert "DELETE" not in text and "TRUNCATE" not in text
    assert "public" not in text and "auth." not in text and "storage" not in text


@pytest.mark.parametrize("index", range(9))
def test_existing_role_attributes_and_marker_must_match(monkeypatch, index):
    value = payload()
    row = [runtime._marker(value), True, False, False, False, False, False, False, False]
    row[index] = "foreign" if index == 0 else not row[index]
    monkeypatch.setattr(runtime, "_role_row", lambda _c: tuple(row))
    with pytest.raises(CloudConfigurationError, match="归属"):
        runtime._verify_role(FakeConnection(), value)


def test_schema_absent_fails_before_role_changes():
    with pytest.raises(CloudConfigurationError, match="专用结构"):
        runtime._check_schema(FakeConnection())


def test_cli_hides_driver_errors(monkeypatch, capsys):
    def failed(**_kwargs):
        raise psycopg.OperationalError("password=synthetic-do-not-print")

    monkeypatch.setattr(runtime, "provision", failed)
    monkeypatch.setattr(
        "sys.argv", ["cloud_runtime", "provision", "--confirm", "medical_app_private"]
    )
    assert runtime.main() == 3
    assert "synthetic-do-not-print" not in capsys.readouterr().out


@pytest.mark.parametrize("extra", [False, True])
def test_private_privilege_matrix_and_metadata_are_checked(monkeypatch, extra):
    value = payload()
    monkeypatch.setattr(
        runtime,
        "_role_row",
        lambda _c: (runtime._marker(value), True, False, False, False, False, False, False, False),
    )
    checked = []

    def execute(statement, params):
        if "has_schema_privilege" in statement:
            row = (True, False, True)
        else:
            role, table, privilege = params
            checked.append((table, privilege))
            assert role == runtime.RUNTIME_ROLE
            allowed = privilege in runtime.TABLE_PRIVILEGES.get(table.split(".")[1], ())
            row = (allowed or (extra and table.endswith("alembic_version")),)
        return SimpleNamespace(fetchone=lambda: row)

    connection = SimpleNamespace(execute=execute)
    if extra:
        with pytest.raises(CloudConfigurationError, match="最小权限"):
            runtime._verify_role(connection, value)
    else:
        runtime._verify_role(connection, value)
        assert len(checked) == 40


def test_bound_connection_kwargs_preserve_tls_without_overrides(monkeypatch):
    captured = {}
    monkeypatch.setattr(runtime.psycopg, "connect", lambda **kwargs: captured.update(kwargs))
    runtime._connect(make_url(admin_payload()["migration_database_url"]))
    assert captured["sslmode"] == "verify-full"
    assert captured["port"] == 5432
    assert captured["password"] == "synthetic-admin@password-only"
    assert "hostaddr" not in captured and "options" not in captured
