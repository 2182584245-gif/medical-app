from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

MOBILE_SOURCE = Path(__file__).resolve().parents[1] / "android/app/src/main/python"
sys.path.insert(0, str(MOBILE_SOURCE))
mobile_api = importlib.import_module("mobile_api")
mobile_providers = importlib.import_module("mobile_providers")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_DUAL_CHAT_DATA_DIR", str(tmp_path / "unused"))
    result = json.loads(mobile_api.initialize(str(tmp_path / "data")))
    assert result["ok"]
    yield mobile_api
    mobile_api._APP.logout()


def call(action, **params):
    return json.loads(
        mobile_api.dispatch(json.dumps({"id": "test-1", "action": action, "params": params}))
    )


def good(action, **params):
    result = call(action, **params)
    assert result["ok"], result
    return result["result"]


def member(name="member"):
    return good("auth.register", username=name, password="android-test-password")


def operator():
    return good("auth.bootstrap_operator", username="operator", password="android-test-password")


def login(name):
    return good("auth.login", username=name, password="android-test-password")


def test_initial_state_no_network_and_beijing(api, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("No automatic network requests")

    monkeypatch.setattr(httpx.Client, "send", fail)
    state = good("auth.state")
    assert state["user"] is None and not state["has_operator"]
    assert state["can_import"] and not state["can_backup"]
    assert state["beijing_now"].endswith("+08:00")
    member()
    assert good("provider.status")["provider"] == "ollama_cloud"
    good("provider.configure", provider="local", local_url="http://192.168.1.2:11434")
    assert good("system.check")["network_tested"] is False


def test_account_login_logout_hash_and_key_not_persisted(api):
    user = member()
    assert user["role_code"] == "member" and "password_hash" not in user
    good("provider.configure", provider="deepseek", api_key="sk-secret-android-test")
    assert good("provider.status")["has_api_key"]
    assert "sk-secret" not in json.dumps(good("provider.status"))
    with api._APP.database.connect() as connection:
        row = connection.execute("SELECT password_hash FROM users").fetchone()
        assert row[0].startswith("$argon2id$")
        dump = "\n".join(connection.iterdump())
        assert "sk-secret-android-test" not in dump and "android-test-password" not in dump
    good("auth.logout")
    assert not api._APP.keys and good("auth.state")["user"] is None
    assert not call("health.get_profile")["ok"]
    login("member")
    assert not good("provider.status")["has_api_key"]
    assert not call("auth.login", username="member", password="wrong")["ok"]


@pytest.mark.parametrize(
    "request_data",
    [
        None,
        "x",
        "[]",
        "{}",
        '{"id":true,"action":"auth.state"}',
        '{"id":{},"action":"auth.state"}',
        '{"id":1,"action":"auth.state","params":[]}',
        '{"id":1,"action":"auth.state","extra":1}',
        '{"id":1,"action":"auth.state","params":{"x":NaN}}',
    ],
)
def test_malformed_requests_are_safe(api, request_data):
    result = json.loads(api.dispatch(request_data))
    assert not result["ok"] and result["error"]
    assert "Traceback" not in result["error"]


@pytest.mark.parametrize(
    "action,params",
    [
        ("health.get_profile", {"user_id": 1}),
        ("management.list_members", {"actor_user_id": 1}),
        ("ai.update_ai_config", {"operator_user_id": 1}),
        ("files.upload_file", {"source": "C:/secret"}),
        ("files.export_file", {"file_id": 1, "destination": "C:/secret"}),
        ("backup_export", {"path": "C:/secret"}),
        ("auth.get_user", {"user_id": 1}),
        ("database.connect", {}),
        ("__dict__", {}),
        (
            "health.add_life_record",
            {
                "category": "water",
                "occurred_at": "2026-09-05T08:00",
                "content": "饮水",
                "source": "ai_confirmed",
            },
        ),
    ],
)
def test_no_actor_spoofing_path_access_or_unlisted_method(api, action, params):
    member()
    assert not call(action, **params)["ok"]


def test_beijing_record_reminder_profile_and_account_isolation(api):
    member()
    rid = good(
        "health.add_life_record",
        category="water",
        occurred_at="2026-09-05T00:20",
        content="饮水",
        details={"amount_ml": 200},
    )
    records = good("health.list_life_records")
    assert records[0]["id"] == rid
    assert records[0]["occurred_at"] == "2026-09-05T00:20:00+08:00"
    assert records[0]["local_date"] == "2026-09-05"
    with api._APP.database.connect() as connection:
        assert (
            connection.execute("SELECT occurred_at FROM life_records")
            .fetchone()[0]
            .startswith("2026-09-04T16:20")
        )
    good(
        "health.save_profile",
        values={"display_name": "张三", "birth_date": "1990-01-01", "height_cm": 170},
    )
    assert good("health.get_profile")["display_name"] == "张三"
    reminder = good(
        "health.add_reminder", title="饮水", scheduled_at="2026-09-06T08:30", reminder_type="water"
    )
    reminder_id = reminder["id"] if isinstance(reminder, dict) else reminder
    good("health.update_reminder", reminder_id=reminder_id, scheduled_at="2026-09-06T09:45")
    assert good("health.list_reminders")[0]["scheduled_at"] == "2026-09-06T09:45:00+08:00"
    good("health.pause_reminder", reminder_id=reminder_id)
    good("health.resume_reminder", reminder_id=reminder_id)
    member("other")
    assert good("health.list_life_records") == []
    assert not call("health.delete_life_record", record_id=rid)["ok"]
    assert not call("health.update_reminder", reminder_id=reminder_id, title="attack")["ok"]


def test_management_advisor_and_commerce_workflow(api):
    mem = member()
    op = operator()
    advisor = good(
        "management.create_advisor",
        username="advisor",
        password="android-test-password",
        display_name="顾问",
    )
    advisor_id = advisor["id"] if "id" in advisor else advisor["user_id"]
    good("management.bind_advisor", member_user_id=mem["id"], advisor_user_id=advisor_id)
    good(
        "management.save_membership",
        member_user_id=mem["id"],
        plan_code="local",
        starts_at="2026-09-01T00:00",
        ends_at="2027-09-01T00:00",
    )
    product = good(
        "commerce.create_product",
        sku="WATER-01",
        name="水杯",
        category="日常生活",
        price_cents=1200,
        is_active=True,
    )
    product_id = product["id"] if isinstance(product, dict) else product
    task = good(
        "management.create_visit_task",
        member_user_id=mem["id"],
        title="电话回访",
        scheduled_at="2026-09-05T10:00",
    )
    task_id = task["id"] if isinstance(task, dict) else task
    login("advisor")
    assert len(good("management.list_members")) == 1
    good("management.start_visit_task", task_id=task_id)
    good(
        "management.complete_visit_task",
        task_id=task_id,
        summary="已了解近期生活记录",
        visited_at="2026-09-05T10:30",
        next_visit_at="2026-09-10T10:00",
    )
    rec = good(
        "commerce.recommend_product",
        member_user_id=mem["id"],
        product_id=product_id,
        reason="用户主动关注日常饮水",
    )
    rec_id = rec["id"] if isinstance(rec, dict) else rec
    login("member")
    assert good("health.get_service_summary")
    good("commerce.mark_interested", recommendation_id=rec_id)
    order = good("commerce.simulate_purchase", product_id=product_id, recommendation_id=rec_id)
    order_id = order["id"] if isinstance(order, dict) else order
    assert not call("commerce.set_product_active", product_id=product_id, is_active=False)["ok"]
    login("operator")
    good("commerce.mark_order_delivered", order_id=order_id)
    assert good("management.get_advisor_work_statistics")
    assert not call("management.set_account_enabled", target_user_id=op["id"], enabled=False)["ok"]


def test_disabled_account_session_revoked(api):
    mem = member()
    operator()
    good("management.set_account_enabled", target_user_id=mem["id"], enabled=False)
    assert not call("auth.login", username="member", password="android-test-password")["ok"]
    login("operator")
    api._APP.user_id = mem["id"]
    assert not call("health.get_profile")["ok"]
    assert api._APP.user_id is None


def test_ai_chat_success_failure_and_restart_recovery(api, monkeypatch):
    from ollama_chat_app.providers.base import ProviderError

    user = member()
    good("provider.configure", provider="deepseek", api_key="sk-dummy")
    seen = []

    def reply(self, model, messages):
        seen.append((model, messages))
        return "您好，这是测试回答"

    monkeypatch.setattr(mobile_providers.MobileProvider, "chat", reply)
    answer = good("chat.send", content="你好")
    assert answer["status"] == "complete" and answer["role"] == "assistant"
    assert seen[0][0] == "deepseek-v4-flash"
    assert seen[0][1][-1]["content"] == "你好"

    def fail(*args):
        raise ProviderError("测试断网")

    monkeypatch.setattr(mobile_providers.MobileProvider, "chat", fail)
    assert not call("chat.send", content="第二条")["ok"]
    assert good("chat.list")[-1]["status"] == "error"
    api._APP.chat.begin_message(user["id"], "模拟中断")
    directory = str(api._APP.data_dir)
    assert json.loads(api.initialize(directory))["ok"]
    login("member")
    assert good("chat.list")[-1]["status"] == "error"
    assert not api._APP.keys


def test_ai_draft_requires_confirmation_and_permissions(api, monkeypatch):
    member()
    good("provider.configure", provider="deepseek", api_key="sk-dummy")
    payload = {
        "answer": "已整理饮水记录，请核对。",
        "proposals": [
            {
                "type": "life_record",
                "category": "water",
                "occurred_at": "2026-09-05T09:00+08:00",
                "content": "喝水200毫升",
                "details": {"amount_ml": 200},
            }
        ],
    }
    monkeypatch.setattr(
        mobile_providers.MobileProvider,
        "chat",
        lambda *args: json.dumps(payload, ensure_ascii=False),
    )
    draft = good("ai.create_member_draft", prompt="记录饮水200毫升")
    assert good("health.list_life_records") == []
    proposal_id = draft["proposals"][0]["id"]
    good("ai.confirm_proposal", insight_id=draft["id"], proposal_id=proposal_id)
    assert len(good("health.list_life_records")) == 1
    member("other")
    assert not call("ai.get_draft", insight_id=draft["id"])["ok"]


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII="
)


