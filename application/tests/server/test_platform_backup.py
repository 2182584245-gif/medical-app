from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import URL

from server import platform_backup as backup


def _uri(password="synthetic-password", **kwargs):
    return URL.create("postgresql+psycopg", username="postgres.synthetic", password=password,
                      host="aws-0-test.pooler.supabase.com", port=kwargs.get("port", 5432),
                      database="postgres", query={"sslmode": kwargs.get("sslmode", "verify-full"),
                                                 "sslrootcert": "synthetic-ca.pem"})


def test_dump_secret_only_in_anonymous_input(monkeypatch):
    monkeypatch.setattr(backup, "ca_file", lambda _path: "C:/trusted/ca.pem")
    uri = _uri("synthetic-!$\\-pass")
    args = backup.dump_arguments(uri, "00000003-00000112-1")
    assert uri.password not in repr(args)
    assert "PGSSLMODE=verify-full" in " ".join(args)
    assert "readonly" in args[args.index("--mount") + 1]
    assert args[-2:] == ["--schema", "medical_app_platform"]
    assert "--enable-row-security" not in args
    assert "--no-privileges" not in args
    assert "--no-owner" not in args
    assert "--snapshot" in args
    assert "default_transaction_read_only=on" in " ".join(args)
    metadata = backup.dump_arguments(uri, "00000003-00000112-1", metadata=True)
    assert metadata[-2:] == ["--table", "medical_app_private.alembic_version"]
    assert "--schema" not in metadata


@pytest.mark.parametrize("uri", [_uri("unsafe\npass"), _uri(port=6543), _uri(sslmode="require")])
def test_dump_rejects_unsafe_transport(uri):
    with pytest.raises(backup.BackupError):
        backup.dump_arguments(uri, "00000003-00000112-1")


def test_snapshot_rejects_shell_content():
    with pytest.raises(backup.BackupError):
        backup.dump_arguments(_uri(), "bad; echo secret")


def test_process_failure_never_leaks_stderr_or_credentials(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "docker.exe")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout=b"private-row", stderr=b"private-password")

    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(backup.BackupError) as error:
        backup._docker(["run", "synthetic"], data=b"synthetic-secret\n")
    assert "private" not in str(error.value)
    assert "synthetic-secret" not in repr(calls[0][0])
    assert calls[0][1]["input"] == b"synthetic-secret\n"
    assert calls[0][1]["capture_output"] is True


def test_process_timeout_never_exposes_partial_output(monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda _name: "docker.exe")

    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret-command", 5, output=b"private-row")

    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(backup.BackupError) as error:
        backup._docker(["run", "synthetic"])
    assert "secret" not in str(error.value)
    assert "private" not in str(error.value)


def test_environment_does_not_inherit_libpq_or_docker_overrides(monkeypatch):
    for key in ("PGPASSWORD", "PGSERVICE", "PGOPTIONS", "DOCKER_HOST", "DOCKER_CONTEXT",
                "SERVER_MIGRATION_DATABASE_URL"):
        monkeypatch.setenv(key, "synthetic-sensitive-value")
    environment = backup._environment()
    assert "synthetic-sensitive-value" not in repr(environment)


@pytest.mark.parametrize("host", ["tcp://untrusted:2376", "ssh://untrusted", ""])
def test_remote_or_unknown_docker_daemon_is_rejected(monkeypatch, host):
    monkeypatch.setattr(backup, "_docker", lambda _args: json.dumps([
        {"Endpoints": {"docker": {"Host": host}}}]).encode())
    with pytest.raises(backup.BackupError):
        backup.require_local_daemon()


def test_local_json_preserves_duplicate_catalog_field_names(monkeypatch):
    monkeypatch.setattr(backup, "local_sql", lambda *args: b'[{"same":1,"same":2,"x":[3]}]')
    assert backup._local_json("synthetic", "SELECT 1,2,3") == [[1, 2, [3]]]


def _container_info(token):
    return [{"Id": "a" * 64, "Config": {"Labels": {backup.LABEL: token}, "Image": backup.IMAGE},
             "HostConfig": {"NetworkMode": "none", "PortBindings": {},
                            "Tmpfs": {"/var/lib/postgresql/data": "rw"},
                            "LogConfig": {"Type": "none"}}, "Mounts": [{"Type": "tmpfs"}]}]


@pytest.mark.parametrize("mutation", ["label", "network", "port", "log", "mount", "image"])
def test_cleanup_refuses_any_unowned_or_nonisolated_container(monkeypatch, mutation):
    token = "b" * 32
    info = _container_info(token)
    item = info[0]
    if mutation == "label":
        item["Config"]["Labels"] = {}
    elif mutation == "network":
        item["HostConfig"]["NetworkMode"] = "bridge"
    elif mutation == "port":
        item["HostConfig"]["PortBindings"] = {"5432/tcp": [{}]}
    elif mutation == "log":
        item["HostConfig"]["LogConfig"]["Type"] = "json-file"
    elif mutation == "mount":
        item["Mounts"] = [{"Type": "bind"}]
    elif mutation == "image":
        item["Config"]["Image"] = "postgres:latest"
    monkeypatch.setattr(backup, "_docker", lambda _args: json.dumps(info).encode())
    assert backup._owned("a" * 64, token) is False


