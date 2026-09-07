from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import URL

from server import cloud_admin as admin
from server.schema import PRIVATE_SCHEMA


def test_confirmation_precedes_any_secret_or_network_access(monkeypatch):
    def reject(*_args, **_kwargs):
        raise AssertionError("unexpected access")

    monkeypatch.setattr(admin, "inspect_catalog", reject)
    monkeypatch.setattr(admin, "management_url", reject)
    with pytest.raises(admin.CloudAdminError, match="明确确认"):
        admin.migrate(confirm="")


@pytest.mark.parametrize("failure", [False, True])
def test_migration_environment_restored_without_printing_secret(monkeypatch, capsys, failure):
    monkeypatch.setenv("SERVER_MIGRATION_CONFIRM", "prior-value")
    monkeypatch.delenv("SERVER_MIGRATION_DATABASE_URL", raising=False)
    url = URL.create("postgresql+psycopg", username="synthetic", password="synthetic-secret")
    try:
        with admin.migration_environment(url):
            assert os.environ["SERVER_MIGRATION_CONFIRM"] == PRIVATE_SCHEMA
            assert "synthetic-secret" in os.environ["SERVER_MIGRATION_DATABASE_URL"]
            if failure:
                raise ValueError("synthetic interruption")
    except ValueError:
        pass
    assert os.environ["SERVER_MIGRATION_CONFIRM"] == "prior-value"
    assert "SERVER_MIGRATION_DATABASE_URL" not in os.environ
    assert capsys.readouterr().out == ""


def test_unknown_existing_schema_never_migrated(monkeypatch):
    monkeypatch.setattr(admin, "inspect_catalog", lambda *_: {"schema_exists": True})
    with pytest.raises(admin.CloudAdminError, match="未确认"):
        admin.migrate(confirm=PRIVATE_SCHEMA)


def test_migration_error_preserves_unknown_commit_status_and_hides_secret(monkeypatch):
    monkeypatch.setattr(admin, "inspect_catalog", lambda *_: {"schema_exists": False})
    monkeypatch.setattr(admin, "management_url", lambda *_: URL.create("postgresql+psycopg"))

    def reject(*_args):
        raise RuntimeError("secret-should-never-leak")

    monkeypatch.setattr(admin.command, "upgrade", reject)
    with pytest.raises(admin.CloudAdminError, match="提交状态需重新只读核实") as error:
        admin.migrate(confirm=PRIVATE_SCHEMA)
    assert "secret-should-never-leak" not in str(error.value)


def test_success_requires_post_migration_privilege_verification(monkeypatch):
    reports = iter(
        [
            {"schema_exists": False},
            {
                "schema_exists": True,
                "schema_owned": True,
                "expected_table_set": True,
                "revisions": ["pilot_0001"],
                "data_api_roles_no_access": False,
            },
        ]
    )
    monkeypatch.setattr(admin, "inspect_catalog", lambda *_: next(reports))
    monkeypatch.setattr(admin, "management_url", lambda *_: URL.create("postgresql+psycopg"))
    monkeypatch.setattr(admin.command, "upgrade", lambda *_: None)
    with pytest.raises(admin.CloudAdminError, match="权限或结构"):
        admin.migrate(confirm=PRIVATE_SCHEMA)


def test_invalid_management_payload_fails_closed(monkeypatch):
    monkeypatch.setattr(admin, "load_secret_payload", lambda *_: {"secret": "hidden-value"})
    with pytest.raises(admin.CloudAdminError) as error:
        admin.management_url()
    assert "hidden-value" not in str(error.value)
