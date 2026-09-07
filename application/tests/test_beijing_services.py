from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ollama_chat_app import time_utils
from ollama_chat_app.data.database import Database
from ollama_chat_app.services import ai_assistant, health
from ollama_chat_app.services.ai_assistant import (
    MEMBER_PROPOSAL_TYPES,
    AiAssistantService,
    AiValidationError,
)
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService, HealthValidationError
from ollama_chat_app.services.service_management import (
    ManagementValidationError,
    ServiceManagementService,
)
from ollama_chat_app.time_utils import BEIJING_TIMEZONE


@pytest.fixture
def beijing_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Beijing has entered January 2 while UTC is still on January 1.
    now = datetime(2030, 1, 2, 0, 30, tzinfo=BEIJING_TIMEZONE)
    monkeypatch.setattr(time_utils, "beijing_now", lambda: now)
    monkeypatch.setattr(ai_assistant, "beijing_now", lambda: now)
    monkeypatch.setattr(health, "utc_now", lambda: now.astimezone(UTC))
    monkeypatch.setattr(ai_assistant, "utc_now", lambda: now.astimezone(UTC))
    database = Database(tmp_path / "beijing-services.db")
    auth = AuthService(database)
    member = auth.register("beijing-member", "correct-horse-battery-staple")
    return database, auth, member, HealthService(database), AiAssistantService(database)


@pytest.mark.parametrize("value", [datetime(2030, 1, 2, 8, 15), "2030-01-02T08:15:00"])
def test_manual_records_and_reminders_default_to_beijing(beijing_services, value) -> None:
    database, _auth, member, service, _ai = beijing_services
    record_id = service.add_life_record(member.id, "water", value, "饮水 300 毫升")
    reminder_id = service.add_reminder(member.id, "饮水", value, "water")
    record = service.list_life_records(member.id)[0]
    assert record["occurred_at"] == datetime(2030, 1, 2, 0, 15, tzinfo=UTC)
    assert record["local_date"] == "2030-01-02"
    assert record["timezone_offset_minutes"] == 480
    assert service.list_reminders(member.id)[0]["scheduled_at"] == record["occurred_at"]
    edited = service.update_reminder(
        member.id, reminder_id, scheduled_at="2030-01-02T09:00:00"
    )
    assert edited["scheduled_at"] == datetime(2030, 1, 2, 1, tzinfo=UTC)
    with database.connect() as connection:
        assert connection.execute(
            "SELECT occurred_at FROM life_records WHERE id = ?", (record_id,)
        ).fetchone()[0] == "2030-01-02T00:15:00.000000Z"


def test_beijing_dashboard_filters_and_statistics_preserve_historical_offsets(
    beijing_services,
) -> None:
    database, _auth, member, service, _ai = beijing_services
    for stamp, content in (
        ("2030-01-01T15:59:59Z", "前一天"),
        ("2030-01-01T09:00:00-07:00", "当天零点"),
        ("2030-01-02T15:59:59Z", "当天结束前"),
        ("2030-01-02T16:00:00Z", "后一天"),
    ):
        service.add_life_record(member.id, "water", stamp, content)
    with database.connect() as connection:
        original = [tuple(row) for row in connection.execute(
            "SELECT occurred_at, local_date, timezone_offset_minutes FROM life_records ORDER BY id"
        )]

    selected = service.list_life_records(
        member.id, start_date="2030-01-02", end_date="2030-01-02"
    )
    assert [row["content"] for row in selected] == ["当天结束前", "当天零点"]
    assert selected[1]["local_date"] == "2030-01-01"
    assert selected[1]["timezone_offset_minutes"] == -420
    dashboard = service.get_dashboard(member.id)
    assert dashboard["local_date"] == "2030-01-02"
    assert dashboard["today_record_count"] == 2
    assert dashboard["today_records_by_category"]["water"] == 2
    statistics = service.get_record_statistics(
        member.id, as_of=datetime(2030, 1, 1, 16, 30, tzinfo=UTC)
    )
    assert statistics["end_date"] == "2030-01-02"
    assert statistics["total_records"] == 3
    assert statistics["by_date"]["2030-01-01"] == 1
    assert statistics["by_date"]["2030-01-02"] == 2
    assert service.get_record_statistics(member.id)["end_date"] == "2030-01-02"
    with database.connect() as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT occurred_at, local_date, timezone_offset_minutes FROM life_records ORDER BY id"
        )] == original