def test_attachment_report_and_portable_backup_roundtrip(api, tmp_path):
    member()
    source = tmp_path / "sample.png"
    source.write_bytes(PNG)
    uploaded = json.loads(api.import_attachment(str(source), "原始报告.png", "项目A 12 g/L 10-20"))
    assert uploaded["ok"]
    file_id = uploaded["result"]["id"]
    extracted = good("files.extract_text", file_id=file_id)
    assert not extracted["confirmed"] and "项目A" in extracted["text"]
    report_id = good(
        "files.create_report_draft", file_id=file_id, report_type="化验单", report_date="2026-09-05"
    )
    item_id = good(
        "files.add_report_item_draft",
        report_id=report_id,
        item_name="项目A",
        result_value="12",
        unit="g/L",
        reference_range="10-20",
        flag="unknown",
    )
    good("files.confirm_report_item", report_id=report_id, item_id=item_id)
    good("files.confirm_report", report_id=report_id)
    destination = tmp_path / "export.png"
    exported = json.loads(api.attachment_export(file_id, str(destination)))
    assert exported["ok"] and exported["result"]["media_type"] == "image/png"
    assert destination.read_bytes() == PNG
    backup = tmp_path / "data.zip"
    assert json.loads(api.backup_export(str(backup)))["ok"]
    assert json.loads(api.initialize(str(tmp_path / "new-phone")))["ok"]
    imported = json.loads(api.backup_import(str(backup)))
    assert imported["ok"] and imported["result"]["state"]["user"] is None
    assert Path(imported["result"]["safety_backup_path"]).exists()
    login("member")
    assert good("files.list_reports")[0]["status"] == "confirmed"
    assert good("files.list_files")[0]["original_name"] == "原始报告.png"
    assert good("system.check")["integrity"] == ["ok"]
    member("other")
    assert not json.loads(api.attachment_export(file_id, str(tmp_path / "unauthorized.png")))["ok"]
    assert not json.loads(api.backup_export(str(tmp_path / "not-allowed.zip")))["ok"]