def test_cleanup_accepts_exact_owned_isolated_container(monkeypatch):
    token = "b" * 32
    monkeypatch.setattr(
        backup, "_docker", lambda _args: json.dumps(_container_info(token)).encode()
    )
    assert backup._owned("a" * 64, token) is True
    assert backup._owned("friendly-name", token) is False


def test_confirmation_precedes_credentials_or_docker(monkeypatch, tmp_path):
    def forbidden():
        raise AssertionError("Must not access Docker")

    monkeypatch.setattr(backup, "require_image", forbidden)
    with pytest.raises(backup.BackupError):
        backup.backup_and_verify(tmp_path / "not-real-profile.json", confirm="yes")


def test_existing_ciphertext_reverification_cannot_read_cloud(monkeypatch, tmp_path):
    path = tmp_path / "synthetic.hlpgbackup"
    path.touch()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Existing backup verification must never access cloud credentials")

    monkeypatch.setattr(backup, "export_cloud", forbidden)
    monkeypatch.setattr(backup, "management_url", forbidden)
    monkeypatch.setattr(backup, "require_image", lambda: None)
    monkeypatch.setattr(backup, "load_encrypted", lambda _source: {
        "inventory": {"counts": {"synthetic": 1}, "revisions": [["platform_0001"]]}})
    monkeypatch.setattr(backup, "verify_local", lambda _bundle: {"verified": True})
    result = backup.verify_encrypted(path, confirm=backup.CONFIRM)
    assert result["verified"] is True
    assert result["backup_file"] == str(path)


@pytest.mark.parametrize("unsupported", [False, True])
def test_unknown_privilege_or_executable_objects_fail_closed(unsupported):
    statements = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(fetchone=lambda: (unsupported,))

    connection = SimpleNamespace(execute=execute)
    if unsupported:
        with pytest.raises(backup.BackupError):
            backup.require_supported_catalog(connection)
    else:
        backup.require_supported_catalog(connection)
    assert "pg_authid" not in statements[0]
    assert "attacl" in statements[0] and "pg_proc" in statements[0]


