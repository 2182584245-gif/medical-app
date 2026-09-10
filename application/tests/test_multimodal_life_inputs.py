from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from ollama_chat_app.providers.demo_life import DEMO_MODEL, DemoLifeProvider
from ollama_chat_app.services.agent_tools import SnapshotTools
from ollama_chat_app.services.device_observations import observation_to_proposal
from ollama_chat_app.services.life_agent import ConversationAgentProvider
from ollama_chat_app.time_utils import beijing_now


def observation(kind="sleep", source="self_report"):
    end = beijing_now() - timedelta(hours=1)
    return {
        "schema_version": 1,
        "kind": kind,
        "source": source,
        "start_at": (end - timedelta(hours=8)).isoformat(),
        "end_at": end.isoformat(),
    }


def test_self_report_and_wearable_are_distinguished():
    manual = observation_to_proposal(observation())
    wearable = observation_to_proposal(observation(source="wearable"))
    assert manual["details"]["duration_hours"] == 8
    assert manual["details"]["input_source"] == "self_report"
    assert wearable["details"]["input_source"] == "wearable"
    assert "用户自报" in manual["content"]


def test_phone_inactivity_never_becomes_sleep_duration():
    proposal = observation_to_proposal(observation("screen_inactivity", "screen_time"))
    assert proposal["type"] == "insight"
    assert "不代表已经睡着" in proposal["content"]
    assert "details" not in proposal
    with pytest.raises(ValueError, match="不能转换为睡眠"):
        observation_to_proposal(observation("sleep", "screen_time"))


@pytest.mark.parametrize(
    "patch",
    [
        {"user_id": 2},
        {"schema_version": True},
        {"source": "unknown"},
        {"values": {"sleep_stage": "deep"}},
        {"start_at": "2026-01-01T22:00:00"},
        {"end_at": "2020-01-01T00:00:00+08:00"},
    ],
)
def test_device_ingress_rejects_invalid_or_unscoped_payloads(patch):
    with pytest.raises(ValueError):
        observation_to_proposal({**observation(), **patch})


def test_tool_cannot_claim_it_received_wearable_data():
    tools = SnapshotTools({}, input_source="voice")
    proposal = observation_to_proposal(observation(source="wearable"))
    tools.execute("propose_action", json.dumps({"proposal": proposal}))
    assert tools.proposals[0]["details"]["input_source"] == "voice"


def test_walking_without_duration_and_sleep_form_work_through_agent():
    from ollama_chat_app.services.ai_assistant import AiAssistantService

    now = beijing_now()
    prompts = [
        "我现在出去散步了",
        (
            "我自报的作息：入睡 "
            + (now - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
            + "，起床 "
            + (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
            + "（北京时间）。请整理为待确认的作息记录。"
        ),
    ]
    outputs = []
    for prompt in prompts:
        agent = ConversationAgentProvider(
            DemoLifeProvider(), [], {"role": "user", "content": prompt}, threading.Event()
        )
        result = agent.chat(
            DEMO_MODEL,
            [
                {"role": "system", "content": AiAssistantService._member_system_prompt({})},
                {"role": "user", "content": prompt},
            ],
        )
        outputs.append(json.loads(result)["proposals"][0])
    assert outputs[0]["category"] == "activity"
    assert "duration_minutes" not in outputs[0]["details"]
    assert outputs[1]["category"] == "sleep"
    assert outputs[1]["details"]["duration_hours"] == 8


def test_meal_capture_keeps_photo_and_prefills_task_without_camera(qtbot, monkeypatch, tmp_path):
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QDialog

    from ollama_chat_app.data.database import Database
    from ollama_chat_app.security.secret_store import SecretStore
    from ollama_chat_app.services.auth import AuthService
    from ollama_chat_app.services.chat import ChatService
    from ollama_chat_app.ui.chat_page import ChatPage

    photo = tmp_path / "synthetic.png"
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    image.save(str(photo))

    class Capture:
        file_path = str(photo)
        image_bytes = None

        def __init__(self, parent):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

    database = Database(tmp_path / "photo.db")
    member = AuthService(database).register("member", "synthetic-password-123")
    page = ChatPage(ChatService(database), SecretStore())
    qtbot.addWidget(page)
    page.start_session(member)
    monkeypatch.setattr("ollama_chat_app.ui.chat_page.MealCaptureDialog", Capture)
    page._capture_meal()
    assert page._pending_attachments[0].is_image
    assert "看得见的食物" in page.message_input.toPlainText()
    assert "不要猜重量" in page.message_input.toPlainText()
    assert (
        page.chat_service.list_messages(member.id, conversation_id=page.current_conversation_id)
        == []
    )
