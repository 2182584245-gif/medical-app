from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.base import ChatMessage, ChatProvider
from ollama_chat_app.services.ai_assistant import (
    AI_CONFIG_SETTING_KEY,
    DEFAULT_AI_CONFIG,
    AiAssistantService,
    AiFeatureDisabledError,
    AiPermissionError,
    AiValidationError,
)
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService
from ollama_chat_app.ui.operator_ai_config_panel import OperatorAiConfigPanel
from ollama_chat_app.ui.operator_workspace import OperatorWorkspace


class RecordingProvider(ChatProvider):
    def __init__(self, response: str | None = None) -> None:
        self.response = response or json.dumps(
            {"answer": "基于现有记录，可以继续保持规律生活。", "proposals": []},
            ensure_ascii=False,
        )
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    def chat(self, model: str, messages: Sequence[ChatMessage]) -> str:
        self.calls.append((model, [dict(message) for message in messages]))
        return self.response


@pytest.fixture
def config_fixture(tmp_path: Path):
    database = Database(tmp_path / "app.db")
    auth = AuthService(database)
    health = HealthService(database)
    ai = AiAssistantService(database, health)
    operator = auth.bootstrap_operator("operator-ai", "operator-password-123")
    member = auth.register("member-ai", "member-password-123")
    return database, auth, health, ai, operator, member


def test_default_config_is_non_secret_and_only_operator_can_update(config_fixture) -> None:
    database, _auth, _health, ai, operator, member = config_fixture
    assert ai.get_ai_config(member.id) == DEFAULT_AI_CONFIG

    with pytest.raises(AiPermissionError, match="只有运营员"):
        ai.update_ai_config(member.id, {"enabled": False})
    with pytest.raises(AiValidationError, match="不支持的字段"):
        ai.update_ai_config(operator.id, {"api_key": "must-never-be-stored"})

    saved = ai.update_ai_config(
        operator.id,
        {
            "enabled": True,
            "member_assistant_enabled": False,
            "advisor_summary_enabled": True,
            "context_days": 21,
        },
    )
    assert saved == {
        "enabled": True,
        "member_assistant_enabled": False,
        "advisor_summary_enabled": True,
        "context_days": 21,
    }
    assert ai.get_ai_config(member.id) == saved
    with database.connect() as connection:
        settings = connection.execute(
            """
            SELECT user_id, setting_key, value_json FROM app_settings
            WHERE setting_key = ?
            """,
            (AI_CONFIG_SETTING_KEY,),
        ).fetchall()
        audit = connection.execute(
            "SELECT action FROM audit_logs WHERE action = 'structured_ai.config_updated'"
        ).fetchone()
    assert len(settings) == 1
    assert settings[0]["user_id"] is None
    assert set(json.loads(settings[0]["value_json"])) == set(DEFAULT_AI_CONFIG)
    assert "api" not in settings[0]["value_json"].casefold()
    assert audit is not None


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"enabled": "yes"}, "enabled 必须"),
        ({"context_days": 0}, "1 到 90"),
        ({"context_days": True}, "1 到 90"),
        ({"member_assistant_enabled": 1}, "member_assistant_enabled 必须"),
    ],
)
def test_operator_config_validation(config_fixture, values, match) -> None:
    _database, _auth, _health, ai, operator, _member = config_fixture
    with pytest.raises(AiValidationError, match=match):
        ai.update_ai_config(operator.id, values)


def test_disabled_member_feature_stops_before_provider_or_draft(config_fixture) -> None:
    database, _auth, _health, ai, operator, member = config_fixture
    ai.update_ai_config(operator.id, {"enabled": False})
    provider = RecordingProvider()

    with pytest.raises(AiFeatureDisabledError, match="未向任何 AI 服务发送数据"):
        ai.create_member_draft(member.id, provider, "测试 AI", "test-model", "今天如何安排散步？")
    assert provider.calls == []
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_insights").fetchone()[0] == 0

    ai.update_ai_config(operator.id, {"enabled": True, "member_assistant_enabled": False})
    with pytest.raises(AiFeatureDisabledError):
        ai.create_member_draft(member.id, provider, "测试 AI", "test-model", "今天如何安排散步？")
    assert provider.calls == []


