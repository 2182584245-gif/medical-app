"""Explicit real-Postgres, rollback-only business/RLS acceptance.

This uses one administrator connection and SET LOCAL ROLE for business checks.
The existing postgres role administrator temporarily enables its SET option
inside the same rollback-only transaction; the original membership is verified
unchanged afterwards. No permanent grant is made to the runtime or postgres.
It is not an independent-connection, concurrent, or public-HTTPS test. Runtime
login/readiness is checked separately. No real records or AI provider are read.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import secrets
import traceback
from contextlib import contextmanager
from uuid import uuid4

from PIL import Image

from ollama_chat_app.data.database import timestamp_to_db, user_from_row, utc_now
from ollama_chat_app.security.passwords import hash_password
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat_attachments import ChatAttachment
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc

from .cloud_connection import DEFAULT_PROFILE
from .cloud_runtime import _admin_url
from .platform_app import create_app
from .platform_database import PlatformDatabase, ServiceConnection
from .platform_rpc import RpcDispatcher, RpcError
from .platform_schema import PLATFORM_RUNTIME_ROLE, PLATFORM_SCHEMA


class BoundDatabase(PlatformDatabase):
    """An outer transaction always belongs to this test, never to services."""

    def bind(self, connection):
        self.bound_connection = connection

    @contextmanager
    def connect(self):
        self._apply_identity(self.bound_connection)
        yield ServiceConnection(self.bound_connection, writable=True)

    @contextmanager
    def transaction(self, *, immediate=True):
        with self.bound_connection.begin_nested():
            self._apply_identity(self.bound_connection)
            yield ServiceConnection(self.bound_connection, writable=True)


def run(*, confirm: str) -> dict:
    if confirm != PLATFORM_SCHEMA:
        raise ValueError("Explicit platform confirmation is required")
    from .platform_admin import load_runtime_settings

    settings = load_runtime_settings(test_client=True)
    runtime = PlatformDatabase(settings)
    try:
        from .platform_auth import PlatformAuth

        runtime.initialize(force=True)
        PlatformAuth(runtime, settings).check_ready()
    finally:
        runtime.close()
    checks = ["dedicated_runtime_login_and_readiness"]
    database = BoundDatabase(_admin_url(DEFAULT_PROFILE).set(drivername="postgresql+psycopg"))
    marker = "verify_" + uuid4().hex[:16]
    stage = "begin"
    try:
        with database.engine.connect() as connection:
            outer = connection.begin()
            database.bind(connection)
            membership_sql = (
                "SELECT m.admin_option,m.inherit_option,m.set_option "
                "FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member "
                "JOIN pg_roles target ON target.oid=m.roleid "
                "WHERE member.rolname='postgres' AND target.rolname=%s"
            )
            original_membership = connection.exec_driver_sql(
                membership_sql, (PLATFORM_RUNTIME_ROLE,)
            ).fetchone()
            try:
                if original_membership is None or not original_membership[0]:
                    raise ValueError("Existing administrator membership required")
                connection.exec_driver_sql(f"SET LOCAL search_path={PLATFORM_SCHEMA},pg_catalog")
                connection.exec_driver_sql("SET LOCAL statement_timeout='15000ms'")
                connection.exec_driver_sql("SELECT pg_advisory_xact_lock(717380021,1)")
                stage = "synthetic_fixture"
                users = []
                password_hash = hash_password(secrets.token_urlsafe(32))
                for index, role in enumerate(("operator", "advisor", "member", "member")):
                    row = AuthService._insert_account(
                        ServiceConnection(connection, writable=True),
                        display_username=f"{marker}_{index}",
                        normalized_username=f"{marker}_{index}",
                        password_hash=password_hash,
                        role_code=role,
                        now=timestamp_to_db(utc_now()),
                    )
                    users.append(user_from_row(row))
                operator, advisor, member, other = users
                stage = "transaction_only_role_switch"
                connection.exec_driver_sql(
                    f"GRANT {PLATFORM_RUNTIME_ROLE} TO postgres WITH SET TRUE"
                )
                connection.exec_driver_sql(f"SET LOCAL ROLE {PLATFORM_RUNTIME_ROLE}")
                app = create_app(settings, database=database)
                rpc = RpcDispatcher(database, app.state.services)

                def call(actor, service, method, *args, request_id=None, **kwargs):
                    nonlocal stage
                    stage = service + "." + method
                    with database.identity(actor.id, actor.role_code):
                        result = rpc.invoke(
                            actor.id,
                            service,
                            method,
                            {
                                "args": encode_rpc([actor.id, *args]),
                                "kwargs": encode_rpc(kwargs),
                                "request_id": request_id or str(uuid4()),
                            },
                        )
                    checks.append(stage)
                    return decode_rpc(result["result"])

                call(
                    operator,
                    "service_management",
                    "save_advisor_profile",
                    advisor.id,
                    display_name="合成验收顾问",
                )
                call(operator, "service_management", "bind_advisor", member.id, advisor.id)
                call(
                    operator,
                    "service_management",
                    "save_membership",
                    member.id,
                    plan_code="合成会员方案",
                    starts_at="2026-01-01T00:00:00+08:00",
                    ends_at="2030-01-01T00:00:00+08:00",
                    benefits={"visit_total": 4, "visit_used": 0},
                )
                call(member, "health", "save_profile", {"display_name": "合成会员"})
                life_id = call(
                    member,
                    "health",
                    "add_life_record",
                    "water",
                    "2026-09-07T09:00:00+08:00",
                    "合成饮水记录",
                    details={"amount_ml": 200},
                )
                assert life_id > 0
                call(member, "health", "get_dashboard")
                call(member, "health", "get_record_statistics")
                reminder = call(
                    member, "health", "add_reminder", "合成日程", "2030-01-01T09:00:00+08:00"
                )
                call(
                    member,
                    "health",
                    "update_reminder",
                    reminder,
                    scheduled_at="2030-01-02T10:00:00+08:00",
                )
                call(member, "health", "list_reminders")
                call(advisor, "service_management", "get_member_overview", member.id)
                task = call(
                    operator,
                    "service_management",
                    "create_visit_task",
                    member.id,
                    title="合成上门",
                    advisor_user_id=advisor.id,
                    scheduled_at="2026-09-07T09:00:00+08:00",
                )
                task_id = task["id"] if isinstance(task, dict) else task
                call(advisor, "service_management", "start_visit_task", task_id)
                call(
                    advisor,
                    "service_management",
                    "complete_visit_task",
                    task_id,
                    summary="合成验收已完成",
                    visited_at="2026-09-07T10:00:00+08:00",
                    next_visit_at="2030-01-01T10:00:00+08:00",
                    details={"uses_membership_benefit": True},
                )
                call(operator, "service_management", "get_advisor_work_statistics")
                product = call(
                    operator,
                    "commerce",
                    "create_product",
                    sku=marker,
                    name="合成水杯",
                    category="测试",
                    price_cents=1299,
                    is_active=True,
                )
                recommendation = call(
                    advisor,
                    "commerce",
                    "recommend_product",
                    member.id,
                    product["id"],
                    "合成推荐原因",
                )
                call(member, "commerce", "mark_interested", recommendation["id"])
                order = call(
                    member,
                    "commerce",
                    "simulate_purchase",
                    product["id"],
                    quantity=2,
                    recommendation_id=recommendation["id"],
                )
                call(operator, "commerce", "mark_order_delivered", order["id"])
                call(member, "commerce", "reorder", order["id"])
                call(member, "commerce", "list_orders")
                call(advisor, "commerce", "list_recommendations")
                call(operator, "service_management", "get_dashboard")
                stream = io.BytesIO()
                Image.new("RGB", (2, 2), "white").save(stream, format="PNG")
                picture = stream.getvalue()
                file_id = call(member, "files", "upload_bytes", "synthetic.png", picture)
                assert call(member, "files", "get_file", file_id)["content"] == picture
                report_id = call(
                    member,
                    "files",
                    "create_report_draft",
                    file_id,
                    report_date="2026-09-07",
                    summary="合成报告非真实医疗记录",
                )
                item_id = call(
                    member,
                    "files",
                    "add_report_item_draft",
                    report_id,
                    "合成项目",
                    result_value="1",
                    flag="unknown",
                )
                call(member, "files", "confirm_report_item", report_id, item_id)
                call(member, "files", "confirm_report", report_id)
                call(member, "files", "get_report", report_id)
                conversation = call(member, "chat", "create_conversation", title="合成对话")
                attachment = ChatAttachment(
                    "synthetic.png",
                    "image/png",
                    picture,
                    "",
                    "native_image",
                    hashlib.sha256(picture).hexdigest(),
                )
                request_id = str(uuid4())
                exchange = call(
                    member,
                    "chat",
                    "begin_message",
                    "合成消息",
                    "deepseek_cloud",
                    "synthetic-no-model-call",
                    conversation_id=conversation.id,
                    attachments=(attachment,),
                    request_id=request_id,
                )
                repeat = call(
                    member,
                    "chat",
                    "begin_message",
                    "合成消息",
                    "deepseek_cloud",
                    "synthetic-no-model-call",
                    conversation_id=conversation.id,
                    attachments=(attachment,),
                    request_id=request_id,
                )
                assert repeat == exchange
                call(
                    member,
                    "chat",
                    "complete_message",
                    exchange.assistant_message.id,
                    "固定合成回答，未调用AI",
                )
                assert call(member, "chat", "get_context", conversation_id=conversation.id)
                call(member, "ai", "build_member_context")
                prepared = call(
                    member, "ai", "prepare_member_draft", "synthetic", "no-ai", "整理已有生活记录"
                )
                draft = call(
                    member,
                    "ai",
                    "finish_member_draft",
                    prepared["ticket"],
                    json.dumps({"answer": "固定合成回答", "proposals": []}),
                )
                assert draft
                stage = "direct_rls_missing_where_isolation"
                with database.identity(other.id, other.role_code), database.connect() as db:
                    assert db.execute("SELECT id FROM life_records").fetchall() == []
                    assert db.execute("SELECT id FROM user_files").fetchall() == []
                    assert db.execute("SELECT id FROM messages").fetchall() == []
                with database.identity(advisor.id, advisor.role_code), database.connect() as db:
                    assert [r[0] for r in db.execute("SELECT id FROM life_records")] == [life_id]
                checks.append(stage)
                stage = "rpc_actor_forgery"
                with database.identity(other.id, other.role_code):
                    try:
                        rpc.invoke(
                            other.id,
                            "health",
                            "list_life_records",
                            {"args": [member.id], "kwargs": {}, "request_id": str(uuid4())},
                        )
                    except RpcError as error:
                        assert error.status == 403
                    else:
                        raise AssertionError
                checks.append(stage)
            finally:
                outer.rollback()
            stage = "rollback_verified"
            assert (
                connection.exec_driver_sql(membership_sql, (PLATFORM_RUNTIME_ROLE,)).fetchone()
                == original_membership
            )
            remaining = connection.exec_driver_sql(
                f"SELECT COUNT(*) FROM {PLATFORM_SCHEMA}.users WHERE username_normalized LIKE %s",
                (marker + "%",),
            ).scalar_one()
            assert remaining == 0
            checks.append(stage)
    except Exception as error:
        # Fixed stage names and SQLSTATE are safe; never print SQL or values.
        return {
            "status": "failed",
            "stage": stage,
            "error_type": type(error).__name__,
            "sqlstate": getattr(error, "sqlstate", None)
            or getattr(getattr(error, "orig", None), "sqlstate", None),
            "frames": [
                f"{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)
            ],
            "checks_passed": checks,
            "rollback_requested": True,
        }
    finally:
        database.close()
    return {
        "status": "passed",
        "checks": checks,
        "synthetic_rows_remaining": 0,
        "mode": "real-postgres-single-connection-rollback",
        "ai_called": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        result = run(confirm=args.confirm)
    except Exception as error:
        result = {
            "status": "unavailable",
            "detail": "configuration_or_database_unavailable",
            "error_type": type(error).__name__,
            "frames": [
                f"{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
