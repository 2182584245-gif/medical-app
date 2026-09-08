"""Synthetic-only HTTPS checks for the isolated local Compose deployment.

No configurable origin, cloud credentials, database connection or AI provider is
used here. Only a fixed container service may be contacted. The private state
volume contains newly generated synthetic credentials, never real app data, so
the same history can be verified after restarting both API and database.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import secrets
import ssl
import stat
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image

from ollama_chat_app.services.chat_attachments import ChatAttachment
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc

API_ORIGIN = "https://api:8443"
CA_FILE = Path("/run/api-secrets/ca.crt")
STATE_FILE = Path("/run/smoke-state/state.json")
_PREFIX = "local_smoke_"
_USER_TEXT = "本地容器合成消息，不调用 AI"
_ANSWER_TEXT = "固定合成回答，不调用 AI"
_TITLES = ("本地容器合成会话一", "本地容器合成会话二")
_STATE_FIELDS = {
    "version", "marker", "accounts", "conversation_ids", "message_ids",
    "file_id", "image_sha256", "record_id", "reminder_id",
}


class LocalSmokeError(RuntimeError):
    def __init__(self, code="check_failed"):
        self.code = code if code in {
            "configuration", "check_failed", "state_exists", "state_invalid",
            "state_missing", "network", "tls", "timeout",
        } else "check_failed"
        super().__init__(self.code)


def _require(condition, code="check_failed"):
    if not condition:
        raise LocalSmokeError(code)


def _create_client():
    return httpx.Client(
        base_url=API_ORIGIN,
        verify=ssl.create_default_context(cafile=str(CA_FILE)),
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(30, connect=10),
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    )


def _request(client, method, path, *, status=200, **kwargs):
    response = client.request(method, path, **kwargs)
    _require(response.status_code == status)
    if status == 204:
        _require(not response.content)
        return None
    if status != 200 and status != 201:
        # Error bodies are intentionally not decoded, logged or retained.
        return None
    _require(len(response.content) < 1024 * 1024)
    result = response.json()
    _require(isinstance(result, dict))
    return result


def _payload(args, kwargs, request_id=None):
    return {
        "args": encode_rpc(list(args)),
        "kwargs": encode_rpc(kwargs),
        "request_id": request_id or str(uuid4()),
    }


def _rpc(client, service, method, *args, request_id=None, _status=200, **kwargs):
    response = _request(
        client, "POST", f"/v1/rpc/{service}/{method}", status=_status,
        json=_payload(args, kwargs, request_id),
    )
    if _status != 200:
        return None
    _require(set(response) == {"result"})
    return decode_rpc(response["result"])


def _login(client, account):
    result = _request(client, "POST", "/v1/auth/login", json={
        "username": account["username"], "password": account["password"],
    })
    _require(result["user"]["id"] == account["id"])
    _require(result["user"]["role_code"] == "member")
    _require(re.fullmatch(r"[A-Za-z0-9_-]{43}", result["access_token"]) is not None)
    client.headers["Authorization"] = "Bearer " + result["access_token"]


def _picture():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    return output.getvalue()


def _validate_state(state):
    _require(isinstance(state, dict) and set(state) == _STATE_FIELDS, "state_invalid")
    _require(type(state["version"]) is int and state["version"] == 1, "state_invalid")
    _require(isinstance(state["marker"], str) and re.fullmatch(
        _PREFIX + r"[a-f0-9]{32}", state["marker"],
    ) is not None, "state_invalid")
    accounts = state["accounts"]
    _require(isinstance(accounts, list) and len(accounts) == 2, "state_invalid")
    for account, suffix in zip(accounts, ("_a", "_b"), strict=True):
        _require(isinstance(account, dict) and set(account) == {
            "id", "username", "password",
        }, "state_invalid")
        _require(account["username"] == state["marker"] + suffix, "state_invalid")
        _require(isinstance(account["password"], str) and re.fullmatch(
            r"LocalOnly!9_[A-Za-z0-9_-]{43}", account["password"],
        ) is not None, "state_invalid")
    ids = [account["id"] for account in accounts]
    for field in ("conversation_ids", "message_ids"):
        values = state[field]
        _require(isinstance(values, list) and len(values) == 2, "state_invalid")
        _require(len(set(values)) == 2, "state_invalid")
        ids.extend(values)
    ids.extend(state[field] for field in ("file_id", "record_id", "reminder_id"))
    _require(all(type(value) is int and 0 < value < 2**63 for value in ids), "state_invalid")
    _require(accounts[0]["id"] != accounts[1]["id"], "state_invalid")
    _require(state["image_sha256"] == hashlib.sha256(_picture()).hexdigest(), "state_invalid")


def _load_state():
    try:
        metadata = STATE_FILE.lstat()
        _require(stat.S_ISREG(metadata.st_mode), "state_invalid")
        _require(0 < metadata.st_size <= 8192, "state_invalid")
        if os.name == "posix":
            _require(stat.S_IMODE(metadata.st_mode) == 0o600, "state_invalid")
            _require(metadata.st_uid == os.getuid(), "state_invalid")
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        _validate_state(state)
        return state
    except FileNotFoundError:
        raise LocalSmokeError("state_missing") from None
    except LocalSmokeError:
        raise
    except Exception:
        raise LocalSmokeError("state_invalid") from None


def _save_state(state):
    _validate_state(state)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(STATE_FILE, flags, 0o600)
    except FileExistsError:
        raise LocalSmokeError("state_exists") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def _register(clients, check, report):
    marker = _PREFIX + uuid4().hex
    accounts = [
        {"username": marker + suffix, "password": "LocalOnly!9_" + secrets.token_urlsafe(32)}
        for suffix in ("_a", "_b")
    ]
    with check("registration_role_injection_rejected"):
        _request(clients[0], "POST", "/v1/auth/register", status=422,
                 json={**accounts[0], "role_code": "operator"})
    with check("register_two_synthetic_members"):
        for client, account in zip((clients[0], clients[2]), accounts, strict=True):
            user = _request(client, "POST", "/v1/auth/register", status=201, json=account)
            _require(user["username"] == account["username"] and user["role_code"] == "member")
            _require(type(user["id"]) is int and user["id"] > 0)
            account["id"] = user["id"]
            report["synthetic_data_retained"] = True
        _require(accounts[0]["id"] != accounts[1]["id"])
    return {"version": 1, "marker": marker, "accounts": accounts}


def _create_history(clients, state, check):
    first = clients[0]
    actor = state["accounts"][0]["id"]
    picture = _picture()
    state["image_sha256"] = hashlib.sha256(picture).hexdigest()
    with check("multiple_conversations_and_idempotency"):
        request_id = str(uuid4())
        initial = _rpc(first, "chat", "create_conversation", actor, _TITLES[0],
                       request_id=request_id)
        repeated = _rpc(first, "chat", "create_conversation", actor, _TITLES[0],
                        request_id=request_id)
        _require(initial == repeated)
        _rpc(first, "chat", "create_conversation", actor, _TITLES[1],
             request_id=request_id, _status=409)
        second = _rpc(first, "chat", "create_conversation", actor, _TITLES[1])
        _require(initial.id != second.id)
        state["conversation_ids"] = [initial.id, second.id]
    with check("synthetic_image_chat_without_ai"):
        attachment = ChatAttachment(
            "synthetic.png", "image/png", picture, "", "native_image", state["image_sha256"],
        )
        request_id = str(uuid4())
        arguments = (actor, _USER_TEXT, "deepseek_cloud", "synthetic-no-ai-call")
        keywords = {"conversation_id": initial.id, "attachments": (attachment,)}
        exchange = _rpc(first, "chat", "begin_message", *arguments,
                        request_id=request_id, **keywords)
        _require(exchange == _rpc(first, "chat", "begin_message", *arguments,
                                 request_id=request_id, **keywords))
        _rpc(first, "chat", "complete_message", actor, exchange.assistant_message.id, _ANSWER_TEXT)
        state["message_ids"] = [exchange.user_message.id, exchange.assistant_message.id]
    with check("file_upload"):
        state["file_id"] = _rpc(first, "files", "upload_bytes", actor, "synthetic.png", picture)
    with check("beijing_time_records_created"):
        state["record_id"] = _rpc(
            first, "health", "add_life_record", actor, "water", "2026-09-07T00:30:00+08:00",
            "合成饮水记录", details={"amount_ml": 200},
        )
        state["reminder_id"] = _rpc(
            first, "health", "add_reminder", actor, "合成提醒", "2030-01-01T09:00:00+08:00",
        )


def _verify_history(clients, state, check):
    _, second, outsider = clients
    actor, other = (account["id"] for account in state["accounts"])
    conversation = state["conversation_ids"][0]
    with check("independent_client_history_sync"):
        listed = _rpc(second, "chat", "list_conversations", actor)
        by_id = {item.id: item for item in listed}
        _require(all(by_id[identifier].title == title for identifier, title in zip(
            state["conversation_ids"], _TITLES, strict=True,
        )))
        messages = _rpc(second, "chat", "list_messages", actor, conversation_id=conversation)
        _require([item.id for item in messages] == state["message_ids"])
        _require([item.content for item in messages] == [_USER_TEXT, _ANSWER_TEXT])
        _require(all(item.status == "complete" for item in messages))
        attachments = _rpc(second, "chat", "list_message_attachments", actor,
                           state["message_ids"][0])
        _require(len(attachments) == 1)
        context = _rpc(second, "chat", "get_context", actor, conversation_id=conversation)
        _require(any(hashlib.sha256(item.content).hexdigest() == state["image_sha256"]
                     for turn in context for item in turn.attachments))
    with check("file_download_hash_matches"):
        downloaded = _rpc(second, "files", "get_file", actor, state["file_id"])
        _require(hashlib.sha256(downloaded["content"]).hexdigest() == state["image_sha256"])
    with check("cross_user_access_rejected"):
        outside_conversations = _rpc(outsider, "chat", "list_conversations", other)
        _require(not set(state["conversation_ids"]) & {item.id for item in outside_conversations})
        _rpc(outsider, "health", "get_profile", actor, _status=403)
        # Existing service contract sanitizes inaccessible resources as 422.
        _rpc(outsider, "chat", "list_messages", other, conversation_id=conversation, _status=422)
        _rpc(outsider, "files", "get_file", other, state["file_id"], _status=422)
    with check("beijing_time_roundtrip"):
        records = _rpc(second, "health", "list_life_records", actor,
                       start_date="2026-09-07", end_date="2026-09-07")
        record = next(item for item in records if item["id"] == state["record_id"])
        _require(str(record["local_date"]) == "2026-09-07")
        _require(record["timezone_offset_minutes"] == 480)
        _require(record["occurred_at"].astimezone(UTC) == datetime(2026, 9, 6, 16, 30, tzinfo=UTC))
        reminders = _rpc(second, "health", "list_reminders", actor)
        reminder = next(item for item in reminders if item["id"] == state["reminder_id"])
        _require(reminder["scheduled_at"].astimezone(UTC) == datetime(2030, 1, 1, 1, tzinfo=UTC))
        _require(_rpc(outsider, "health", "list_life_records", other) == [])


def run(*, verify_persistence=False, _client_factory=None):
    """Run fixed local checks; injected clients are explicitly labelled offline.