@pytest.mark.parametrize("valid", [True, False])
def test_cloud_export_readonly_shared_snapshot_and_ownership_gate(monkeypatch, tmp_path, valid):
    class Connection:
        def __init__(self):
            self.pgconn = SimpleNamespace(ssl_in_use=True)
            self.statements = []
            self.rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            self.statements.append(statement)
            assert self.read_only is True
            assert self.isolation_level == backup.psycopg.IsolationLevel.REPEATABLE_READ
            assert statement.startswith(("SET LOCAL", "SELECT"))
            return SimpleNamespace(fetchone=lambda: ("00000003-00000112-1",))

        def rollback(self):
            self.rolled_back = True

    connection = Connection()
    monkeypatch.setattr(backup, "management_url", lambda _path: _uri("synthetic-pipe-password"))
    monkeypatch.setattr(backup.psycopg, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(backup, "ca_file", lambda _path: "C:/trusted/ca.pem")
    gates = {key: True for key in (
        "pilot_owned", "schema_owned", "expected_table_set", "expected_columns",
        "all_rls_forced", "expected_policy_set", "data_api_roles_no_access")}
    gates.update(schema_owned=valid, revisions=["platform_0001"])
    monkeypatch.setattr(backup, "_inspect", lambda _connection: gates)
    monkeypatch.setattr(backup, "source_inventory", lambda _connection: {"synthetic": True})
    calls = []

    def docker(args, **kwargs):
        calls.append((args, kwargs))
        return b"PGDMP-synthetic-archive"

    monkeypatch.setattr(backup, "_docker", docker)
    if not valid:
        with pytest.raises(backup.BackupError):
            backup.export_cloud(tmp_path / "synthetic-profile.json")
        assert calls == []
        return
    result = backup.export_cloud(tmp_path / "synthetic-profile.json")
    assert connection.rolled_back is True
    assert len(calls) == 2
    assert all(args[args.index("--snapshot") + 1] == "00000003-00000112-1"
               for args, _kwargs in calls)
    assert all(kwargs["data"] == b"synthetic-pipe-password\n" for _args, kwargs in calls)
    assert "synthetic-pipe-password" not in repr(result)
    assert "synthetic-pipe-password" not in repr([args for args, _kwargs in calls])


@pytest.mark.skipif(sys.platform != "win32", reason="current-user Windows DPAPI")
def test_dpapi_roundtrip_ciphertext_only_and_no_overwrite(tmp_path):
    bundle = {"version": 1, "image": backup.IMAGE,
              "scope": [backup.PLATFORM_SCHEMA, backup.PRIVATE_SCHEMA + ".alembic_version"],
              "synthetic": "synthetic-private-health-data-and-hash"}
    target = tmp_path / "synthetic.hlpgbackup"
    backup.save_encrypted(bundle, target)
    assert b"synthetic-private-health-data-and-hash" not in target.read_bytes()
    assert backup.load_encrypted(target) == bundle
    before = target.read_bytes()
    with pytest.raises(backup.BackupError):
        backup.save_encrypted(bundle, target)
    assert target.read_bytes() == before
    corrupted = tmp_path / "corrupt.hlpgbackup"
    corrupted.write_bytes(before[:-10])  # Synthetic ciphertext, never business data.
    with pytest.raises(backup.BackupError):
        backup.load_encrypted(corrupted)


@pytest.mark.skipif(os.environ.get("HEALTHLIFE_RUN_BACKUP_CONTAINER_TEST") != "1",
                    reason="explicit synthetic local Docker integration gate")
def test_official_pg17_synthetic_dump_restore_constraints_acl_and_counts(tmp_path):
    backup.require_image()
    with backup.disposable_restore() as container:
        backup.local_sql(container, """
            CREATE ROLE synthetic_backup_reader NOLOGIN;
            CREATE SCHEMA medical_app_platform;
            CREATE SCHEMA medical_app_private;
            CREATE TABLE medical_app_platform.members (
                id bigint PRIMARY KEY, name text NOT NULL UNIQUE CHECK(length(name)>0));
            CREATE TABLE medical_app_platform.notes (
                id bigint PRIMARY KEY, member_id bigint NOT NULL
                REFERENCES medical_app_platform.members(id), content text NOT NULL);
            CREATE INDEX note_member_idx ON medical_app_platform.notes(member_id);
            INSERT INTO medical_app_platform.members VALUES (1,'synthetic-member');
            INSERT INTO medical_app_platform.notes VALUES (1,1,'synthetic-private-data');
            ALTER TABLE medical_app_platform.members ENABLE ROW LEVEL SECURITY;
            ALTER TABLE medical_app_platform.members FORCE ROW LEVEL SECURITY;
            ALTER TABLE medical_app_platform.notes ENABLE ROW LEVEL SECURITY;
            ALTER TABLE medical_app_platform.notes FORCE ROW LEVEL SECURITY;
            CREATE POLICY own_member ON medical_app_platform.members FOR SELECT
                TO synthetic_backup_reader USING (id=1);
            GRANT USAGE ON SCHEMA medical_app_platform TO synthetic_backup_reader;
            GRANT SELECT ON medical_app_platform.members TO synthetic_backup_reader;
            CREATE TABLE medical_app_private.alembic_version (version_num varchar(32) PRIMARY KEY);
            INSERT INTO medical_app_private.alembic_version VALUES ('platform_0001');
            GRANT SELECT ON medical_app_private.alembic_version TO synthetic_backup_reader;
            REVOKE SELECT ON medical_app_private.alembic_version FROM synthetic_backup_reader;
        """
        )
        catalog = {name: backup._local_json(container, query)
                   for name, query in backup.CATALOG_QUERIES.items()}
        # PostgreSQL preserves an explicit owner-default ACL here, while pg_dump
        # can restore it as NULL (the same default). Normalize only NULL; an
        # actually empty ACL must still mean revoked privileges and fail equality.
        metadata_acl = [row for row in catalog["table_acl"]
                        if row[0:2] == ["medical_app_private", "alembic_version"]]
        assert len(metadata_acl) == 8
        backup.local_sql(container,
                         "CREATE TABLE medical_app_platform.revoked (id bigint);"
                         "REVOKE ALL ON medical_app_platform.revoked FROM postgres;")
        revoked_acl = backup._local_json(container, backup.CATALOG_QUERIES["table_acl"])
        assert not [row for row in revoked_acl if row[1] == "revoked"]
        backup.local_sql(container, "DROP TABLE medical_app_platform.revoked;")
        archives = {}
        for name, selection in (
            ("platform", ["--schema", "medical_app_platform"]),
            ("alembic", ["--table", "medical_app_private.alembic_version"]),
        ):
            raw = backup._docker(["exec", container, "pg_dump", "-U", "postgres", "-d", "postgres",
                                  "--format=custom", *selection])
            archives[name] = base64.b64encode(raw).decode("ascii")
    bundle = {"version": 1, "image": backup.IMAGE,
              "scope": [backup.PLATFORM_SCHEMA, backup.PRIVATE_SCHEMA + ".alembic_version"],
              "archives": archives, "inventory": {"catalog": catalog,
              "counts": {"medical_app_platform.members": 1, "medical_app_platform.notes": 1,
                         "medical_app_private.alembic_version": 1},
              "revisions": [["platform_0001"]], "roles": ["synthetic_backup_reader"]}}
    target = tmp_path / "synthetic-restore.hlpgbackup"
    backup.save_encrypted(bundle, target)
    result = backup.verify_local(backup.load_encrypted(target))
    assert result["tables_verified"] == 3
    assert result["source_rows"] == 3
    assert result["constraints_verified"] == 6
    assert result["disposable_container_removed"] is True
    assert result["catalog_and_acl_match"] is True
    assert "synthetic-private" not in repr(result)
