"""Opt-in public HTTPS acceptance with exact synthetic-account cleanup.

This is not run by app startup, imports, or normal pytest collection. The CLI
requires both a verified Render HTTPS origin and the platform schema marker.
Business checks use the real desktop HTTP client/facades, never an admin token.
Admin access is limited to catalog/absence checks and short cleanup transactions.
No AI provider is called. No password/token, response body or driver error is
included in the report. Test-only dependency injection is explicitly labelled.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from PIL import Image

from ollama_chat_app.services.chat_attachments import ChatAttachment
from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError, validate_base_url
from ollama_chat_app.services.remote_services import (
    RemoteAuthService,
    RemoteChatService,
    RemoteFileManagementService,
    RemoteHealthService,
)

from .platform_schema import PLATFORM_SCHEMA

_PREFIX = "public_verify_"
_SAFE_CODES = frozenset({
    "configuration", "authentication", "permission", "not_found", "conflict",
    "validation", "limited", "unavailable", "timeout", "network", "redirect",
    "protocol", "limit", "closed", "not_authenticated",
})
# These fixed identifiers are never derived from the URL, CLI, RPC, or response.
_DIRECT_OWNERS = {
    "member_profiles": ("user_id",), "profile_facts": ("user_id",),
    "life_records": ("user_id",), "reminders": ("user_id",),
    "advisor_profiles": ("user_id",), "advisor_bindings": ("member_user_id", "advisor_user_id"),
    "memberships": ("user_id",), "visit_tasks": ("member_user_id", "advisor_user_id"),
    "visit_records": ("member_user_id", "advisor_user_id"), "user_files": ("user_id",),
    "medical_reports": ("user_id",), "ai_insights": ("user_id",),
    "advisor_summaries": ("member_user_id", "advisor_user_id"), "app_settings": ("user_id",),
    "products": ("created_by_user_id",),
    "product_recommendations": ("member_user_id", "advisor_user_id"),
    "orders": ("member_user_id",), "conversations": ("user_id",),
    "platform_sessions": ("user_id",), "rpc_requests": ("user_id",),
    "audit_logs": ("actor_user_id",),
}


class PublicSmokeError(RuntimeError):
    def __init__(self, code="check_failed"):
        self.code = code if code in {
            "check_failed", "configuration", "cleanup_refused", "management_unavailable",
        } else "check_failed"
        super().__init__(self.code)


def validate_render_origin(value: str) -> str:
    """Only a Render public HTTPS origin, with no path, credentials or custom port."""
    try:
        validated = validate_base_url(value)
        parsed = urlsplit(validated)
        if (
            parsed.scheme != "https" or parsed.port not in {None, 443}
            or parsed.path or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.onrender\.com",
                                             parsed.hostname or "")
        ):
            raise ValueError
        return validated
    except Exception:
        raise PublicSmokeError("configuration") from None


@dataclass
class SyntheticRun:
    """An in-memory cleanup manifest, never an authorization input from the CLI."""

    marker: str = field(default_factory=lambda: _PREFIX + uuid4().hex)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attempted: set[str] = field(default_factory=set, repr=False)
    rejected: set[str] = field(default_factory=set, repr=False)
    known_ids: dict[str, int] = field(default_factory=dict, repr=False)
    preflight_verified: bool = False

    @property
    def usernames(self) -> tuple[str, str]:
        return self.marker + "_a", self.marker + "_b"

    def validate(self):
        if (
            not re.fullmatch(_PREFIX + r"[a-f0-9]{32}", self.marker)
            or self.started_at.tzinfo is None
            or not self.attempted <= set(self.usernames)
            or not self.rejected <= self.attempted
            or not set(self.known_ids) <= self.attempted
            or any(type(value) is not int or value <= 0 for value in self.known_ids.values())
            or len(set(self.known_ids.values())) != len(self.known_ids)
        ):
            raise PublicSmokeError("cleanup_refused")

    def remember(self, username: str, user):
        self.validate()
        if (
            username not in self.attempted or user.username != username
            or user.username_normalized != username or type(user.id) is not int or user.id <= 0
        ):
            raise PublicSmokeError()
        self.known_ids[username] = user.id
        self.validate()


def _query_users(connection, run: SyntheticRun, *, lock=False):
    return connection.execute(
        f"SELECT id,username,username_normalized,role_code,created_at "
        f"FROM {PLATFORM_SCHEMA}.users WHERE username_normalized IN (%s,%s)"
        + (" FOR UPDATE" if lock else ""), run.usernames,
    ).fetchall()


def _preflight(connection, run: SyntheticRun):
    run.validate()
    if run.attempted or run.known_ids or _query_users(connection, run):
        raise PublicSmokeError("cleanup_refused")
    run.preflight_verified = True


def _selected_ids(connection, table, field_name, values):
    if not values:
        return []
    placeholders = ",".join(["%s"] * len(values))
    return [row[0] for row in connection.execute(
        f"SELECT id FROM {PLATFORM_SCHEMA}.{table} WHERE {field_name} IN ({placeholders})",
        tuple(values),
    ).fetchall()]


def _cleanup_transaction(connection, run: SyntheticRun) -> dict:
    """All ownership guards precede DELETE; the caller must commit/rollback atomically."""
    run.validate()
    if not run.preflight_verified:
        raise PublicSmokeError("cleanup_refused")
    rows = _query_users(connection, run, lock=True)
    resolved = {}
    now = datetime.now(UTC)
    for row in rows:
        user_id, username, normalized, role, created = row
        created_at = datetime.fromisoformat(created) if isinstance(created, str) else created
        if (
            username not in run.attempted or normalized != username
            or type(user_id) is not int or user_id <= 0
            or role not in {"member", "advisor", "operator"}
            or not isinstance(created_at, datetime) or created_at.tzinfo is None
            or not run.started_at - timedelta(minutes=5) <= created_at <= now + timedelta(minutes=5)
            or (username in run.known_ids and run.known_ids[username] != user_id)
            or user_id in resolved.values()
        ):
            raise PublicSmokeError("cleanup_refused")
        resolved[username] = user_id
    # A successful registration cannot silently disappear before cleanup.
    if not set(run.known_ids) <= set(resolved):
        raise PublicSmokeError("cleanup_refused")
    # A transport error may precede a late server commit. Absence right now is
    # NOT proof of cleanup for a registration whose outcome was never confirmed.
    if run.attempted - run.rejected - set(resolved):
        raise PublicSmokeError("cleanup_refused")
    # An uncertain registration is resolved ONLY after exact name + prior absence
    # + this-run timestamp checks, never from a prefix/LIKE scan or an arbitrary ID.
    run.known_ids.update(resolved)
    ids = sorted(resolved.values())
    conversations = _selected_ids(connection, "conversations", "user_id", ids)
    messages = _selected_ids(connection, "messages", "conversation_id", conversations)
    files = _selected_ids(connection, "user_files", "user_id", ids)
    reports = _selected_ids(connection, "medical_reports", "user_id", ids)
    # This helper never creates staff, products, orders or cross-user service
    # relationships. Refuse unexpected related state instead of orphaning a
    # SET NULL row or removing someone else's relationship through a cascade.
    for table in ("advisor_profiles", "advisor_bindings", "memberships", "visit_tasks",
                  "visit_records", "advisor_summaries", "products",
                  "product_recommendations", "orders"):
        for column in _DIRECT_OWNERS[table]:
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                if connection.execute(
                    f"SELECT COUNT(*) FROM {PLATFORM_SCHEMA}.{table} "
                    f"WHERE {column} IN ({placeholders})", tuple(ids),
                ).fetchone()[0]:
                    raise PublicSmokeError("cleanup_refused")
    indirect = {
        "messages": ("conversation_id", conversations),
        "chat_attachments": ("message_id", messages),
        "user_file_contents": ("file_id", files), "report_items": ("report_id", reports),
    }
    for username, user_id in resolved.items():
        # audit_logs uses SET NULL, so remove these exact synthetic actors first.
        connection.execute(
            f"DELETE FROM {PLATFORM_SCHEMA}.audit_logs WHERE actor_user_id=%s", (user_id,),
        )
        deleted = connection.execute(
            f"DELETE FROM {PLATFORM_SCHEMA}.users "
            "WHERE id=%s AND username=%s AND username_normalized=%s",
            (user_id, username, username),
        )
        if deleted.rowcount != 1:
            raise PublicSmokeError("cleanup_refused")
    if _query_users(connection, run):
        raise PublicSmokeError("cleanup_refused")
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        for table, columns in _DIRECT_OWNERS.items():
            where = " OR ".join(f"{column} IN ({placeholders})" for column in columns)
            if connection.execute(
                f"SELECT COUNT(*) FROM {PLATFORM_SCHEMA}.{table} WHERE {where}",
                tuple(ids) * len(columns),
            ).fetchone()[0]:
                raise PublicSmokeError("cleanup_refused")
    for table, (column, values) in indirect.items():
        if values:
            placeholders = ",".join(["%s"] * len(values))
            if connection.execute(
                f"SELECT COUNT(*) FROM {PLATFORM_SCHEMA}.{table} "
                f"WHERE {column} IN ({placeholders})", tuple(values),
            ).fetchone()[0]:
                raise PublicSmokeError("cleanup_refused")
    return {"status": "verified", "accounts_removed": len(ids), "synthetic_rows_remaining": 0,
            "shared_rate_buckets_modified": False}


class ManagementCleanup:
    """Never hold a database transaction open during a public network operation."""

    def __init__(self):
        from . import platform_admin

        self.admin = platform_admin
        self.uri = platform_admin.management_url()

    def _verify_catalog(self, connection):
        if not self.admin._verified(self.admin._inspect(connection)):
            raise PublicSmokeError("management_unavailable")

    def preflight(self, run):
        with self.admin._connection(self.uri, readonly=True) as connection:
            self._verify_catalog(connection)
            _preflight(connection, run)

    def cleanup(self, run):
        with self.admin._connection(self.uri, readonly=False) as connection:
            self._verify_catalog(connection)
            result = _cleanup_transaction(connection, run)
        # The context's successful exit confirms commit, not just an in-tx check.
        with self.admin._connection(self.uri, readonly=True) as connection:
            if _query_users(connection, run):
                raise PublicSmokeError("cleanup_refused")
        return result


def _require(condition):
    if not condition:
        raise PublicSmokeError()


def _expect_status(statuses, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except CloudAPIError as error:
        _require(error.status_code in statuses)
    else:
        raise PublicSmokeError()


def _business_checks(clients, run, passwords, check):
    first, second, other = clients
    auth = [RemoteAuthService(client) for client in clients]
    check("health_live")
    _require(first.request("GET", "/health/live", authenticated=False).get("status") == "alive")
    check("health_ready")
    _require(first.request("GET", "/health/ready", authenticated=False).get("status") == "ready")
    check("unauthenticated_401")
    _expect_status({401}, first.request, "GET", "/v1/auth/me", authenticated=False)
    name, other_name = run.usernames
    check("registration_role_injection_422")
    run.attempted.add(name)
    _expect_status({422}, first.request, "POST", "/v1/auth/register", authenticated=False,
                   json={"username": name, "password": passwords[0], "role_code": "operator"})
    run.rejected.add(name)
    check("register_two_members")
    run.rejected.discard(name)
    member = auth[0].register(name, passwords[0])
    run.remember(name, member)
    run.attempted.add(other_name)
    outsider = auth[2].register(other_name, passwords[1])
    run.remember(other_name, outsider)
    _require(member.role_code == outsider.role_code == "member" and member.id != outsider.id)
    check("independent_clients_same_account")
    _require(auth[0].login(name, passwords[0]).id == member.id)
    _require(auth[1].login(name, passwords[0]).id == member.id)
    _require(auth[2].login(other_name, passwords[1]).id == outsider.id)
    chats = [RemoteChatService(client) for client in clients]
    check("multiple_conversations_idempotency")
    request_id = str(uuid4())
    conversation = chats[0].create_conversation(member.id, "合成公网验收对话一",
                                               request_id=request_id)
    repeated = chats[0].create_conversation(member.id, "合成公网验收对话一",
                                           request_id=request_id)
    _require(conversation == repeated)
    _expect_status({409}, chats[0].create_conversation, member.id, "不同合成内容",
                   request_id=request_id)
    another = chats[0].create_conversation(member.id, "合成公网验收对话二")
    listed = chats[1].list_conversations(member.id)
    _require(conversation.id != another.id and {conversation.id, another.id} <= {
        item.id for item in listed})
    check("small_image_message_idempotency")
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(stream, format="PNG")
    picture = stream.getvalue()
    digest = hashlib.sha256(picture).hexdigest()
    attachment = ChatAttachment("synthetic.png", "image/png", picture, "", "native_image", digest)
    request_id = str(uuid4())
    arguments = (member.id, "合成图片消息，未调用AI", "deepseek_cloud", "synthetic-no-ai-call")
    keywords = {"conversation_id": conversation.id, "attachments": (attachment,),
                "request_id": request_id}
    exchange = chats[0].begin_message(*arguments, **keywords)
    _require(chats[0].begin_message(*arguments, **keywords) == exchange)
    chats[0].complete_message(member.id, exchange.assistant_message.id, "固定合成回答，未调用AI")
    messages = chats[1].list_messages(member.id, conversation_id=conversation.id)
    _require(len(messages) == 2 and all(item.status == "complete" for item in messages))
    _require(len(chats[1].list_message_attachments(member.id, exchange.user_message.id)) == 1)
    context = chats[1].get_context(member.id, conversation_id=conversation.id)
    _require(any(hashlib.sha256(item.content).hexdigest() == digest
                 for turn in context for item in turn.attachments))
    check("different_account_isolation")
    _require(not {conversation.id, another.id} & {item.id for item in
                                                chats[2].list_conversations(outsider.id)})
    # Legacy service not-found maps to sanitized 422; both responses contain no data.
    _expect_status({404, 422}, chats[2].list_messages, outsider.id,
                   conversation_id=conversation.id)
    check("forged_actor_403")
    _expect_status({403}, RemoteHealthService(other).get_profile, member.id)
    check("beijing_health_time_roundtrip")
    health = RemoteHealthService(first)
    record_id = health.add_life_record(member.id, "water", "2026-09-07T00:30:00+08:00",
                                      "合成饮水记录", details={"amount_ml": 200})
    records = RemoteHealthService(second).list_life_records(member.id, start_date="2026-09-07",
                                                           end_date="2026-09-07")
    record = next(item for item in records if item["id"] == record_id)
    _require(str(record["local_date"]) == "2026-09-07"
             and record["timezone_offset_minutes"] == 480)
    instant = record["occurred_at"]
    _require(instant.astimezone(UTC) == datetime(2026, 9, 6, 16, 30, tzinfo=UTC))
    reminder_id = health.add_reminder(member.id, "合成提醒", "2030-01-01T09:00:00+08:00")
    reminder = next(item for item in RemoteHealthService(second).list_reminders(member.id)
                    if item["id"] == reminder_id)
    _require(reminder["scheduled_at"].astimezone(UTC) == datetime(2030, 1, 1, 1, tzinfo=UTC))
    _require(RemoteHealthService(other).list_life_records(outsider.id) == [])
    check("file_upload_download_hash_isolation")
    file_id = RemoteFileManagementService(first).upload_bytes(member.id, "synthetic.png", picture)
    downloaded = RemoteFileManagementService(second).get_file(member.id, file_id)
    _require(hashlib.sha256(downloaded["content"]).hexdigest() == digest)
    _expect_status({404, 422}, RemoteFileManagementService(other).get_file, outsider.id, file_id)
    check("logout_only_current_token")
    # Retain this synthetic token solely in memory to prove SERVER revocation.
    revoked_token = first._token
    first.logout()
    _require(not first.is_authenticated)
    first._token = revoked_token
    try:
        _expect_status({401}, first.me)
    finally:
        first.clear_session()
        revoked_token = None
    _require(auth[1].me().id == member.id and auth[2].me().id == outsider.id)


def run(*, base_url: str, confirm: str, client_factory=None, management=None) -> dict:
    """Only explicit calls do work; injected transports are labelled offline.

    The optional factories are for deterministic offline tests, not CLI switches.
    Unexpected/uncertain errors STOP the sequence; writes are never auto-retried.
    finally always attempts precise cleanup after a successful absence preflight.
    """
    stage, checks = "configuration", []
    report = {"status": "failed", "stage": stage, "code": "configuration",
              "mode": "offline_transport" if client_factory is not None else "public_https",
              "ai_called": False, "checks_passed": checks}
    clients, manifest = [], SyntheticRun()
    cleanup = {"status": "not_required", "shared_rate_buckets_modified": False}
    passwords = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]

    def check(next_stage):
        nonlocal stage
        if stage not in {"configuration", "management_preflight"}:
            checks.append(stage)
        stage = next_stage

    try:
        if confirm != PLATFORM_SCHEMA:
            raise PublicSmokeError("configuration")
        origin = validate_render_origin(base_url)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        stage = "management_preflight"
        management = management if management is not None else ManagementCleanup()
        management.preflight(manifest)
        factory = client_factory or CloudAPIClient
        for _ in range(3):
            clients.append(factory(origin))
        _business_checks(clients, manifest, passwords, check)
        checks.append(stage)
        report.update(status="passed", code="ok", stage="complete")
    except (Exception, KeyboardInterrupt) as error:
        code = "interrupted" if isinstance(error, KeyboardInterrupt) else "check_failed"
        if isinstance(error, PublicSmokeError):
            code = error.code
        elif isinstance(error, CloudAPIError):
            code = error.code if error.code in _SAFE_CODES else "check_failed"
        elif stage == "management_preflight":
            code = "management_unavailable"
        report.update(stage=stage, code=code)
    finally:
        passwords.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                report.update(status="failed", code="client_close_failed")
        if manifest.preflight_verified:
            try:
                cleanup = management.cleanup(manifest)
                if cleanup.get("status") != "verified":
                    raise PublicSmokeError("cleanup_refused")
            except (Exception, KeyboardInterrupt):
                cleanup = {"status": "failed", "code": "cleanup_unconfirmed",
                           "shared_rate_buckets_modified": False}
                report.update(status="failed", code="cleanup_unconfirmed")
        report["cleanup"] = cleanup
        report["run_marker"] = manifest.marker
    return report


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, '{"status":"failed","code":"arguments_required_or_invalid"}\n')


def main(argv=None):
    parser = _SafeParser(description="Explicit public HTTPS synthetic acceptance; no AI calls.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    result = run(base_url=args.base_url, confirm=args.confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
