from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ollama_chat_app.data.database import Database, timestamp_to_db, utc_now
from ollama_chat_app.providers.base import (
    ChatMessage,
    ChatProvider,
    ProviderCapabilities,
    ProviderError,
)
from ollama_chat_app.services.ai_assistant import (
    AiAssistantService,
    AiDraftNotFoundError,
    AiPermissionError,
    AiValidationError,
    UnsafeMedicalRequestError,
)
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService


class FakeProvider(ChatProvider):
    def __init__(self, response: str, *, images: bool = False) -> None:
        self.response = response
        self._images = images
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_images=self._images)

    def chat(self, model: str, messages: list[ChatMessage]) -> str:
        self.calls.append((model, [dict(message) for message in messages]))
        return self.response


@pytest.fixture
def ai_fixture(tmp_path: Path):
    database = Database(tmp_path / "app.db")
    auth = AuthService(database)
    health = HealthService(database)
    ai = AiAssistantService(database, health)
    member = auth.register("member", "member-password-123")
    outsider = auth.register("outsider", "outsider-password-123")
    return database, auth, health, ai, member, outsider


def _response(*proposals: dict[str, object], answer: str = "这是一般性生活建议。") -> str:
    return json.dumps(
        {"answer": answer, "proposals": list(proposals)}, ensure_ascii=False
    )


def test_context_uses_only_confirmed_recent_member_data(ai_fixture) -> None:
    database, _auth, health, ai, member, outsider = ai_fixture
    now = utc_now()
    health.add_life_record(member.id, "water", now, "喝水 300 毫升")
    health.add_life_record(
        member.id, "activity", now - timedelta(days=30), "很久以前散步"
    )
    active_reminder = health.add_reminder(
        member.id, "今晚散步", now + timedelta(hours=3), "activity"
    )
    disabled_reminder = health.add_reminder(
        member.id, "不再提示", now + timedelta(hours=4), "custom"
    )
    health.toggle_reminder(member.id, disabled_reminder, enabled=False)
    with database.transaction() as connection:
        stamp = timestamp_to_db(now)
        connection.execute(
            """
            INSERT INTO profile_facts (
                user_id, fact_key, value_json, source, status,
                effective_at, created_at, updated_at
            ) VALUES (?, 'dietary_preferences', '"清淡"', 'manual', 'confirmed', ?, ?, ?)
            """,
            (member.id, stamp, stamp, stamp),
        )
        connection.execute(
            """
            INSERT INTO profile_facts (
                user_id, fact_key, value_json, source, status,
                effective_at, created_at, updated_at
            ) VALUES (?, 'health_goals', '"未确认目标"', 'ai', 'draft', ?, ?, ?)
            """,
            (member.id, stamp, stamp, stamp),
        )

    context = ai.build_member_context(member.id, context_days=14)

    assert context["confirmed_facts"] == {"dietary_preferences": "清淡"}
    assert [item["content"] for item in context["recent_life_records"]] == [
        "喝水 300 毫升"
    ]
    assert [item["id"] for item in context["active_reminders"]] == [active_reminder]
    with pytest.raises(AiPermissionError):
        ai.build_member_context(outsider.id, member.id)


def test_draft_never_changes_facts_and_each_proposal_needs_confirmation(ai_fixture) -> None:
    database, _auth, health, ai, member, _outsider = ai_fixture
    occurred = datetime.now(UTC).isoformat()
    scheduled = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    provider = FakeProvider(
        "```json\n"
        + _response(
            {
                "type": "life_record",
                "category": "water",
                "occurred_at": occurred,
                "content": "喝水 250 毫升",
                "details": {"amount_ml": 250},
            },
            {
                "type": "profile_fact",
                "fact_key": "dietary_preferences",
                "value": "偏好清淡饮食",
            },
            {
                "type": "reminder",
                "title": "晚饭后散步",
                "reminder_type": "activity",
                "scheduled_at": scheduled,
            },
        )
        + "\n```"
    )

    draft = ai.create_member_draft(
        member.id, provider, "测试服务", "test-model", "记录我刚才喝的水"
    )
    assert len(draft["proposals"]) == 3
    assert health.list_life_records(member.id) == []
    assert health.list_reminders(member.id) == []
    assert health.get_profile(member.id)["dietary_preferences"] is None
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM profile_facts WHERE user_id = ?", (member.id,)
        ).fetchone()[0] == 0

    draft = ai.confirm_proposal(member.id, draft["id"], 1)
    assert health.list_life_records(member.id)[0]["source"] == "ai_confirmed"
    assert health.list_reminders(member.id) == []
    assert draft["proposals"][0]["status"] == "accepted"
    assert draft["proposals"][1]["status"] == "pending"
    with pytest.raises(AiDraftNotFoundError):
        ai.confirm_proposal(member.id, draft["id"], 1)

    ai.confirm_proposal(member.id, draft["id"], 2)
    final = ai.dismiss_proposal(member.id, draft["id"], 3)
    assert final["status"] == "accepted"
    assert health.get_profile(member.id)["dietary_preferences"] == "偏好清淡饮食"
    with database.connect() as connection:
        fact = connection.execute(
            "SELECT source, status FROM profile_facts WHERE user_id = ?",
            (member.id,),
        ).fetchone()
        assert tuple(fact) == ("ai_confirmed", "confirmed")
        assert connection.execute(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (member.id,)
        ).fetchone()[0] == 0


