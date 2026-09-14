"""Manager report/confirmation gates; no credentials or network."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from tools.aliyun_platform import bootstrap
from tools.aliyun_platform import upgrade_medical as upgrade


def test_frozen_v2_ddl_digest_unchanged():
    # Exact historical statements are preserved; only v3 appends its one ALTER.
    statements = bootstrap.ddl(revision="platform_0002")
    assert bootstrap.ddl()[:-1] == statements
    assert bootstrap.ddl()[-1] == upgrade.ALTER_CATEGORY_SQL
    assert (
        bootstrap.ddl_digest(revision="platform_0002")
        == hashlib.sha256("\n".join(statements).encode()).hexdigest()
    )
    assert bootstrap.ddl_digest() != bootstrap.ddl_digest(revision="platform_0002")
    # Independently recomputed from the pre-v3 git version's frozen ddl() body.
    assert bootstrap.ddl_digest(revision="platform_0002") == (
        "0bf19be5af1c901590c4c1490f766e989cffcc3bf8ae585334ca5f4db33660cd"
    )


def test_reports_are_exclusive_and_do_not_overwrite(tmp_path):
    path = tmp_path / "plan.json"
    value = {"credentials_exported": False, "plan_sha256": "a" * 64}
    upgrade.write_report(path, value)
    before = path.read_bytes()
    assert upgrade.read_report(path) == value
    with pytest.raises(upgrade.DeploymentError):
        upgrade.write_report(path, {"replace": True})
    assert path.read_bytes() == before
    path = tmp_path / "large.json"
    path.write_bytes(b"x" * 65537)
    with pytest.raises(upgrade.DeploymentError):
        upgrade.read_report(path)


@pytest.mark.parametrize("state", [TransactionStatus.INTRANS, TransactionStatus.INERROR])
def test_existing_transaction_is_never_adopted(state):
    connection = SimpleNamespace(info=SimpleNamespace(transaction_status=state))
    with pytest.raises(upgrade.DeploymentError, match="Idle"):
        upgrade.plan_upgrade(connection, {"id": "a" * 32})
    with pytest.raises(upgrade.DeploymentError, match="Idle"):
        upgrade.apply_upgrade(connection, {}, {}, None)


@pytest.mark.parametrize("confirmation", [None, "", "a" * 63, "b" * 64])
def test_confirmation_precedes_any_database_operation(confirmation):
    connection = SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
    with pytest.raises(upgrade.DeploymentError, match="SHA256"):
        upgrade.apply_upgrade(connection, {}, {"plan_sha256": "a" * 64}, confirmation)


def test_cli_wrong_deployment_refuses_before_secret_or_connection(monkeypatch, capsys):
    monkeypatch.setattr(upgrade, "container_gate", lambda: {"id": "a" * 32})
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(upgrade, "secret", lambda _path: pytest.fail("must not read credentials"))
    result = upgrade.main(["status", "--deployment-id", "b" * 32])
    assert result == 1
    assert capsys.readouterr().out == (
        '{"status":"medical_upgrade_not_verified","automatic_retry":false}\n'
    )


def test_missing_output_parent_is_not_created(tmp_path):
    path = tmp_path / "missing" / "plan.json"
    with pytest.raises(upgrade.DeploymentError):
        upgrade.write_report(path, {})
    assert not Path(path.parent).exists()


def test_aliyun_build_includes_complete_medical_upgrade_and_proxy_dependencies():
    root = Path(__file__).resolve().parents[2]
    whitelist = (root / "tools/aliyun_platform/Dockerfile.dockerignore").read_text()
    for name in (
        "server/platform_ai_proxy.py",
        "server/platform_medical_schema.py",
        "server/platform_experience_schema.py",
        "server/platform_schema.py",
        "src/ollama_chat_app/data/medical_schema.py",
        "tools/aliyun_platform/upgrade_medical.py",
        "tools/aliyun_platform/bootstrap.py",
        "tools/aliyun_platform/guard.py",
    ):
        assert "!" + name in whitelist.splitlines()
    assert "!src/ollama_chat_app/ui/" not in whitelist
    assert "!outputs/" not in whitelist and "!data/" not in whitelist