def test_backup_permissions_and_failed_import_preserves_database(api, tmp_path):
    member()
    assert good("auth.state")["can_backup"]
    operator()
    login("member")
    assert not good("auth.state")["can_backup"]
    login("operator")
    assert good("auth.state")["can_backup"]
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert not json.loads(api.backup_import(str(bad)))["ok"]
    assert good("auth.state")["user"]["username"] == "operator"
    assert len(good("management.list_members")) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://192.168.1.2:11434",
        "http://10.0.2.2:11434",
        "https://172.16.0.1:11434",
        "http://[::1]:11434",
        "http://[fd00::1]:11434",
    ],
)
def test_private_local_urls_accepted(url):
    assert mobile_providers.validate_local_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://api.deepseek.com",
        "file:///data/app.db",
        "http://8.8.8.8",
        "http://169.254.169.254",
        "http://0.0.0.0",
        "http://224.0.0.1",
        "http://192.168.0.2/private",
        "http://key@192.168.0.2",
        "http://192.168.0.2?token=x",
        "http://192.168.0.2#fragment",
        "http://[::ffff:8.8.8.8]",
        "http://172.32.0.1",
    ],
)
def test_public_or_dangerous_local_urls_rejected(url):
    with pytest.raises(mobile_providers.ProviderError):
        mobile_providers.validate_local_url(url)