def test_beijing_calendar_validation_retains_date_limits(beijing_services) -> None:
    _database, _auth, member, service, ai = beijing_services
    service.save_profile(member.id, {"birth_date": "2030-01-02"})
    ai._validate_profile_proposal_value("birth_date", "2030-01-02")
    with pytest.raises(HealthValidationError, match="出生日期不能晚于今天"):
        service.save_profile(member.id, {"birth_date": "2030-01-03"})
    with pytest.raises(AiValidationError, match="出生日期不能晚于今天"):
        ai._validate_profile_proposal_value("birth_date", "2030-01-03")
    service.add_life_record(member.id, "sleep", "2030-01-03T15:59:59Z", "明天结束前")
    with pytest.raises(HealthValidationError, match="不能晚于明天"):
        service.add_life_record(member.id, "sleep", "2030-01-03T16:00:00Z", "北京后天")
    horizon = date(2030, 1, 2) + timedelta(days=366 * 20)
    service.add_reminder(member.id, "长期提醒", f"{horizon.isoformat()}T23:59:00")
    with pytest.raises(HealthValidationError, match="不能晚于 20 年后"):
        service.add_reminder(
            member.id, "过远提醒", f"{(horizon + timedelta(days=1)).isoformat()}T00:00:00"
        )
    with pytest.raises(HealthValidationError, match="精确到分钟"):
        service.add_life_record(member.id, "water", "2030-01-02T08:00:00+08:00:30", "无效偏移")
    with pytest.raises(HealthValidationError, match="时区偏移超出"):
        service.add_life_record(member.id, "water", "2030-01-02T08:00:00+15:00", "无效偏移")


def test_management_dates_default_to_beijing_through_membership_and_visit_workflow(
    beijing_services,
) -> None:
    database, auth, member, health_service, _ai = beijing_services
    service = ServiceManagementService(database)
    operator = auth.bootstrap_operator("beijing-operator", "operator-password-123")
    advisor = service.create_advisor(
        operator.id, "beijing-advisor", "advisor-password-123", display_name="王顾问"
    )
    membership_id = service.save_membership(
        operator.id, member.id, plan_code="年度服务",
        starts_at="2030-01-02T00:00:00", ends_at="2031-01-02T00:00:00",
    )
    binding_id = service.bind_advisor(
        operator.id, member.id, advisor.id, started_at=datetime(2030, 1, 2, 8)
    )
    task_id = service.create_visit_task(
        operator.id, member.id, title="上门服务", scheduled_at="2030-01-02T09:00:00"
    )
    service.complete_visit_task(
        advisor.id, task_id, summary="完成回访", visited_at=datetime(2030, 1, 2, 10),
        next_visit_at="2030-01-03T09:00:00",
    )
    with database.connect() as connection:
        assert tuple(connection.execute(
            "SELECT starts_at, ends_at FROM memberships WHERE id = ?", (membership_id,)
        ).fetchone()) == ("2030-01-01T16:00:00.000000Z", "2031-01-01T16:00:00.000000Z")
        assert connection.execute(
            "SELECT started_at FROM advisor_bindings WHERE id = ?", (binding_id,)
        ).fetchone()[0] == "2030-01-02T00:00:00.000000Z"
        assert connection.execute(
            "SELECT scheduled_at FROM visit_tasks WHERE id = ?", (task_id,)
        ).fetchone()[0] == "2030-01-02T01:00:00.000000Z"
        assert connection.execute(
            "SELECT visited_at FROM visit_records WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == "2030-01-02T02:00:00.000000Z"
    assert health_service.list_reminders(member.id)[0]["scheduled_at"] == datetime(
        2030, 1, 3, 1, tzinfo=UTC
    )
    # Compare actual instants when one field has an explicit foreign offset.
    with pytest.raises(ManagementValidationError, match="结束时间必须晚于开始时间"):
        service.save_membership(
            operator.id, member.id, plan_code="无效顺序",
            starts_at="2030-01-02T08:00:00", ends_at="2030-01-01T17:00:00-07:00",
        )


def test_ai_context_uses_beijing_calendar_window_and_timezone_instructions(
    beijing_services,
) -> None:
    _database, _auth, member, service, ai = beijing_services
    for stamp, content in (
        ("2029-12-31T15:59:59Z", "范围之前"),
        ("2029-12-31T16:00:00Z", "范围起点"),
        ("2030-01-01T16:00:00Z", "北京今天"),
        ("2030-01-02T16:00:00Z", "明天"),
    ):
        service.add_life_record(member.id, "water", stamp, content)
    context = ai.build_member_context(member.id, context_days=2)
    assert [row["content"] for row in context["recent_life_records"]] == [
        "北京今天", "范围起点"
    ]
    assert [row["local_date"] for row in context["recent_life_records"]] == [
        "2030-01-02", "2030-01-01"
    ]
    member_prompt = ai._member_system_prompt(context)
    advisor_prompt = ai._advisor_system_prompt(context, {})
    assert "当前北京时间：2030-01-02T00:30:00+08:00" in member_prompt
    assert "默认时区为北京时间（UTC+08:00）" in member_prompt
    assert "必须输出带时区" in member_prompt
    assert "当前北京时间：2030-01-02T00:30:00+08:00" in advisor_prompt
    assert "建议周期 2029-12-20 至 2030-01-02" in advisor_prompt
    # AI output remains strict even though human input gains a default zone.
    response = json.dumps({"answer": "已整理提醒建议", "proposals": [{
        "type": "reminder", "title": "饮水", "reminder_type": "water",
        "scheduled_at": "2030-01-02T08:00:00",
    }]})
    with pytest.raises(AiValidationError, match="必须包含时区"):
        ai._parse_response(response, allowed_types=MEMBER_PROPOSAL_TYPES)