def test_malformed_or_medically_unsafe_output_is_never_persisted(ai_fixture) -> None:
    database, _auth, _health, ai, member, _outsider = ai_fixture
    with pytest.raises(UnsafeMedicalRequestError):
        ai.create_member_draft(
            member.id,
            FakeProvider(_response()),
            "测试服务",
            "test-model",
            "请帮我诊断这是什么病",
        )
    with pytest.raises(AiValidationError, match="不是有效 JSON"):
        ai.create_member_draft(
            member.id,
            FakeProvider("这不是 JSON"),
            "测试服务",
            "test-model",
            "今天怎样安排散步？",
        )
    with pytest.raises(UnsafeMedicalRequestError):
        ai.create_member_draft(
            member.id,
            FakeProvider(_response(answer="你确诊为某疾病，不用去医院。")),
            "测试服务",
            "test-model",
            "我今天有点累",
        )
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_insights").fetchone()[0] == 0


def test_image_capability_is_checked_without_calling_unsupported_provider(ai_fixture) -> None:
    _database, _auth, _health, ai, member, _outsider = ai_fixture
    text_only = FakeProvider(_response())
    with pytest.raises(ProviderError) as error:
        ai.create_member_draft(
            member.id,
            text_only,
            "DeepSeek",
            "deepseek-v4-flash",
            "请描述这张饮食图片",
            images=["C:/example/meal.jpg"],
        )
    assert error.value.code == "images_not_supported"
    assert text_only.calls == []

    visual = FakeProvider(_response(), images=True)
    ai.create_member_draft(
        member.id,
        visual,
        "Ollama",
        "qwen-vl",
        "请描述这张饮食图片",
        images=["C:/example/meal.jpg"],
    )
    assert visual.calls[0][1][-1]["images"] == ["C:/example/meal.jpg"]

    blob_visual = FakeProvider(_response(), images=True)
    ai.create_member_draft(
        member.id,
        blob_visual,
        "Ollama",
        "qwen-vl",
        "请描述这张饮食图片",
        images=[b"small-image-bytes"],
    )
    assert blob_visual.calls[0][1][-1]["images"] == [b"small-image-bytes"]


def test_only_active_bound_advisor_can_create_and_confirm_summary(ai_fixture) -> None:
    database, auth, _health, ai, member, _outsider = ai_fixture
    management = ServiceManagementService(database)
    operator = auth.bootstrap_operator("operator", "operator-password-123")
    advisor = management.create_advisor(
        operator.id,
        "advisor",
        "advisor-password-123",
        display_name="王顾问",
    )
    replacement = management.create_advisor(
        operator.id,
        "replacement",
        "replacement-password-123",
        display_name="李顾问",
    )
    management.bind_advisor(operator.id, member.id, advisor.id)
    today = datetime.now().astimezone().date()
    provider = FakeProvider(
        _response(
            {
                "type": "advisor_summary",
                "period_start": (today - timedelta(days=13)).isoformat(),
                "period_end": today.isoformat(),
                "content": "近期饮水记录暂无；下次继续了解会员需求。",
            },
            answer="已形成一份待顾问确认的摘要。",
        )
    )

    draft = ai.create_advisor_summary_draft(
        advisor.id, member.id, provider, "测试服务", "test-model"
    )
    with pytest.raises(AiDraftNotFoundError):
        ai.confirm_proposal(member.id, draft["id"], 1)
    confirmed = ai.confirm_proposal(advisor.id, draft["id"], 1)
    assert confirmed["status"] == "accepted"
    with database.connect() as connection:
        summary = connection.execute(
            "SELECT source FROM advisor_summaries WHERE member_user_id = ?",
            (member.id,),
        ).fetchone()
    assert summary["source"] == "ai_confirmed"

    second_draft = ai.create_advisor_summary_draft(
        advisor.id, member.id, provider, "测试服务", "test-model"
    )
    management.bind_advisor(operator.id, member.id, replacement.id)
    with pytest.raises(AiDraftNotFoundError):
        ai.get_draft(advisor.id, second_draft["id"])
    with pytest.raises(AiPermissionError):
        ai.build_member_context(advisor.id, member.id)
    assert ai.build_member_context(replacement.id, member.id)["actor_role"] == "advisor"
