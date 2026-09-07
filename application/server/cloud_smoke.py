"""Explicit, rollback-only pilot API acceptance checks.

This is an administrator-side test helper, not a deployed HTTP server. Two
in-process TestClients make sequential requests through the real API handlers
using one SQLAlchemy connection. It does NOT establish that real HTTPS,
independent database connections, concurrent access, or production limits work.
No AI provider is contacted. All test data is synthetic and rolled back.
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import DateTime, Uuid, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from .app import create_app, get_db
from .config import Settings
from .models import Base, Conversation, LoginSession, Message, User, UTCDateTime
from .schema import PRIVATE_SCHEMA

_ERROR_CODES = frozenset(
    {
        "confirmation_required",
        "configuration_unavailable",
        "test_settings_required",
        "readiness_failed",
        "api_check_failed",
        "transaction_lost",
        "rollback_failed",
        "synthetic_rows_remain",
        "database_or_test_unavailable",
        "runtime_security_failed",
        "native_types_failed",
        "permission_boundary_failed",
    }
)


class CloudSmokeError(RuntimeError):
    """Only a fixed, public error code may cross the command-line boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "database_or_test_unavailable"
        super().__init__(self.code)


def _require(condition: bool, code: str = "api_check_failed") -> None:
    if not condition:
        raise CloudSmokeError(code)


def _exercise(first: TestClient, second: TestClient, usernames: tuple[str, str]) -> list[str]:
    """Keep every response, password, and bearer token strictly inside this process."""
    checks = []

    def request(client, method, path, expected, **kwargs):
        response = client.request(method, path, **kwargs)
        _require(response.status_code == expected)
        return None if expected == 204 else response.json()

    accounts = [{"username": name, "password": secrets.token_urlsafe(32)} for name in usernames]
    registered = [request(first, "POST", "/auth/register", 201, json=item) for item in accounts]
    _require(registered[0]["id"] != registered[1]["id"])
    checks.append("two_synthetic_registrations")

    def login(client, account):
        result = request(client, "POST", "/auth/login", 200, json=account)
        _require(isinstance(result.get("access_token"), str))
        return {"Authorization": "Bearer " + result["access_token"]}

    owner_first = login(first, accounts[0])
    owner_second = login(second, accounts[0])
    outsider = login(second, accounts[1])
    _require(owner_first != owner_second)
    for client, headers in ((first, owner_first), (second, owner_second)):
        _require(request(client, "GET", "/auth/me", 200, headers=headers) == registered[0])
    checks.append("login_and_independent_tokens")

    conversation_payload = {"title": "合成验收对话", "request_id": str(uuid4())}
    conversation = request(
        first, "POST", "/conversations", 201, headers=owner_first, json=conversation_payload
    )
    _require(
        request(
            second,
            "POST",
            "/conversations",
            200,
            headers=owner_second,
            json=conversation_payload,
        )
        == conversation
    )
    request(
        first,
        "POST",
        "/conversations",
        409,
        headers=owner_first,
        json={**conversation_payload, "title": "合成冲突标题"},
    )
    _require(request(second, "GET", "/conversations", 200, headers=owner_second) == [conversation])
    checks.extend(
        [
            "conversation_retry_idempotent",
            "conversation_conflict_409",
            "same_account_conversation_visible",
        ]
    )

    path = "/conversations/" + conversation["id"]
    message_path = path + "/messages"
    message_payload = {
        "request_id": str(uuid4()),
        "role": "user",
        "content": "  合成验收消息🙂  ",
        "provider": "synthetic",
        "model": "no-ai-call",
    }
    _require(request(second, "GET", "/conversations", 200, headers=outsider) == [])
    request(second, "GET", message_path, 404, headers=outsider)
    request(second, "PATCH", path, 404, headers=outsider, json={"title": "无权改名"})
    request(second, "POST", message_path, 404, headers=outsider, json=message_payload)
    checks.append("different_account_404_isolation")

    message = request(first, "POST", message_path, 201, headers=owner_first, json=message_payload)
    _require(message["sequence_no"] == 1 and message["content"] == message_payload["content"])
    _require(
        request(second, "POST", message_path, 200, headers=owner_second, json=message_payload)
        == message
    )
    request(
        second,
        "POST",
        message_path,
        409,
        headers=owner_second,
        json={**message_payload, "content": "合成冲突消息"},
    )
    _require(request(second, "GET", message_path, 200, headers=owner_second) == [message])
    answer = request(
        second,
        "POST",
        message_path,
        201,
        headers=owner_second,
        json={
            "request_id": str(uuid4()),
            "role": "assistant",
            "content": "固定合成回答，不调用 AI",
        },
    )
    _require(answer["sequence_no"] == 2)
    _require(request(first, "GET", message_path, 200, headers=owner_first) == [message, answer])
    checks.extend(
        [
            "message_retry_idempotent",
            "message_conflict_409",
            "same_account_messages_visible",
            "message_sequence_preserved",
        ]
    )

    renamed = request(
        second, "PATCH", path, 200, headers=owner_second, json={"title": "合成验收已改名"}
    )
    _require(renamed["title"] == "合成验收已改名")
    _require(request(first, "GET", "/conversations", 200, headers=owner_first) == [renamed])
    checks.append("rename_visible_to_other_client")
    request(first, "POST", "/auth/logout", 204, headers=owner_first)
    request(first, "GET", "/auth/me", 401, headers=owner_first)
    _require(request(second, "GET", "/auth/me", 200, headers=owner_second) == registered[0])
    checks.append("logout_revokes_only_used_token")
    return checks


