from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import certifi
import psycopg
import pytest
from sqlalchemy.engine import URL, make_url

from server import platform_admin as admin

REF = "rvtjveplqnelgwnpuqtn"


def management():
    return URL.create(
        "postgresql+psycopg",
        username=f"postgres.{REF}",
        password="synthetic-admin-secret",
        host="aws-0-test.pooler.supabase.com",
        port=5432,
        database="postgres",
        query={"sslmode": "verify-full", "sslrootcert": certifi.where()},
    )


def runtime_payload(state="ready"):
    url = management().set(username=f"{admin.PLATFORM_RUNTIME_ROLE}.{REF}", password="ab" * 32)
    return {
        "project_url": admin.project_url(),
        "purpose": admin.PURPOSE,
        "state": state,
        "runtime_database_url": url.render_as_string(hide_password=False),
        "token_pepper": bytes(range(32)).hex(),
        "provision_id": "cd" * 16,
    }


def verified_report():
    return {
        "schema_exists": True,
        "pilot_owned": True,
        "schema_owned": True,
        "expected_table_set": True,
        "expected_columns": True,
        "all_rls_forced": True,
        "expected_policy_set": True,
        "data_api_roles_no_access": True,
        "revisions": [admin.PLATFORM_REVISION],
    }


@pytest.fixture(autouse=True)
def no_real_access(monkeypatch):
    monkeypatch.setattr(admin, "load_secret_payload", lambda _p: pytest.fail("no actual secrets"))
    monkeypatch.setattr(
        admin, "save_secret_payload", lambda *_a, **_k: pytest.fail("no actual writes")
    )
    monkeypatch.setattr(admin.psycopg, "connect", lambda **_k: pytest.fail("no actual network"))
    monkeypatch.setattr(admin.command, "upgrade", lambda *_a: pytest.fail("no actual migration"))
    monkeypatch.setattr(admin, "ca_file", lambda value: value)  # TLS parser is tested separately.


@pytest.mark.parametrize("operation", ["migrate", "provision"])
def test_explicit_confirmations_before_secret_or_network(operation):
    with pytest.raises(admin.PlatformAdminError):
        if operation == "migrate":
            admin.migrate(confirm=admin.PLATFORM_SCHEMA, pilot_confirm="")
        else:
            admin.provision(confirm="yes")


@pytest.mark.parametrize(
    "mutation",
    [
        {"purpose": "private-pilot-runtime-v1"},
        {"state": "pending"},
        {"token_pepper": "ab" * 32},
        {"provision_id": "bad"},
        {"extra": "secret"},
        {"project_url": "https://" + "a" * 20 + ".supabase.co"},
    ],
)
def test_runtime_payload_rejects_pilot_pending_and_invalid(monkeypatch, mutation):
    value = {**runtime_payload(), **mutation}
    monkeypatch.setattr(admin, "load_secret_payload", lambda _p: value)
    with pytest.raises(admin.PlatformAdminError) as error:
        admin.load_runtime_payload()
    assert value["runtime_database_url"] not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        {"username": f"postgres.{REF}"},
        {"username": f"medical_app_runtime.{REF}"},
        {"port": 6543},
        {"host": "wrong.invalid"},
        {"query": {"sslmode": "require"}},
        {"query": {"sslmode": "verify-full"}},
        {"password": "weak"},
    ],
)
def test_runtime_url_must_be_platform_specific_and_verified(monkeypatch, change):
    value = runtime_payload()
    value["runtime_database_url"] = (
        make_url(value["runtime_database_url"]).set(**change).render_as_string(hide_password=False)
    )
    monkeypatch.setattr(admin, "load_secret_payload", lambda _p: value)
    with pytest.raises(admin.PlatformAdminError):
        admin.load_runtime_url()


def test_settings_and_url_factories_do_not_connect_or_echo_secrets(monkeypatch):
    value = runtime_payload()
    monkeypatch.setattr(admin, "load_secret_payload", lambda _p: value)
    settings = admin.load_runtime_settings(test_client=True)
    assert settings.env == "test" and settings.require_https is False
    assert "testserver" in settings.allowed_hosts
    assert "ab" * 32 not in repr(settings)
    assert value["token_pepper"] not in repr(settings)
    url = admin.load_runtime_url()
    assert url.username == f"{admin.PLATFORM_RUNTIME_ROLE}.{REF}"
    assert "ab" * 32 not in repr(url)