def test_generation_uses_operator_context_limit(config_fixture) -> None:
    _database, _auth, health, ai, operator, member = config_fixture
    now = datetime.now(UTC)
    health.add_life_record(member.id, "water", now, "今天喝水 300 毫升")
    health.add_life_record(member.id, "activity", now - timedelta(days=10), "十天前散步")
    ai.update_ai_config(operator.id, {"context_days": 3})
    provider = RecordingProvider()

    ai.create_member_draft(
        member.id,
        provider,
        "测试 AI",
        "test-model",
        "请根据已有事实给一般生活提示",
        context_days=30,
    )

    system_prompt = str(provider.calls[0][1][0]["content"])
    assert '"context_days":3' in system_prompt
    assert "今天喝水 300 毫升" in system_prompt
    assert "十天前散步" not in system_prompt


def test_advisor_summary_obeys_its_own_switch(config_fixture) -> None:
    database, auth, _health, ai, operator, member = config_fixture
    management = ServiceManagementService(database)
    advisor = management.create_advisor(
        operator.id,
        "advisor-ai-config",
        "advisor-password-123",
        display_name="配置测试顾问",
    )
    management.bind_advisor(operator.id, member.id, advisor.id)
    ai.update_ai_config(operator.id, {"advisor_summary_enabled": False})
    provider = RecordingProvider()

    with pytest.raises(AiFeatureDisabledError, match="顾问 AI 摘要"):
        ai.create_advisor_summary_draft(advisor.id, member.id, provider, "测试 AI", "test-model")
    assert provider.calls == []


def test_operator_ai_config_panel_reads_saves_and_never_has_a_key_field(
    config_fixture, qtbot
) -> None:
    _database, _auth, _health, ai, operator, member = config_fixture
    panel = OperatorAiConfigPanel(ai)
    qtbot.addWidget(panel)
    panel.set_operator(operator.id)

    assert panel.enabled_checkbox.isChecked()
    assert panel.context_days_input.value() == 14
    assert "API Key" in panel.security_notice.text()
    assert not any("key" in child.objectName().casefold() for child in panel.findChildren(QWidget))

    panel.member_checkbox.setChecked(False)
    panel.context_days_input.setValue(28)
    with qtbot.waitSignal(panel.config_saved) as signal:
        panel.save_button.click()
    assert signal.args[0]["member_assistant_enabled"] is False
    assert signal.args[0]["context_days"] == 28
    assert ai.get_ai_config()["context_days"] == 28
    assert "已保存在本地数据库" in panel.status_label.text()

    panel.enabled_checkbox.setChecked(False)
    assert not panel.context_days_input.isEnabled()
    assert not panel.member_checkbox.isEnabled()

    panel.set_operator(member.id)
    panel.enabled_checkbox.setChecked(True)
    panel.save()
    assert "只有运营员" in panel.status_label.text()


def test_operator_workspace_omits_ai_config_ui_as_requested(config_fixture, qtbot) -> None:
    database, _auth, _health, ai, operator, _member = config_fixture
    workspace = OperatorWorkspace(
        ServiceManagementService(database),
        ai_assistant_service=ai,
    )
    qtbot.addWidget(workspace)

    workspace.start_session(operator)

    # The redesigned operator workspace intentionally removes AI configuration.
    # Independent component and service authorization tests above remain intact.
    assert workspace.ai_config_panel is None
    assert all("AI" not in workspace.tabs.tabText(index) for index in range(workspace.tabs.count()))

    workspace.end_session()
    assert workspace.ai_config_panel is None