def _remaining_synthetic_rows(connection, usernames: tuple[str, str]) -> int:
    """Scope verification to generated identifiers; never inspect existing account rows."""
    user_ids = select(User.id).where(User.username_normalized.in_(usernames))
    conversation_ids = select(Conversation.id).where(Conversation.user_id.in_(user_ids))
    predicates = (
        (User, User.username_normalized.in_(usernames)),
        (LoginSession, LoginSession.user_id.in_(user_ids)),
        (Conversation, Conversation.user_id.in_(user_ids)),
        (Message, Message.conversation_id.in_(conversation_ids)),
    )
    return sum(
        int(connection.scalar(select(func.count()).select_from(model).where(predicate)))
        for model, predicate in predicates
    )


def _postgres_checks(connection, settings: Settings) -> list[str]:
    """Check the real PostgreSQL role/TLS/types; never use administrator credentials."""
    if connection.dialect.name != "postgresql":
        return []
    uri = make_url(settings.database_url.get_secret_value())
    _require(
        uri.query.get("sslmode") == "verify-full" and bool(uri.query.get("sslrootcert")),
        "runtime_security_failed",
    )
    _require(
        bool(connection.connection.driver_connection.pgconn.ssl_in_use), "runtime_security_failed"
    )
    _require(
        connection.exec_driver_sql("SELECT current_user").scalar_one() == "medical_app_runtime",
        "runtime_security_failed",
    )
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables:
        reflected = {
            column["name"]: column["type"]
            for column in inspector.get_columns(table.name, schema=PRIVATE_SCHEMA)
        }
        for column in table.columns:
            if isinstance(column.type, Uuid):
                _require(isinstance(reflected.get(column.name), Uuid), "native_types_failed")
            if isinstance(column.type, UTCDateTime):
                native = reflected.get(column.name)
                _require(isinstance(native, DateTime) and native.timezone, "native_types_failed")
    # These statements cannot change business rows even if a grant is unexpectedly
    # present. Each attempted statement is wrapped in a rollback-only savepoint.
    forbidden = (
        f"SELECT version_num FROM {PRIVATE_SCHEMA}.alembic_version LIMIT 0",
        f"DELETE FROM {PRIVATE_SCHEMA}.pilot_users WHERE FALSE",
        f"UPDATE {PRIVATE_SCHEMA}.pilot_messages SET content=content WHERE FALSE",
    )
    for statement in forbidden:
        denied = False
        savepoint = connection.begin_nested()
        try:
            connection.exec_driver_sql(statement)
        except DBAPIError as error:
            denied = getattr(error.orig, "sqlstate", None) == "42501"
        finally:
            savepoint.rollback()
        _require(denied, "permission_boundary_failed")
    return [
        "runtime_role_and_database_tls",
        "native_uuid_and_timestamptz",
        "forbidden_sql_permission_denied",
    ]