@pytest.mark.parametrize("fail", [False, True])
def test_migration_environment_restores_all_three_values(monkeypatch, capsys, fail):
    monkeypatch.setenv("SERVER_MIGRATION_CONFIRM", "prior")
    monkeypatch.setenv("SERVER_PLATFORM_CONFIRM", "prior-platform")
    monkeypatch.delenv("SERVER_MIGRATION_DATABASE_URL", raising=False)
    try:
        with admin.migration_environment(management()):
            assert os.environ["SERVER_MIGRATION_CONFIRM"] == admin.PRIVATE_SCHEMA
            assert os.environ["SERVER_PLATFORM_CONFIRM"] == admin.PLATFORM_SCHEMA
            if fail:
                raise ValueError("interrupted")
    except ValueError:
        pass
    assert os.environ["SERVER_MIGRATION_CONFIRM"] == "prior"
    assert os.environ["SERVER_PLATFORM_CONFIRM"] == "prior-platform"
    assert "SERVER_MIGRATION_DATABASE_URL" not in os.environ
    assert capsys.readouterr().out == ""


def test_existing_unknown_platform_not_adopted(monkeypatch):
    monkeypatch.setattr(admin, "inspect_catalog", lambda _p: {"schema_exists": True})
    with pytest.raises(admin.PlatformAdminError, match="未接管"):
        admin.migrate(confirm=admin.PLATFORM_SCHEMA, pilot_confirm=admin.PRIVATE_SCHEMA)


def test_migration_idempotent_when_already_verified(monkeypatch):
    monkeypatch.setattr(admin, "inspect_catalog", lambda _p: verified_report())
    result = admin.migrate(confirm=admin.PLATFORM_SCHEMA, pilot_confirm=admin.PRIVATE_SCHEMA)
    assert result["status"] == "platform_migration_already_verified"


@pytest.mark.parametrize("succeed", [False, True])
def test_migration_fixed_revision_and_postcheck(monkeypatch, succeed):
    before = {"schema_exists": False, "pilot_owned": True, "revisions": ["pilot_0001"]}
    after = {**verified_report(), "all_rls_forced": succeed}
    reports = iter([before, after])
    monkeypatch.setattr(admin, "inspect_catalog", lambda _p: next(reports))
    monkeypatch.setattr(admin, "management_url", lambda _p: management())
    calls = []
    monkeypatch.setattr(admin.command, "upgrade", lambda _cfg, revision: calls.append(revision))
    if succeed:
        result = admin.migrate(confirm=admin.PLATFORM_SCHEMA, pilot_confirm=admin.PRIVATE_SCHEMA)
        assert result["status"] == "platform_migration_verified"
        assert result["created_tables"] == 27
    else:
        with pytest.raises(admin.PlatformAdminError, match="RLS"):
            admin.migrate(confirm=admin.PLATFORM_SCHEMA, pilot_confirm=admin.PRIVATE_SCHEMA)
    assert calls == [admin.PLATFORM_REVISION]


class FakeConnection:
    def __init__(self):
        self.pgconn = SimpleNamespace(
            ssl_in_use=True, encrypt_password=lambda *_a: b"SCRAM-SHA-256$synthetic"
        )
        self.queries = []
        self.read_only = None
        self.rollbacks = 0
        self.committed = False
        self.fail_commit = False

    def execute(self, statement, params=None):
        query = statement if isinstance(statement, str) else statement.as_string()
        self.queries.append((query, params))
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, kind, *_a):
        if kind is None:
            if self.fail_commit:
                raise psycopg.OperationalError("uncertain-commit-secret")
            self.committed = True
        return False


def test_readonly_connection_rolls_back_and_enforces_tls(monkeypatch):
    connection = FakeConnection()
    captured = []
    monkeypatch.setattr(
        admin.psycopg, "connect", lambda **kwargs: captured.append(kwargs) or connection
    )
    with admin._connection(management(), readonly=True):
        assert connection.read_only
    assert connection.rollbacks == 1
    assert captured[0]["sslmode"] == "verify-full"
    assert "hostaddr" not in captured[0]