@pytest.mark.parametrize("kind", ["deepseek", "ollama_cloud", "local"])
def test_provider_http_payload_and_secret_destination(kind):
    captured = []

    def handler(request):
        captured.append(request)
        data = (
            {"choices": [{"message": {"content": "测试回复"}}]}
            if kind == "deepseek"
            else {"message": {"content": "测试回复"}}
        )
        return httpx.Response(200, json=data)

    provider = mobile_providers.MobileProvider(
        kind, "sk-unit-test", "http://192.168.1.10:11434", transport=httpx.MockTransport(handler)
    )
    model = "deepseek-v4-flash" if kind == "deepseek" else "test-model"
    assert provider.chat(model, [{"role": "user", "content": "你好"}]) == "测试回复"
    request = captured[0]
    if kind == "local":
        assert "authorization" not in request.headers and request.url.host == "192.168.1.10"
    else:
        assert request.headers["authorization"] == "Bearer sk-unit-test"
        assert request.url.scheme == "https"
    payload = json.loads(request.content)
    assert payload["stream"] is False
    if kind == "deepseek":
        assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("status", [301, 302, 400, 401, 402, 403, 404, 429, 500, 503])
def test_provider_status_sanitizes_secrets_and_never_follows_redirect(status):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://evil.example/steal"},
            text="sk-secret-echoed-in-server-error",
        )

    provider = mobile_providers.MobileProvider(
        "deepseek", "sk-secret", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(mobile_providers.ProviderError) as error:
        provider.chat("deepseek-v4-flash", [{"role": "user", "content": "test"}])
    assert "secret" not in str(error.value) and len(captured) == 1


def test_models_select_first_and_do_not_claim_free(api, monkeypatch):
    member()
    good("provider.configure", provider="ollama_cloud", api_key="sk-test")
    monkeypatch.setattr(
        mobile_providers.MobileProvider, "list_models", lambda _: ["model-a", "model-b"]
    )
    result = good("provider.models")
    assert result["selected"] == "model-a" and "不保证永久免费" in result["notice"]


def test_provider_no_key_relay_between_modes(api):
    member()
    good("provider.configure", provider="deepseek", api_key="sk-deep")
    good("provider.configure", provider="ollama_cloud")
    assert not good("provider.status")["has_api_key"]
    good("provider.configure", provider="local")
    assert not api._APP._provider()._api_key
    good("provider.configure", provider="deepseek")
    assert good("provider.status")["has_api_key"]


def test_native_ocr_errors_are_safe_off_android(api, tmp_path):
    member()
    source = tmp_path / "image.png"
    source.write_bytes(PNG)
    file_id = json.loads(api.import_attachment(str(source), "image.png"))["result"]["id"]
    assert not call("files.extract_text", file_id=file_id)["ok"]
