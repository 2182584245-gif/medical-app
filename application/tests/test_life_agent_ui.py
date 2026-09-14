from __future__ import annotations

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.ui.chat_page import ChatPage


def test_normal_send_without_key_creates_reviewable_action(qtbot, tmp_path, monkeypatch):
    database = Database(tmp_path / "ui-agent.db")
    member = AuthService(database).register("member", "synthetic-password-123")
    ai = AiAssistantService(database)
    page = ChatPage(ChatService(database), SecretStore(), ai_assistant_service=ai)
    qtbot.addWidget(page)
    page.start_session(member)
    page.show()
    page.demo_checkbox.setChecked(True)
    page.context_checkbox.setChecked(True)

    def forbidden(*args, **kwargs):
        raise AssertionError("No key dialog or external request is allowed in demo")

    monkeypatch.setattr(page, "_open_key_dialog", forbidden)
    import httpx

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    page.message_input.setPlainText("刚才喝了300毫升水")
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running, timeout=10000)
    drafts = ai.list_drafts(member.id)
    assert len(drafts) == 1
    assert drafts[0]["proposals"][0]["type"] == "life_record"
    assert "待确认" in page.status_label.text()
    assert page.message_input.toPlainText() == ""
    assert HealthService(database).list_life_records(member.id) == []
    ai.confirm_proposal(member.id, drafts[0]["id"], 1)
    assert len(HealthService(database).list_life_records(member.id)) == 1
    page.message_input.setPlainText("总结最近记录")
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running, timeout=10000)
    messages = page.chat_service.list_messages(
        member.id, conversation_id=page.current_conversation_id
    )
    assert "300毫升" in messages[-1].content
    assert "离线流程演示" in messages[-1].content


def test_disabled_agent_stops_even_small_talk_before_provider(qtbot, tmp_path, monkeypatch):
    database = Database(tmp_path / "disabled-agent.db")
    auth = AuthService(database)
    member = auth.register("member", "synthetic-password-123")
    operator = auth.bootstrap_operator("operator", "synthetic-password-123")
    ai = AiAssistantService(database)
    ai.update_ai_config(operator.id, {"enabled": False})
    page = ChatPage(ChatService(database), SecretStore(), ai_assistant_service=ai)
    qtbot.addWidget(page)
    page.start_session(member)
    page.demo_checkbox.setChecked(True)
    page.message_input.setPlainText("你好")
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running, timeout=10000)
    assert "关闭" in page.status_label.text()
    assert page.message_input.toPlainText() == "你好"
    assert ai.list_drafts(member.id) == []