@contextmanager
def provision_setup(monkeypatch, tmp_path, saved=None, existing=None):
    target = tmp_path / "platform.json"
    store = {} if saved is None else {target: copy.deepcopy(saved)}
    if saved:
        target.touch()
    connection = FakeConnection()
    events = []
    role = [existing]

    def save(path, value, *, replace=False):
        assert replace or path not in store
        events.append(("save", value["state"]))
        store[path] = copy.deepcopy(value)
        path.touch()

    def create(_c, value):
        assert store[target] == value and value["state"] == "pending"
        events.append(("create", "role"))
        role[0] = ("owned",)

    monkeypatch.setattr(admin, "management_url", lambda _p: management())
    monkeypatch.setattr(admin, "_connect", lambda _u: connection)
    monkeypatch.setattr(admin, "_inspect", lambda _c: verified_report())
    monkeypatch.setattr(admin, "load_secret_payload", lambda path: copy.deepcopy(store[path]))
    monkeypatch.setattr(admin, "save_secret_payload", save)
    monkeypatch.setattr(admin, "_role_row", lambda _c: role[0])
    monkeypatch.setattr(admin, "_create_role", create)
    monkeypatch.setattr(admin, "_verify_role", lambda _c, _p: events.append(("verify", "role")))
    yield SimpleNamespace(target=target, store=store, connection=connection, events=events)


def do_provision(ctx):
    return admin.provision(confirm=admin.PLATFORM_SCHEMA, runtime_profile=ctx.target)


def test_journal_saved_before_role_and_ready_after_commit(monkeypatch, tmp_path):
    with provision_setup(monkeypatch, tmp_path) as ctx:
        result = do_provision(ctx)
        assert ctx.connection.committed
        assert ctx.events == [
            ("save", "pending"),
            ("create", "role"),
            ("verify", "role"),
            ("save", "ready"),
        ]
        assert result["connection_limit"] == 5 and result["business_rows_changed"] == 0
        assert ctx.store[ctx.target]["token_pepper"] not in json.dumps(result)


def test_unknown_role_rejected_before_journal_write(monkeypatch, tmp_path):
    with provision_setup(monkeypatch, tmp_path, existing=("unknown",)) as ctx:
        with pytest.raises(admin.PlatformAdminError, match="拒绝接管"):
            do_provision(ctx)
        assert not ctx.store and not ctx.events


@pytest.mark.parametrize("existing", [None, ("owned",)])
def test_pending_retry_reuses_both_secrets(monkeypatch, tmp_path, existing):
    original = runtime_payload("pending")
    with provision_setup(monkeypatch, tmp_path, saved=original, existing=existing) as ctx:
        do_provision(ctx)
        assert ctx.store[ctx.target] == {**original, "state": "ready"}
        assert ("save", "pending") not in ctx.events
        assert (("create", "role") in ctx.events) == (existing is None)


def test_ready_missing_role_never_recreated(monkeypatch, tmp_path):
    with provision_setup(monkeypatch, tmp_path, saved=runtime_payload()) as ctx:
        with pytest.raises(admin.PlatformAdminError, match="未自动重建"):
            do_provision(ctx)
        assert not ctx.events


def test_commit_uncertainty_keeps_pending_and_hides_driver_text(monkeypatch, tmp_path):
    with provision_setup(monkeypatch, tmp_path) as ctx:
        ctx.connection.fail_commit = True
        with pytest.raises(admin.PlatformAdminError) as error:
            do_provision(ctx)
        assert ctx.store[ctx.target]["state"] == "pending"
        assert "uncertain-commit-secret" not in str(error.value)


def test_pilot_profile_cannot_be_overwritten():
    with pytest.raises(admin.PlatformAdminError, match="旧试点"):
        admin.provision(
            confirm=admin.PLATFORM_SCHEMA,
            runtime_profile=admin.DEFAULT_PROFILE.with_name("supabase-runtime.json"),
        )


