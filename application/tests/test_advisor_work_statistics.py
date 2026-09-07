from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.service_management import (
    PermissionDeniedError,
    ServiceManagementService,
)


@pytest.fixture
def work_statistics_fixture(
    tmp_path: Path,
) -> tuple[
    Database,
    AuthService,
    ServiceManagementService,
    object,
    object,
    object,
    object,
    object,
]:
    database = Database(tmp_path / "advisor-work.db")
    auth = AuthService(database)
    management = ServiceManagementService(database)
    operator = auth.bootstrap_operator("statistics-operator", "operator-password-123")
    advisor_one = management.create_advisor(
        operator.id,
        "statistics-advisor-one",
        "advisor-one-password-123",
        display_name="王顾问",
        organization="本地生活服务站",
    )
    advisor_two = management.create_advisor(
        operator.id,
        "statistics-advisor-two",
        "advisor-two-password-123",
        display_name="李顾问",
    )
    alice = auth.register("statistics-alice", "alice-member-password-123")
    bob = auth.register("statistics-bob", "bob-member-password-123")
    return database, auth, management, operator, advisor_one, advisor_two, alice, bob


def test_operator_gets_only_recorded_advisor_work_facts(
    work_statistics_fixture: tuple[
        Database,
        AuthService,
        ServiceManagementService,
        object,
        object,
        object,
        object,
        object,
    ],
) -> None:
    (
        _database,
        _auth,
        management,
        operator,
        advisor_one,
        advisor_two,
        alice,
        bob,
    ) = work_statistics_fixture
    management.bind_advisor(operator.id, alice.id, advisor_one.id)
    management.bind_advisor(operator.id, bob.id, advisor_one.id)
    when = datetime.now(UTC) + timedelta(days=1)

    management.create_visit_task(advisor_one.id, alice.id, title="待上门", scheduled_at=when)
    in_progress = management.create_visit_task(
        advisor_one.id, bob.id, title="服务中", scheduled_at=when
    )
    management.start_visit_task(advisor_one.id, in_progress)

    for index, details in enumerate(
        (
            {"duration_minutes": 45, "sop_completed": True},
            {"duration_minutes": 75},
            {"notes": "没有填写时长"},
        )
    ):
        task_id = management.create_visit_task(
            advisor_one.id,
            alice.id,
            title=f"已完成服务 {index + 1}",
            scheduled_at=when + timedelta(days=index + 1),
        )
        management.complete_visit_task(
            advisor_one.id,
            task_id,
            summary="已完成约定的生活服务事项",
            details=details,
        )

    second_task = management.create_visit_task(
        operator.id,
        alice.id,
        advisor_user_id=advisor_one.id,
        title="另一项已完成任务",
        scheduled_at=when + timedelta(days=5),
    )
    management.complete_visit_task(
        operator.id,
        second_task,
        summary="完成",
        details={"duration_minutes": "60"},
    )

    result = management.get_advisor_work_statistics(operator.id)
    rows = {row["advisor_user_id"]: row for row in result["advisors"]}
    one = rows[advisor_one.id]
    assert one == {
        "advisor_user_id": advisor_one.id,
        "username": "statistics-advisor-one",
        "account_status": "active",
        "display_name": "王顾问",
        "organization": "本地生活服务站",
        "specialty": None,
        "active_member_count": 2,
        "pending_task_count": 2,
        "completed_task_count": 4,
        "completed_visit_record_count": 4,
        "recorded_visit_minutes": 120,
        "records_with_duration_count": 2,
        "records_without_duration_count": 2,
    }
    assert rows[advisor_two.id]["active_member_count"] == 0
    assert rows[advisor_two.id]["pending_task_count"] == 0
    assert rows[advisor_two.id]["completed_task_count"] == 0
    assert rows[advisor_two.id]["completed_visit_record_count"] == 0
    assert rows[advisor_two.id]["recorded_visit_minutes"] == 0
    assert "未生成自动绩效分数" in result["notice"]
    forbidden_fragments = {"score", "rank", "rating", "medical_evaluation", "health_score"}
    assert not forbidden_fragments.intersection(one)


def test_advisor_work_statistics_reject_advisors_members_and_disabled_operators(
    work_statistics_fixture: tuple[
        Database,
        AuthService,
        ServiceManagementService,
        object,
        object,
        object,
        object,
        object,
    ],
) -> None:
    database, auth, management, operator, advisor, _other, member, _bob = work_statistics_fixture
    with pytest.raises(PermissionDeniedError):
        management.get_advisor_work_statistics(advisor.id)
    with pytest.raises(PermissionDeniedError):
        management.get_advisor_work_statistics(member.id)

    disabled_operator = auth.create_staff(
        operator.id,
        "disabled-statistics-operator",
        "disabled-operator-password-123",
        "operator",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET account_status = 'disabled' WHERE id = ?",
            (disabled_operator.id,),
        )
    with pytest.raises(PermissionDeniedError):
        management.get_advisor_work_statistics(disabled_operator.id)
