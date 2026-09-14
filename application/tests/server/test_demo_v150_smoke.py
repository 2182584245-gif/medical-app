"""Real in-memory API plus mocked HTTPS; never uses imported/live credentials."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from platform_test_support import PASSWORD
from test_aliyun_public_smoke import context  # noqa: F401

from ollama_chat_app.services.cloud_client import CloudAPIClient
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc
from ollama_chat_app.services.health import HealthService
from tools.aliyun_platform import demo_v150_smoke as smoke


@pytest.fixture
def demo(context, tmp_path):  # noqa: F811
    accounts = {}
    health = HealthService(context.database)
    for username, role in smoke.ACCOUNTS.items():
        actor = context.database.account(username, role)
        accounts[username] = actor
        if role == "member":
            for category in smoke.CATEGORIES:
                health.add_life_record(
                    actor,
                    category,
                    datetime.now(UTC),
                    "合成原始示例",
                    details={"event_type": "consultation"} if category == "medical" else {},
                )
    path = tmp_path / "DEMO_ACCOUNTS.md"
    labels = {value: key for key, value in smoke.ROLE_LABELS.items()}
    path.write_text(
        "| 角色 | 昵称 | 账号 | 初始密码 |\n|---|---|---|---|\n"
        + "\n".join(
            f"| {labels[role]} | 合成 | {username} | {PASSWORD} |"
            for username, role in smoke.ACCOUNTS.items()
        ),
        encoding="utf-8",
    )
    context.accounts = accounts
    context.source = path
    context.health = health
    return context


def _assert_receipt_private(report, demo, directory):
    serialized = json.dumps(report)
    assert PASSWORD not in serialized and "合成原始示例" not in serialized
    assert "NEVER_PRINT" not in serialized
    for request in demo.requests:
        if token := request.headers.get("authorization"):
            assert token.split(" ", 1)[1] not in serialized
        assert not request.url.path.endswith("/register") and "/ai/" not in request.url.path
    for path in directory.rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes()
    assert all(not client.is_authenticated for client in demo.clients)


def test_six_sessions_complete_six_categories_and_only_returned_id_deleted(demo, tmp_path):
    initial = {
        name: demo.health.list_life_records(actor)
        for name, actor in demo.accounts.items()
        if name.startswith("demo-member")
    }
    source_before = demo.source.read_bytes()
    directory = tmp_path / "new-run"
    report = smoke.run(
        accounts_file=demo.source,
        execute=True,
        report_directory=directory,
        client_factory=demo.factory,
    )
    assert report["status"] == "passed", report
    assert len(report["accounts"]) == 5 and len(demo.clients) == 6
    assert report["baseline_record_counts"] == [6, 6]
    assert report["snapshot_schema_version"] == 2
    assert len(report["record_ids"]) == 1
    assert smoke.inspect_receipt(directory) == report
    assert demo.source.read_bytes() == source_before
    for name, records in initial.items():
        assert demo.health.list_life_records(demo.accounts[name]) == records
    assert demo.database.scalar("SELECT COUNT(*) FROM users") == 6
    assert demo.database.scalar("SELECT COUNT(*) FROM life_records") == 12
    assert (
        demo.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE actor_user_id=?", (demo.untouched,)
        )
        == 0
    )
    writes = [
        json.loads(request.content)
        for request in demo.requests
        if request.url.path.endswith(("/apply_queued", "/delete_life_record"))
    ]
    assert len(writes) == 3
    assert writes[0]["request_id"] == writes[1]["request_id"]
    assert decode_rpc(writes[2]["args"]) == [
        demo.accounts["demo-member-1"],
        report["record_ids"][0],
    ]
    member_ids = {demo.accounts["demo-member-1"], demo.accounts["demo-member-2"]}
    for request in demo.requests:
        if request.url.path.endswith(("/get_sync_manifest", "/get_sync_page")):
            assert decode_rpc(json.loads(request.content)["args"])[0] in member_ids
    _assert_receipt_private(report, demo, directory)


def test_lost_create_response_retains_uuid_without_retry_or_delete(demo, tmp_path):
    def factory(url):
        client = demo.factory(url)
        original = client.rpc

        def rpc(service, method, args, kwargs, **options):
            result = original(service, method, args, kwargs, **options)
            if method == "apply_queued":
                from ollama_chat_app.services.cloud_client import CloudAPIError

                raise CloudAPIError("timeout", outcome_uncertain=True)
            return result

        client.rpc = rpc
        return client

    directory = tmp_path / "uncertain"
    report = smoke.run(
        accounts_file=demo.source, execute=True, report_directory=directory, client_factory=factory
    )
    assert report["status"] == "failed" and report["error_code"] == "timeout"
    creates = [op for op in report["operations"] if op["name"] == "create_only_this_record"]
    assert len(creates) == 1 and creates[0]["request_id"]
    assert creates[0]["status"] == "attempted_result_unconfirmed"
    assert report["record_ids"] == []
    assert not any("delete_life_record" in str(request.url) for request in demo.requests)
    assert demo.database.scalar("SELECT COUNT(*) FROM life_records") == 13
    _assert_receipt_private(report, demo, directory)


def test_baseline_returned_id_is_never_deleted(demo, tmp_path):
    original_id = demo.health.list_life_records(demo.accounts["demo-member-1"])[0]["id"]

    def factory(url):
        client = demo.factory(url)
        original = client.rpc

        def rpc(service, method, args, kwargs, **options):
            result = original(service, method, args, kwargs, **options)
            return original_id if method == "apply_queued" else result

        client.rpc = rpc
        return client

    report = smoke.run(
        accounts_file=demo.source,
        execute=True,
        report_directory=tmp_path / "bad-response",
        client_factory=factory,
    )
    assert report["status"] == "failed"
    assert report["record_ids"] == []  # Never advertise an original row as a cleanup target.
    assert not any("delete_life_record" in str(request.url) for request in demo.requests)
    assert demo.database.scalar("SELECT COUNT(*) FROM life_records") == 13


@pytest.mark.parametrize(
    "execute,endpoint",
    [
        (False, smoke.ENDPOINT),
        (1, smoke.ENDPOINT),
        (True, "http://39.106.166.15/aliyun"),
        (True, "https://39.106.166.15/supabase"),
        (True, "https://example.com/aliyun"),
    ],
)
def test_gate_prevents_source_read_client_and_new_directory(tmp_path, execute, endpoint):
    def forbidden(_):
        pytest.fail("Client must not be constructed")

    directory = tmp_path / "not-created"
    with pytest.raises(smoke.SmokeError):
        smoke.run(
            accounts_file=tmp_path / "missing",
            execute=execute,
            endpoint=endpoint,
            report_directory=directory,
            client_factory=forbidden,
        )
    assert not directory.exists()


def test_wrong_account_source_is_rejected_before_network(demo, tmp_path):
    demo.source.write_text(
        demo.source.read_text(encoding="utf-8").replace(
            "demo-member-2", "existing-real-account-not-allowed"
        ),
        encoding="utf-8",
    )
    with pytest.raises(smoke.SmokeError, match="configuration"):
        smoke.run(
            accounts_file=demo.source,
            execute=True,
            report_directory=tmp_path / "no-run",
            client_factory=demo.factory,
        )
    assert not demo.requests and not demo.clients


def test_cli_default_is_no_network_and_does_not_read_password_file(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("No source/network access without --run")

    monkeypatch.setattr(smoke, "read_accounts", forbidden)
    monkeypatch.setattr(CloudAPIClient, "login", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    assert smoke.main([]) == 0
    assert json.loads(capsys.readouterr().out)["network_requests"] == 0
