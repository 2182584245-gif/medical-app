from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ollama_chat_app.data.database import DEFAULT_CONVERSATION_TITLE, Database
from ollama_chat_app.services.auth import (
    AuthorizationError,
    AuthService,
    RegistrationError,
)


@pytest.fixture
def auth_service(tmp_path: Path) -> tuple[Database, AuthService]:
    database = Database(tmp_path / "app.sqlite3")
    return database, AuthService(database)


def test_bootstrap_operator_is_single_atomic_account_with_audit(
    auth_service: tuple[Database, AuthService],
) -> None:
    database, auth = auth_service
    password = "operator-password-123"

    assert auth.has_operator() is False
    operator = auth.bootstrap_operator("  ＯＰＳ  ", password)

    assert auth.has_operator() is True
    assert operator.username == "OPS"
    assert operator.username_normalized == "ops"
    assert operator.role_code == "operator"
    assert operator.account_status == "active"
    assert auth.authenticate("ops", password).id == operator.id

    with database.connect() as connection:
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (operator.id,)
        ).fetchone()
        conversation = connection.execute(
            "SELECT title FROM conversations WHERE user_id = ?", (operator.id,)
        ).fetchone()
        audit = connection.execute(
            """
            SELECT actor_user_id, action, entity_type, entity_id, details_json
            FROM audit_logs
            WHERE entity_type = 'user' AND entity_id = ?
            """,
            (operator.id,),
        ).fetchone()

    assert stored is not None
    assert str(stored["password_hash"]).startswith("$argon2id$")
    assert str(stored["password_hash"]) != password
    assert conversation is not None
    assert str(conversation["title"]) == DEFAULT_CONVERSATION_TITLE
    assert audit is not None
    assert int(audit["actor_user_id"]) == operator.id
    assert str(audit["action"]) == "operator.bootstrapped"
    assert str(audit["entity_type"]) == "user"
    assert int(audit["entity_id"]) == operator.id
    assert json.loads(str(audit["details_json"])) == {"role_code": "operator"}

    with pytest.raises(RegistrationError, match="已经初始化"):
        auth.bootstrap_operator("another-operator", "another-password-123")

    with database.connect() as connection:
        operator_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE role_code = 'operator'"
            ).fetchone()[0]
        )
    assert operator_count == 1


def test_concurrent_bootstrap_allows_at_most_one_operator(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3", timeout_seconds=10.0)
    database.initialize()

    def bootstrap(index: int) -> tuple[str, str]:
        service = AuthService(Database(database.path, timeout_seconds=10.0))
        try:
            user = service.bootstrap_operator(f"operator-{index}", f"concurrent-password-{index}")
        except RegistrationError as error:
            return "error", str(error)
        return "ok", user.username

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bootstrap, range(2)))

    assert [status for status, _message in results].count("ok") == 1
    assert [status for status, _message in results].count("error") == 1
    assert any("已经初始化" in message for status, message in results if status == "error")
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE role_code = 'operator'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 1


def test_only_active_operator_can_create_staff_and_roles_are_restricted(
    auth_service: tuple[Database, AuthService],
) -> None:
    database, auth = auth_service
    operator = auth.bootstrap_operator("operator", "operator-password")
    member = auth.register("member", "member-password")

    with pytest.raises(AuthorizationError, match="启用中的运营账号"):
        auth.create_staff(member.id, "forbidden", "forbidden-password", "advisor")

    advisor = auth.create_staff(operator.id, "advisor-one", "advisor-password", "advisor")
    second_operator = auth.create_staff(
        operator.id, "operator-two", "operator-two-password", "operator"
    )
    assert advisor.role_code == "advisor"
    assert second_operator.role_code == "operator"

    with pytest.raises(AuthorizationError, match="启用中的运营账号"):
        auth.create_staff(advisor.id, "forbidden-two", "forbidden-password", "advisor")
    with pytest.raises(RegistrationError, match="员工角色"):
        auth.create_staff(operator.id, "staff-member", "staff-password", "member")

    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET account_status = 'disabled' WHERE id = ?", (operator.id,)
        )
    with pytest.raises(AuthorizationError, match="启用中的运营账号"):
        auth.create_staff(operator.id, "forbidden-three", "forbidden-password", "advisor")

    with database.connect() as connection:
        forbidden_count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE username_normalized LIKE 'forbidden%'"
        ).fetchone()[0]
    assert forbidden_count == 0


def test_staff_account_has_hash_default_conversation_and_audit(
    auth_service: tuple[Database, AuthService],
) -> None:
    database, auth = auth_service
    operator = auth.bootstrap_operator("operator", "operator-password")
    password = "advisor-secret-password"

    advisor = auth.create_staff(operator.id, "  Advisor  ", password, "advisor")

    with database.connect() as connection:
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (advisor.id,)
        ).fetchone()
        conversation = connection.execute(
            "SELECT title FROM conversations WHERE user_id = ?", (advisor.id,)
        ).fetchone()
        audit = connection.execute(
            """
            SELECT actor_user_id, action, entity_type, entity_id, details_json
            FROM audit_logs
            WHERE action = 'staff.created' AND entity_id = ?
            """,
            (advisor.id,),
        ).fetchone()

    assert stored is not None
    assert str(stored["password_hash"]).startswith("$argon2id$")
    assert password not in str(stored["password_hash"])
    assert password.encode() not in database.path.read_bytes()
    assert conversation is not None
    assert str(conversation["title"]) == DEFAULT_CONVERSATION_TITLE
    assert audit is not None
    assert int(audit["actor_user_id"]) == operator.id
    assert str(audit["entity_type"]) == "user"
    assert int(audit["entity_id"]) == advisor.id
    assert json.loads(str(audit["details_json"])) == {"role_code": "advisor"}

    with pytest.raises(RegistrationError, match="用户名已存在"):
        auth.create_staff(operator.id, "ADVISOR", "different-password", "advisor")