def run_smoke(settings: Settings) -> dict[str, object]:
    """Run only when called explicitly; every API commit stays inside a savepoint."""
    if settings.env != "test":
        raise CloudSmokeError("test_settings_required")
    application = None
    try:
        application = create_app(settings)
        engine = application.state.database.engine
        usernames = ("smoke_" + uuid4().hex, "smoke_" + uuid4().hex)
        # The URL is simulated by TestClient, not a TLS connection to a web server.
        with (
            TestClient(application, base_url="https://testserver") as first,
            TestClient(application, base_url="https://testserver") as second,
        ):
            ready = first.get("/health/ready")
            _require(
                ready.status_code == 200 and ready.json().get("status") == "ready",
                "readiness_failed",
            )
            with engine.connect() as connection:
                transaction = connection.begin()
                if engine.dialect.name == "sqlite":
                    # sqlite3 legacy transaction mode otherwise releases the first
                    # SAVEPOINT as a real commit. Materialize the outer BEGIN first.
                    connection.exec_driver_sql("BEGIN")

                def isolated_session():
                    with Session(
                        bind=connection,
                        expire_on_commit=False,
                        join_transaction_mode="create_savepoint",
                    ) as session:
                        yield session

                application.dependency_overrides[get_db] = isolated_session
                try:
                    checks = _postgres_checks(connection, settings)
                    checks += _exercise(first, second, usernames)
                    _require(transaction.is_active, "transaction_lost")
                finally:
                    application.dependency_overrides.pop(get_db, None)
                    try:
                        transaction.rollback()
                    except Exception:
                        raise CloudSmokeError("rollback_failed") from None
                _require(
                    _remaining_synthetic_rows(connection, usernames) == 0, "synthetic_rows_remain"
                )
                # The scoped verification SELECT starts another read transaction.
                connection.rollback()
            ready = first.get("/health/ready")
            _require(
                ready.status_code == 200 and ready.json().get("status") == "ready",
                "readiness_failed",
            )
        return {
            "status": "passed_rolled_back",
            "database_backend": engine.dialect.name,
            "checks": ["ready_before", *checks, "rollback_verified", "ready_after"],
            "synthetic_accounts": 2,
            "synthetic_records_persisted": 0,
            "rollback_completed": True,
            "schema_changed": False,
            "real_user_records_read": 0,
            "ai_calls": 0,
            "transport": "in_process_testclient_sequential_shared_db_connection",
            "network_https_tested": False,
            "concurrent_requests_tested": False,
        }
    except CloudSmokeError:
        raise
    except Exception:
        raise CloudSmokeError("database_or_test_unavailable") from None
    finally:
        if application is not None:
            application.dependency_overrides.pop(get_db, None)
            application.state.database.engine.dispose()


def _load_settings(profile: Path | None) -> Settings:
    # Lazy import: help and missing-confirmation paths never read local credentials.
    from .cloud_runtime import DEFAULT_RUNTIME_PROFILE, load_runtime_settings

    return load_runtime_settings(profile or DEFAULT_RUNTIME_PROFILE, test_client=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit rollback-only pilot API smoke test")
    parser.add_argument("--confirm", help="Must equal medical_app_private; no default execution")
    parser.add_argument("--profile", type=Path, help="Encrypted runtime profile, never a URI")
    args = parser.parse_args(argv)
    if args.confirm != PRIVATE_SCHEMA:
        print(json.dumps({"status": "not_run", "error": "confirmation_required"}))
        return 2
    try:
        try:
            settings = _load_settings(args.profile)
        except Exception:
            raise CloudSmokeError("configuration_unavailable") from None
        report = run_smoke(settings)
    except CloudSmokeError as error:
        print(json.dumps({"status": "failed", "error": error.code}))
        return 3
    except Exception:
        print(json.dumps({"status": "failed", "error": "database_or_test_unavailable"}))
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
