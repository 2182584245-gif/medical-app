"""Offline safety tests: no network, real secret files or cloud configuration."""

from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

from tools.local_platform import bootstrap as local
from tools.local_platform import guard


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, execute=None):
        self.calls = []
        self.execute_result = execute or (lambda _query, _params: Result())
        self.pgconn = SimpleNamespace(
            ssl_in_use=True,
            encrypt_password=lambda *_args: b"SCRAM-SHA-256$synthetic-verifier",
        )
        self.committed = False
        self.rolled_back = False

    def execute(self, query, params=None):
        query = query if isinstance(query, str) else query.as_string()
        self.calls.append((query, params))
        return self.execute_result(query, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None


@pytest.fixture(autouse=True)
def isolated_test(monkeypatch):
    for name in list(os.environ):
        if name.upper().startswith(guard.FORBIDDEN_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LOCAL_CONTAINER_ONLY", "1")
    monkeypatch.setattr(guard, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(guard, "Path", lambda _path: SimpleNamespace(is_file=lambda: True))
    monkeypatch.setattr(local.psycopg, "connect", lambda **_kwargs: pytest.fail("No network"))
    monkeypatch.setattr(local, "_read_password", lambda _path: pytest.fail("No real secrets"))


@pytest.mark.parametrize("value", [None, "", "true", "0", "yes"])
def test_explicit_gate_precedes_secrets_and_network(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LOCAL_CONTAINER_ONLY")
    else:
        monkeypatch.setenv("LOCAL_CONTAINER_ONLY", value)
    with pytest.raises(local.LocalBootstrapError, match="confirmation"):
        local.bootstrap()


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "PGPASSWORD"])
def test_rejects_libpq_environment_overrides_before_connect(monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-cloud-routing-or-secret")
    with pytest.raises(local.LocalBootstrapError, match="environment") as error:
        local.bootstrap()
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("prefix", ["PLATFORM_", "SERVER_", "SUPABASE_", "RAILWAY_", "RENDER_"])
def test_shared_guard_rejects_cloud_environment_before_secrets_or_connect(monkeypatch, prefix):
    monkeypatch.setenv(prefix + "DATABASE_URL", "synthetic-cloud-secret")
    with pytest.raises(local.LocalBootstrapError, match="cloud environment") as error:
        local.bootstrap()
    assert "synthetic" not in str(error.value)
    with pytest.raises(local.LocalBootstrapError, match="cloud environment"):
        local._connect(local.ADMIN, "12" * 32)


@pytest.mark.parametrize("platform,docker", [("win32", True), ("darwin", True), ("linux", False)])
def test_shared_guard_rejects_non_linux_or_non_docker(monkeypatch, platform, docker):
    monkeypatch.setattr(guard, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(guard, "Path", lambda _path: SimpleNamespace(is_file=lambda: docker))
    with pytest.raises(local.LocalBootstrapError, match="Linux Docker"):
        local.bootstrap()


def test_fixed_connection_and_verify_full(monkeypatch, tmp_path):
    certificate = tmp_path / "ca.crt"
    certificate.write_text("synthetic-ca", encoding="ascii")
    monkeypatch.setattr(local, "CA_CERTIFICATE", certificate)
    calls = []
    monkeypatch.setattr(local.psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    local._connect(local.ADMIN, "12" * 32)
    assert calls == [
        {
            "host": "postgres",
            "port": 5432,
            "dbname": "medical_app_local",
            "user": "local_bootstrap",
            "password": "12" * 32,
            "sslmode": "verify-full",
            "sslrootcert": str(certificate),
            "connect_timeout": 10,
            "application_name": "medical-app-local-bootstrap",
        }
    ]


def target_row(user=local.ADMIN):
    return [
        local.DATABASE,
        user,
        user,
        local.SERVER_MARKER,
        local.DATABASE_MARKER,
        local.ADMIN,
        "172.25.0.2",
        "scram-sha-256",
    ]


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "postgres"),
        (1, "postgres"),
        (2, "postgres"),
        (3, None),
        (3, "other"),
        (4, None),
        (4, "other"),
        (5, "postgres"),
        (6, "8.8.8.8"),
        (6, "127.0.0.1"),
        (6, "0.0.0.0"),
        (6, "::1"),
        (6, "::"),
        (6, "2606:4700:4700::1111"),
        (6, "172.25.0.2/32"),
        (6, "invalid"),
        (7, "md5"),
    ],
)
def test_target_identity_or_address_mismatch_cannot_modify(index, value):
    row = target_row()
    row[index] = value
    connection = Connection(lambda _q, _p: Result(row=tuple(row)))
    with pytest.raises(local.LocalBootstrapError):
        local._check_target(connection, local.ADMIN)
    assert all(query.startswith("SELECT") for query, _ in connection.calls)


def test_target_tls_is_mandatory_and_checked_first():
    connection = Connection()
    connection.pgconn.ssl_in_use = False
    with pytest.raises(local.LocalBootstrapError, match="TLS"):
        local._check_target(connection, local.ADMIN)
    assert connection.calls == []


def test_expected_server_and_runtime_identities_pass():
    for user in (local.ADMIN, local.PLATFORM_RUNTIME_ROLE):
        connection = Connection(lambda _q, _p, user=user: Result(row=tuple(target_row(user))))
        local._check_target(connection, user)


@pytest.mark.parametrize("address", ["172.25.0.2", "fd00:1234::2"])
def test_server_inet_is_converted_to_plain_host_text_before_validation(address):
    # PostgreSQL inet::text preserves /32 or /128 even for host addresses;
    # host(inet) removes that representation detail without loosening IP checks.
    def execute(query, _params):
        assert "pg_catalog.host(inet_server_addr())" in query
        assert "inet_server_addr()::text" not in query
        row = target_row()
        row[6] = address
        return Result(row=tuple(row))

    local._check_target(Connection(execute), local.ADMIN)


def orchestration(monkeypatch, *, schema=None, role=None):
    admin = Connection()
    runtime = Connection(lambda _q, _p: Result(row=(0,)))
    monkeypatch.setattr(
        local,
        "_read_password",
        lambda path: "12" * 32 if path == local.ADMIN_PASSWORD else "34" * 32,
    )
    monkeypatch.setattr(
        local, "_connect", lambda user, _p: admin if user == local.ADMIN else runtime
    )
    monkeypatch.setattr(local, "_check_target", lambda *_args: None)
    monkeypatch.setattr(local, "_schema_row", lambda _c: schema)
    monkeypatch.setattr(local, "_role_row", lambda _c: role)
    calls = []
    monkeypatch.setattr(local, "_create_installation", lambda *_args: calls.append("create"))
    monkeypatch.setattr(local, "_verify_installation", lambda *_args: calls.append("verify"))
    return admin, runtime, calls


def test_create_and_verify_in_one_transaction_then_check_runtime(monkeypatch):
    admin, runtime, calls = orchestration(monkeypatch)
    result = local.bootstrap()
    assert result["created"] is True
    assert result["table_count"] == 27
    assert result["cloud_connected"] is False
    assert result["historical_data_imported"] is False
    assert calls == ["create", "verify"]
    assert admin.committed and runtime.committed


def test_rerun_verifies_without_creating_or_resetting_password(monkeypatch):
    admin, _runtime, calls = orchestration(monkeypatch, schema=("owned",), role=("owned",))
    assert local.bootstrap()["created"] is False
    assert calls == ["verify"]
    assert admin.committed
    assert not any(query.startswith(("CREATE", "ALTER", "GRANT")) for query, _ in admin.calls)


@pytest.mark.parametrize("schema,role", [(("unknown",), None), (None, ("unknown",))])
def test_half_existing_installation_is_never_adopted(monkeypatch, schema, role):
    admin, _runtime, calls = orchestration(monkeypatch, schema=schema, role=role)
    with pytest.raises(local.LocalBootstrapError, match="same-name"):
        local.bootstrap()
    assert calls == [] and admin.rolled_back


def test_verification_error_rolls_back_creation_and_masks_driver_secret(monkeypatch):
    admin, runtime, _calls = orchestration(monkeypatch)

    def fail(_connection):
        raise RuntimeError("postgresql://synthetic-password@cloud.invalid")

    monkeypatch.setattr(local, "_verify_installation", fail)
    with pytest.raises(local.LocalBootstrapError) as error:
        local.bootstrap()
    assert "synthetic-password" not in str(error.value)
    assert admin.rolled_back and not runtime.committed


def test_refuse_identical_admin_and_runtime_passwords(monkeypatch):
    monkeypatch.setattr(local, "_read_password", lambda _path: "ab" * 32)
    with pytest.raises(local.LocalBootstrapError, match="independent"):
        local.bootstrap()


def verification_fixture(monkeypatch):
    role = [
        f"{local.ROLE_MARKER}:{local._ddl_digest()}:catalog-ok",
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        5,
        False,
    ]
    tables = [
        [table, local.PLATFORM_MARKER, local.ADMIN, True, True, list(columns)]
        for table, columns in local.PLATFORM_COLUMNS.items()
    ]
    matrix = [
        [table, privilege, privilege in rules, False]
        for table, rules in local.policy_rules().items()
        for privilege in local.PRIVILEGES
    ]
    scope = [True, False, True, False, False, False]
    state = {"role": role, "tables": tables, "matrix": matrix, "scope": scope}
    monkeypatch.setattr(local, "_schema_row", lambda _c: (local.PLATFORM_MARKER, local.ADMIN))
    monkeypatch.setattr(local, "_role_row", lambda _c: tuple(state["role"]))
    monkeypatch.setattr(local, "_catalog_digest", lambda _c: "catalog-ok")

    def execute(query, _params):
        if "SELECT c.relname,pg_catalog.obj_description" in query:
            return Result(rows=state["tables"])
        if "CROSS JOIN unnest" in query:
            return Result(rows=state["matrix"])
        if query.startswith("SELECT has_schema_privilege"):
            return Result(row=tuple(state["scope"]))
        pytest.fail("Unexpected verification query")

    return Connection(execute), state


def test_owned_full_schema_role_and_acl_verify(monkeypatch):
    connection, _state = verification_fixture(monkeypatch)
    local._verify_installation(connection)


@pytest.mark.parametrize(
    "mutation",
    [
        "unowned-role",
        "superuser",
        "bypassrls",
        "membership",
        "role-limit",
        "table-count",
        "columns",
        "rls",
        "owner",
        "grant-option",
        "extra-grant",
        "db-create",
        "db-temp",
        "catalog-drift",
    ],
)
def test_rejects_role_schema_policy_and_privilege_drift(monkeypatch, mutation):
    connection, state = verification_fixture(monkeypatch)
    if mutation == "unowned-role":
        state["role"][0] = "unknown"
    elif mutation == "superuser":
        state["role"][2] = True
    elif mutation == "bypassrls":
        state["role"][6] = True
    elif mutation == "membership":
        state["role"][9] = True
    elif mutation == "role-limit":
        state["role"][8] = -1
    elif mutation == "table-count":
        state["tables"].pop()
    elif mutation == "columns":
        state["tables"][0][5].append("unknown")
    elif mutation == "rls":
        state["tables"][0][4] = False
    elif mutation == "owner":
        state["tables"][0][2] = local.PLATFORM_RUNTIME_ROLE
    elif mutation == "grant-option":
        state["matrix"][0][3] = True
    elif mutation == "extra-grant":
        state["matrix"][4][2] = True  # TRUNCATE
    elif mutation == "db-create":
        state["scope"][3] = True
    elif mutation == "db-temp":
        state["scope"][4] = True
    elif mutation == "catalog-drift":
        monkeypatch.setattr(local, "_catalog_digest", lambda _c: "changed-policy-expression")
    with pytest.raises(local.LocalBootstrapError):
        local._verify_installation(connection)


def test_creation_reuses_frozen_ddl_and_scram_without_plaintext_sql(monkeypatch):
    monkeypatch.setattr(local, "_catalog_digest", lambda _c: "catalog-ok")
    connection = Connection()
    password = "ef" * 32
    local._create_installation(connection, password)
    statements = [query for query, _ in connection.calls]
    for query in (
        *local.TABLE_DDL,
        *local.INDEX_DDL,
        *local.compatibility_constraints(),
        *local.policy_ddl(),
        *local.runtime_grant_ddl(),
    ):
        assert query in statements
    combined = "\n".join(statements)
    assert password not in combined
    assert "SCRAM-SHA-256$synthetic-verifier" in combined
    assert "NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5" in combined
    assert not any(query.startswith(("DROP", "ALTER ROLE")) for query in statements)


def test_catalog_digest_tracks_metadata_but_not_business_data():
    rows = [["table", "policy", True]]
    connection = Connection(lambda _q, _p: Result(rows=copy.deepcopy(rows)))
    first = local._catalog_digest(connection)
    assert len(first) == 64
    rows[0][1] = "changed-policy"
    assert local._catalog_digest(connection) != first
    assert all("pg_catalog." in query for query, _ in connection.calls)
    assert all(params == (local.PLATFORM_SCHEMA,) for _, params in connection.calls)


def test_main_outputs_only_safe_status(monkeypatch, capsys):
    monkeypatch.setattr(local, "bootstrap", lambda: {"status": "local_database_ready"})
    assert local.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "local_database_ready"


def test_no_cloud_or_dpapi_administration_imports():
    source = local.Path(local.__file__).read_text(encoding="utf-8")
    assert "from server.platform_schema import" in source
    for forbidden in (
        "import platform_admin",
        "import cloud_connection",
        "import local_secret_store",
        "load_secret_payload",
        "SERVER_MIGRATION_DATABASE_URL",
    ):
        assert forbidden not in source