def test_role_creation_uses_scram_and_frozen_minimum_grants():
    connection = FakeConnection()
    admin._create_role(connection, runtime_payload())
    text = "\n".join(query for query, _ in connection.queries)
    assert "ab" * 32 not in text
    assert "SCRAM-SHA-256$" in text
    assert "NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5" in text
    assert "NOCREATEDB NOCREATEROLE" in text
    for statement in admin.runtime_grant_ddl():
        assert statement in text
    assert "ALTER ROLE" not in text and "DROP" not in text


def test_cli_errors_and_output_never_include_secrets(monkeypatch, capsys):
    def fail(_p):
        raise psycopg.OperationalError("secret-db-url-password")

    monkeypatch.setattr(admin, "inspect_catalog", fail)
    monkeypatch.setattr("sys.argv", ["platform_admin", "inspect"])
    assert admin.main() == 3
    assert "secret-db-url-password" not in capsys.readouterr().out


@pytest.mark.parametrize("bad", [None, "role", "membership", "grant_option", "table", "sequence"])
def test_exact_role_and_privilege_matrix_is_verified(monkeypatch, bad):
    value = runtime_payload()
    row = [admin._marker(value), True, False, False, False, False, False, False, 5, False]
    if bad == "role":
        row[8] = -1
    if bad == "membership":
        row[9] = True
    monkeypatch.setattr(admin, "_role_row", lambda _c: tuple(row))
    queries = []

    def execute(statement, params):
        queries.append(statement)
        if "has_schema_privilege" in statement:
            single = (True, False, False, True, False)
            rows = []
        elif "has_table_privilege" in statement:
            single = None
            rows = [
                (table, permission, permission in rules, bad == "grant_option")
                for table, rules in admin.policy_rules().items()
                for permission in admin.PRIVILEGES
            ]
            if bad == "table":
                first = rows[0]
                rows[0] = (first[0], first[1], not first[2], first[3])
        else:
            single = None
            rows = [
                (f"{table}_id_seq", True, True, bad == "sequence")
                for table in admin.IDENTITY_TABLES
            ]
        return SimpleNamespace(fetchone=lambda: single, fetchall=lambda: rows)

    if bad:
        with pytest.raises(admin.PlatformAdminError):
            admin._verify_role(SimpleNamespace(execute=execute), value)
    else:
        admin._verify_role(SimpleNamespace(execute=execute), value)
        assert len(queries) == 3  # batched table matrix, not 216 network round-trips


@pytest.mark.parametrize("bad", [None, "schema", "columns", "rls", "policies", "api"])
def test_catalog_metadata_only_inspection(monkeypatch, bad):
    statements = []

    def execute(statement, params=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        statements.append(text)
        single = None
        rows = []
        if "WHERE n.nspname=%s AND c.relname='alembic_version'" in text:
            single = (admin.SCHEMA_MARKER, admin.REVISION_MARKER, True, True)
        elif "SELECT version_num" in text:
            rows = [(admin.PLATFORM_REVISION,)]
        elif "FROM pg_catalog.pg_namespace WHERE nspname" in text:
            single = ("unknown" if bad == "schema" else admin.PLATFORM_MARKER, True)
        elif "pg_catalog.pg_attribute" in text:
            rows = [
                (
                    table,
                    admin.PLATFORM_MARKER,
                    True,
                    bad != "rls",
                    True,
                    [] if bad == "columns" else list(columns),
                )
                for table, columns in admin.PLATFORM_COLUMNS.items()
            ]
        elif "pg_catalog.pg_policy" in text:
            codes = {"SELECT": "r", "INSERT": "a", "UPDATE": "w", "DELETE": "d"}
            rows = [
                (table, "platform_" + action.lower(), codes[action], True)
                for table, rules in admin.policy_rules().items()
                for action in rules
            ]
            if bad == "policies":
                rows.pop()
        elif "has_table_privilege" in text:
            rows = [
                (role, table, bad == "api", False)
                for role in ("anon", "authenticated", "service_role")
                for table in admin.PLATFORM_COLUMNS
            ]
        return SimpleNamespace(fetchone=lambda: single, fetchall=lambda: rows)

    result = admin._inspect(SimpleNamespace(execute=execute))
    assert admin._verified(result) == (bad is None)
    assert result["health_records_read"] == 0
    assert result["tables_changed"] is False
    assert all("pg_catalog" in text or "alembic_version" in text for text in statements)