No request is retried automatically. The state is never overwritten, and only
new synthetic credentials are saved to the private dedicated Docker volume.
"""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    report = {
        "status": "failed", "stage": "configuration", "code": "configuration",
        "mode": "offline_contract_test" if _client_factory else "local_container_https",
        "operation": "verify_persistence" if verify_persistence else "create_synthetic_history",
        "cloud_connected": False, "ai_called": False, "synthetic_data_retained": False,
        "checks_passed": [],
    }

    @contextmanager
    def check(name):
        report["stage"] = name
        yield
        report["checks_passed"].append(name)

    clients = []
    try:
        _require(os.environ.get("LOCAL_CONTAINER_ONLY") == "1", "configuration")
        if verify_persistence:
            state = _load_state()
            report["synthetic_data_retained"] = True
        else:
            _require(not os.path.lexists(STATE_FILE), "state_exists")
            _require(STATE_FILE.parent.is_dir(), "configuration")
            state = None
        with ExitStack() as stack:
            factory = _client_factory or _create_client
            clients = [stack.enter_context(factory()) for _ in range(3)]
            try:
                with check("https_health_live_and_ready"):
                    _require(_request(clients[0], "GET", "/health/live")["status"] == "alive")
                    _require(_request(clients[0], "GET", "/health/ready")["status"] == "ready")
                with check("unauthenticated_and_untrusted_host_rejected"):
                    _request(clients[0], "GET", "/v1/auth/me", status=401)
                    _request(clients[0], "POST", "/v1/rpc/files/upload_bytes", status=401,
                             content=b"not an authenticated upload")
                    _request(clients[0], "GET", "/health/live", status=400,
                             headers={"Host": "untrusted.invalid"})
                if state is None:
                    state = _register(clients, check, report)
                with check("three_independent_authenticated_clients"):
                    for client, account in zip(clients, (
                        state["accounts"][0], state["accounts"][0], state["accounts"][1],
                    ), strict=True):
                        _login(client, account)
                        _require(_request(client, "GET", "/v1/auth/me")["id"] == account["id"])
                if not verify_persistence:
                    _create_history(clients, state, check)
                _verify_history(clients, state, check)
                with check("logout_revokes_only_current_session"):
                    _request(clients[0], "POST", "/v1/auth/logout", status=204)
                    _request(clients[0], "GET", "/v1/auth/me", status=401)
                    clients[0].headers.pop("Authorization", None)
                    _require(_request(clients[1], "GET", "/v1/auth/me")["id"]
                             == state["accounts"][0]["id"])
                    _require(_request(clients[2], "GET", "/v1/auth/me")["id"]
                             == state["accounts"][1]["id"])
                with check("remaining_synthetic_sessions_revoked"):
                    for client in clients[1:]:
                        _request(client, "POST", "/v1/auth/logout", status=204)
                        _request(client, "GET", "/v1/auth/me", status=401)
                        client.headers.pop("Authorization", None)
                if not verify_persistence:
                    with check("private_synthetic_persistence_state_saved"):
                        _save_state(state)
                report.update(
                    status="passed", stage="complete", code="ok",
                    counts={"accounts": 2, "verified_conversations": 2, "messages": 2,
                            "chat_images": 1, "files": 1, "life_records": 1, "reminders": 1},
                )
            finally:
                for client in clients:
                    if "Authorization" in client.headers:
                        with suppress(Exception):
                            _request(client, "POST", "/v1/auth/logout", status=204)
                        client.headers.pop("Authorization", None)
    except LocalSmokeError as error:
        report["code"] = error.code
    except httpx.TimeoutException:
        report["code"] = "timeout"
    except (ssl.SSLError, ssl.CertificateError):
        report["code"] = "tls"
    except httpx.HTTPError:
        report["code"] = "network"
    except Exception:
        report["code"] = "check_failed"
    report["check_count"] = len(report["checks_passed"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Isolated local HTTPS synthetic checks only")
    parser.add_argument("--verify-persistence", action="store_true")
    arguments = parser.parse_args(argv)
    report = run(verify_persistence=arguments.verify_persistence)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
