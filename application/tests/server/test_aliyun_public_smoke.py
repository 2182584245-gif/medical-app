"""Synthetic HTTP transport only; never contact the public deployment."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from platform_test_support import PEPPER, SyntheticDatabase

from ollama_chat_app.security.private_payload import read_private_json
from ollama_chat_app.services.cloud_client import CloudAPIClient
from server.platform_app import create_app
from server.platform_config import PlatformSettings

pytest.importorskip("PySide6.QtCore", reason="Desktop-Python DPAPI/outbox acceptance test")
from tools.aliyun_platform import public_smoke as smoke  # noqa: E402


@pytest.fixture
def context():
    database = SyntheticDatabase()
    untouched = database.account("preexisting-synthetic-never-touched")
    settings = PlatformSettings(
        env="test", database_url="sqlite+pysqlite:///:memory:", token_pepper=PEPPER,
        allowed_hosts=["39.106.166.15"], require_https=True,
    )
    app = create_app(settings, database=database)
    requests, clients = [], []
    state = SimpleNamespace(drop_registration_response=False)
    with TestClient(app, base_url="https://39.106.166.15",
                    raise_server_exceptions=False) as api:
        def send(request):
            assert request.url.scheme == "https" and request.url.host == "39.106.166.15"
            prefix, path = request.url.path[1:].split("/", 1)
            assert prefix in {"aliyun", "supabase"}
            assert "/ai/" not in path
            requests.append(request)
            response = api.request(request.method, "/" + path, content=request.content,
                                   headers=dict(request.headers))
            if state.drop_registration_response and path == "v1/auth/register":
                # Server committed, but receipt must NOT imply failure or create another user.
                raise httpx.ReadTimeout("NEVER_PRINT_response_password_token", request=request)
            return httpx.Response(response.status_code, content=response.content,
                                  headers=response.headers)

        def factory(url):
            assert url in smoke.ENDPOINTS.values()
            client = CloudAPIClient(url, transport=httpx.MockTransport(send))
            clients.append(client)
            return client

        yield SimpleNamespace(database=database, factory=factory, requests=requests,
                              clients=clients, state=state, untouched=untouched)
    database.close()


def _assert_no_secrets(report, context, directory):
    serialized = json.dumps(report)
    assert "NEVER_PRINT" not in serialized
    assert "合成验收昵称" not in serialized and "合成公网验收饮水" not in serialized
    for request in context.requests:
        authorization = request.headers.get("authorization")
        if authorization:
            assert authorization.split(" ", 1)[1] not in serialized
        if request.url.path.endswith(("/register", "/login")):
            password = json.loads(request.content)["password"]
            assert password not in serialized
            for file in directory.rglob("*"):
                if file.is_file():
                    assert password.encode() not in file.read_bytes()
    assert all(not client.is_authenticated for client in context.clients)


@pytest.mark.parametrize("route", ["aliyun", "supabase"])
def test_complete_synthetic_http_and_dpapi_receipt(context, tmp_path, route):
    directory = tmp_path / "new-run"
    report = smoke.run(route=route, confirm=True, report_directory=directory,
                       client_factory=context.factory)
    assert report["status"] == "passed", report
    assert set(report["checks"]) == {
        "ready_and_unauthenticated_denial", "register_two_fresh_members",
        "login_and_protocol_2_complete_manifest",
        "encrypted_outbox_same_uuid_exactly_one_record_and_second_client_sync",
        "stale_queue_version_conflict_preserves_cloud_value",
        "file_upload_second_client_download_sha256",
        "protocol_2_file_chunks_sha256_and_reopened_encrypted_cache",
        "cross_account_snapshot_forgery_and_file_access_denied",
        "all_three_synthetic_sessions_logged_out",
    }
    assert len(report["record_ids"]) == len(report["file_ids"]) == 1
    assert len(report["accounts"]) == 2
    assert smoke.inspect_receipt(directory) == report
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 3  # No auto cleanup.
    assert context.database.scalar("SELECT COUNT(*) FROM life_records") == 1
    assert context.database.scalar("SELECT COUNT(*) FROM user_files") == 1
    assert context.database.scalar(
        "SELECT COUNT(*) FROM life_records WHERE user_id=?", (context.untouched,)) == 0
    assert context.database.scalar(
        "SELECT COUNT(*) FROM audit_logs WHERE actor_user_id=?", (context.untouched,)) == 0
    payload = read_private_json(directory / "credentials.dpapi", smoke._PURPOSE + "credentials")
    assert [row["username"] for row in payload["accounts"]] == [
        row["username"] for row in report["accounts"]]
    assert payload["endpoint"] == smoke.ENDPOINTS[route]
    _assert_no_secrets(report, context, directory)
    replay = [row for row in report["operations"] if row["name"] in {
        "queue_water_record", "replay_same_uuid"}]
    assert len(replay) == 2 and replay[0]["request_id"] == replay[1]["request_id"]
    assert replay[0]["result_id"] == replay[1]["result_id"]
    assert all(urlsplit(str(item.url)).path.startswith("/" + route + "/")
               for item in context.requests)
    chunks = [json.loads(item.content)["args"][-1] for item in context.requests
              if item.url.path.endswith("/get_file_chunk")]
    assert 0 in chunks and 65536 in chunks


def test_lost_registration_response_keeps_exact_intent_without_retry(context, tmp_path):
    context.state.drop_registration_response = True
    directory = tmp_path / "uncertain-run"
    report = smoke.run(route="aliyun", confirm=True, report_directory=directory,
                       client_factory=context.factory)
    assert report["status"] == "failed" and report["error_code"] == "timeout"
    assert report["accounts"][0]["registration"] == "attempted_result_unconfirmed"
    assert report["accounts"][0]["id"] is None
    assert report["accounts"][1]["registration"] == "not_attempted"
    assert len([r for r in context.requests if r.url.path.endswith("/register")]) == 1
    assert context.database.scalar("SELECT COUNT(*) FROM users") == 2
    assert smoke.inspect_receipt(directory) == report
    _assert_no_secrets(report, context, directory)
    before = len(context.requests)
    with pytest.raises(smoke.SmokeError, match="configuration"):
        smoke.run(route="aliyun", confirm=True, report_directory=directory,
                  client_factory=context.factory)
    assert len(context.requests) == before  # Cannot reuse/overwrite a failed receipt.


@pytest.mark.parametrize(("route", "confirm"), [
    ("aliyun", False), ("aliyun", 1), ("http://39.106.166.15/aliyun", True),
    ("https://39.106.166.15/supabase", True), ("elsewhere", True),
])
def test_gate_prevents_network_and_directory_creation(tmp_path, route, confirm):
    directory = tmp_path / "not-created"
    def forbidden(_url):
        pytest.fail("client created before explicit route/confirmation gate")
    with pytest.raises(smoke.SmokeError, match="configuration"):
        smoke.run(route=route, confirm=confirm, report_directory=directory,
                  client_factory=forbidden)
    assert not directory.exists()


def test_failed_dpapi_prevents_registration_and_error_is_fixed(context, tmp_path, monkeypatch):
    original = smoke.write_private_json
    def fail_credentials(path, purpose, value):
        if path.name == "credentials.dpapi":
            raise OSError("NEVER_PRINT_response_password_token")
        return original(path, purpose, value)
    monkeypatch.setattr(smoke, "write_private_json", fail_credentials)
    directory = tmp_path / "encryption-failure"
    report = smoke.run(route="aliyun", confirm=True, report_directory=directory,
                       client_factory=context.factory)
    assert report["status"] == "failed" and report["error_code"] == "check_failed"
    assert not context.requests and not context.clients
    assert smoke.inspect_receipt(directory) == report
    assert "NEVER_PRINT" not in json.dumps(report)


def test_cli_does_not_accept_a_url_or_tls_override(capsys):
    assert smoke.main([]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    with pytest.raises(SystemExit):
        smoke.main(["--route", "aliyun", "--confirm", "--insecure"])
