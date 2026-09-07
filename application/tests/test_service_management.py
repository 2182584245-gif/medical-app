from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthenticationError, AuthService
from ollama_chat_app.services.service_management import (
    InvalidVisitStateError,
    ManagementValidationError,
    PermissionDeniedError,
    ServiceManagementService,
)


@pytest.fixture
def management_fixture(
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
    database = Database(tmp_path / "app.db")
    auth = AuthService(database)
    management = ServiceManagementService(database)
    operator = auth.bootstrap_operator("operations", "operator-password-123")
    second_operator = auth.create_staff(
        operator.id, "operations-two", "operator-two-password-123", "operator"
    )
    advisor_one = management.create_advisor(
        operator.id,
        "advisor-one",
        "advisor-one-password-123",
        display_name="王顾问",
        specialty="家庭生活管理",
    )
    advisor_two = management.create_advisor(
        operator.id,
        "advisor-two",
        "advisor-two-password-123",
        display_name="李顾问",
    )
    alice = auth.register("alice", "alice-member-password-123")
    bob = auth.register("bob", "bob-member-password-123")
    return (
        database,
        auth,
        management,
        operator,
        second_operator,
        advisor_one,
        advisor_two,
        alice,
        bob,
    )


def test_operator_dashboard_members_advisors_membership_and_binding(
    management_fixture,
) -> None:
    (
        database,
        _auth,
        management,
        operator,
        _second_operator,
        advisor_one,
        advisor_two,
        alice,
        bob,
    ) = management_fixture
    starts = datetime(2030, 1, 1, tzinfo=UTC)
    ends = datetime(2031, 1, 1, tzinfo=UTC)

    membership_id = management.save_membership(
        operator.id,
        alice.id,
        plan_code="annual-basic",
        starts_at=starts,
        ends_at=ends,
        benefits={"visit_total": 4, "gift": "米面油"},
    )
    binding_id = management.bind_advisor(operator.id, alice.id, advisor_one.id)

    dashboard = management.get_dashboard(operator.id)
    assert dashboard == {
        "actor_user_id": operator.id,
        "role_code": "operator",
        "member_count": 2,
        "advisor_count": 2,
        "active_membership_count": 1,
        "pending_visit_count": 0,
        "completed_visit_count": 0,
    }
    members = management.list_members(operator.id)
    assert [item["id"] for item in members] == [alice.id, bob.id]
    alice_summary = next(item for item in members if item["id"] == alice.id)
    assert alice_summary["membership_id"] == membership_id
    assert alice_summary["plan_code"] == "annual-basic"
    assert alice_summary["membership_benefits"] == {"gift": "米面油", "visit_total": 4}
    assert alice_summary["advisor_user_id"] == advisor_one.id
    assert alice_summary["advisor_name"] == "王顾问"
    advisors = management.list_advisors(operator.id)
    assert [item["id"] for item in advisors] == [advisor_two.id, advisor_one.id]
    advisor_summary = next(item for item in advisors if item["id"] == advisor_one.id)
    assert advisor_summary["active_member_count"] == 1

    with database.connect() as connection:
        audited_entities = {
            (str(row["action"]), str(row["entity_type"]), int(row["entity_id"]))
            for row in connection.execute(
                """
                SELECT action, entity_type, entity_id FROM audit_logs
                WHERE actor_user_id = ?
                """,
                (operator.id,),
            )
        }
    assert ("membership.created", "membership", membership_id) in audited_entities
    assert ("advisor_binding.created", "advisor_binding", binding_id) in audited_entities


def test_membership_update_preserves_one_active_record(management_fixture) -> None:
    database, _auth, management, operator, *_staff, alice, _bob = management_fixture
    original_id = management.save_membership(
        operator.id,
        alice.id,
        plan_code="家庭基础会员",
        starts_at="2030-01-01T00:00:00+08:00",
        ends_at="2031-01-01T00:00:00+08:00",
        benefits={"visit_total": 4, "visit_used": 2},
    )
    assert (
        management.save_membership(
            operator.id,
            alice.id,
            membership_id=original_id,
            plan_code="家庭基础会员",
            starts_at="2030-01-01T00:00:00+08:00",
            ends_at="2031-06-01T00:00:00+08:00",
            benefits={"visit_total": 6, "visit_used": 2},
        )
        == original_id
    )
    replacement_id = management.save_membership(
        operator.id,
        alice.id,
        plan_code="家庭升级会员",
        starts_at="2031-06-01T00:00:00+08:00",
        ends_at="2032-06-01T00:00:00+08:00",
        benefits={"visit_total": 8, "visit_used": 0},
    )

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, status, benefits_json FROM memberships WHERE user_id = ? ORDER BY id",
            (alice.id,),
        ).fetchall()
    assert [(int(row["id"]), str(row["status"])) for row in rows] == [
        (original_id, "expired"),
        (replacement_id, "active"),
    ]
    assert json.loads(str(rows[0]["benefits_json"]))["visit_used"] == 2
    assert management.get_dashboard(operator.id)["active_membership_count"] == 1


def test_advisor_can_only_see_active_bound_members_and_own_active_tasks(
    management_fixture,
) -> None:
    (
        _database,
        _auth,
        management,
        operator,
        _second_operator,
        advisor_one,
        advisor_two,
        alice,
        bob,
    ) = management_fixture
    management.bind_advisor(operator.id, alice.id, advisor_one.id)
    management.bind_advisor(operator.id, bob.id, advisor_two.id)
    task_id = management.create_visit_task(
        advisor_one.id,
        alice.id,
        title="首次上门",
        scheduled_at="2030-01-10T09:00:00+08:00",
    )

    assert [item["id"] for item in management.list_members(advisor_one.id)] == [alice.id]
    assert management.get_member_overview(advisor_one.id, alice.id)["profile"]["id"] == alice.id
    assert [item["id"] for item in management.list_visit_tasks(advisor_one.id)] == [task_id]

    with pytest.raises(PermissionDeniedError):
        management.get_member_overview(advisor_one.id, bob.id)
    with pytest.raises(PermissionDeniedError):
        management.list_visit_tasks(advisor_one.id, member_user_id=bob.id)
    with pytest.raises(PermissionDeniedError):
        management.create_visit_task(
            advisor_one.id,
            bob.id,
            title="越权任务",
            scheduled_at="2030-01-11T09:00:00+08:00",
        )
    with pytest.raises(PermissionDeniedError):
        management.start_visit_task(advisor_two.id, task_id)
    with pytest.raises(PermissionDeniedError):
        management.complete_visit_task(
            advisor_two.id,
            task_id,
            summary="不应允许保存",
        )


def test_complete_visit_creates_record_next_task_and_reminder_atomically(
    management_fixture,
) -> None:
    (
        database,
        _auth,
        management,
        operator,
        _second_operator,
        advisor_one,
        _advisor_two,
        alice,
        _bob,
    ) = management_fixture
    management.bind_advisor(operator.id, alice.id, advisor_one.id)
    membership_id = management.save_membership(
        operator.id,
        alice.id,
        plan_code="家庭基础会员",
        starts_at="2030-01-01T00:00:00+08:00",
        ends_at="2031-01-01T00:00:00+08:00",
        benefits={"visit_total": 4, "visit_used": 0, "first_filing": False},
    )
    task_id = management.create_visit_task(
        operator.id,
        alice.id,
        advisor_user_id=advisor_one.id,
        title="首次上门",
        scheduled_at="2030-01-10T09:00:00+08:00",
        notes="建立信任、建立档案、教会使用 AI",
    )
    management.start_visit_task(advisor_one.id, task_id)

    result = management.complete_visit_task(
        advisor_one.id,
        task_id,
        summary="已完成首次建档和 AI 使用教学",
        visited_at="2030-01-10T10:00:00+08:00",
        details={
            "gifts": [{"name": "大米", "quantity": 1, "delivered": True}],
            "needs": ["下次复查厨房和冰箱使用情况"],
            "sop_completed": True,
            "uses_membership_benefit": True,
            "first_filing_completed": True,
        },
        next_visit_at="2030-02-10T09:00:00+08:00",
        next_visit_title="首次复访",
    )

    assert result["task_id"] == task_id
    assert isinstance(result["visit_record_id"], int)
    assert isinstance(result["next_task_id"], int)
    assert isinstance(result["reminder_id"], int)
    records = management.list_visit_records(advisor_one.id, member_user_id=alice.id)
    assert len(records) == 1
    assert records[0]["summary"] == "已完成首次建档和 AI 使用教学"
    assert records[0]["details"]["sop_completed"] is True
    tasks = management.list_visit_tasks(alice.id)
    assert {item["status"] for item in tasks} == {"pending", "completed"}
    assert any(item["id"] == result["next_task_id"] for item in tasks)

    with database.connect() as connection:
        original_status = connection.execute(
            "SELECT status FROM visit_tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        reminder = connection.execute(
            """
            SELECT user_id, reminder_type, status FROM reminders WHERE id = ?
            """,
            (result["reminder_id"],),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT details_json FROM audit_logs
            WHERE action = 'visit_task.completed' AND entity_id = ?
            """,
            (task_id,),
        ).fetchone()
        membership = connection.execute(
            "SELECT benefits_json FROM memberships WHERE id = ?", (membership_id,)
        ).fetchone()
    assert original_status == "completed"
    assert tuple(reminder) == (alice.id, "visit", "active")
    assert json.loads(str(audit["details_json"]))["visit_record_id"] == result["visit_record_id"]
    benefits = json.loads(str(membership["benefits_json"]))
    assert benefits["visit_used"] == 1
    assert benefits["first_filing"] is True

    with pytest.raises(InvalidVisitStateError, match="不能重复完成"):
        management.complete_visit_task(advisor_one.id, task_id, summary="重复提交")
    assert len(management.list_visit_records(operator.id)) == 1


def test_invalid_next_visit_rolls_back_and_member_cannot_cross_account(
    management_fixture,
) -> None:
    (
        database,
        _auth,
        management,
        operator,
        _second_operator,
        advisor_one,
        _advisor_two,
        alice,
        bob,
    ) = management_fixture
    management.bind_advisor(operator.id, alice.id, advisor_one.id)
    task_id = management.create_visit_task(
        operator.id,
        alice.id,
        title="首次上门",
        scheduled_at="2030-01-10T09:00:00Z",
    )

    with pytest.raises(ManagementValidationError, match="必须晚于"):
        management.complete_visit_task(
            advisor_one.id,
            task_id,
            summary="不会保存",
            visited_at="2030-01-10T10:00:00Z",
            next_visit_at="2030-01-09T10:00:00Z",
        )

    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM visit_tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
            == "pending"
        )
        assert connection.execute("SELECT COUNT(*) FROM visit_records").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0

    assert [item["id"] for item in management.list_members(alice.id)] == [alice.id]
    assert [item["id"] for item in management.list_visit_tasks(alice.id)] == [task_id]
    with pytest.raises(PermissionDeniedError):
        management.get_member_overview(alice.id, bob.id)
    with pytest.raises(PermissionDeniedError):
        management.list_visit_tasks(alice.id, member_user_id=bob.id)
    with pytest.raises(PermissionDeniedError):
        management.create_visit_task(
            alice.id,
            alice.id,
            title="会员不能创建任务",
            scheduled_at=datetime.now(UTC) + timedelta(days=1),
        )


def test_rebinding_revokes_old_advisor_access_and_account_status_is_enforced(
    management_fixture,
) -> None:
    (
        database,
        auth,
        management,
        operator,
        second_operator,
        advisor_one,
        advisor_two,
        alice,
        _bob,
    ) = management_fixture
    first_binding = management.bind_advisor(operator.id, alice.id, advisor_one.id)
    open_task = management.create_visit_task(
        advisor_one.id,
        alice.id,
        title="待迁移任务",
        scheduled_at="2030-03-10T09:00:00+08:00",
    )
    completed_task = management.create_visit_task(
        advisor_one.id,
        alice.id,
        title="已完成历史任务",
        scheduled_at="2030-02-10T09:00:00+08:00",
    )
    management.complete_visit_task(
        advisor_one.id,
        completed_task,
        summary="保留原顾问的历史归属",
    )
    replacement = management.bind_advisor(operator.id, alice.id, advisor_two.id)
    assert replacement != first_binding

    with pytest.raises(PermissionDeniedError):
        management.get_member_overview(advisor_one.id, alice.id)
    assert management.get_member_overview(advisor_two.id, alice.id)["profile"]["id"] == alice.id
    assert [item["id"] for item in management.list_visit_tasks(advisor_two.id)] == [open_task]
    assert [item["task_id"] for item in management.list_visit_records(advisor_one.id)] == [
        completed_task
    ]
    assert management.list_visit_records(advisor_two.id) == []

    with pytest.raises(ManagementValidationError, match="先把会员换绑"):
        management.set_account_enabled(operator.id, advisor_two.id, enabled=False)
    management.bind_advisor(operator.id, alice.id, advisor_one.id)
    management.set_account_enabled(operator.id, advisor_two.id, enabled=False)
    with pytest.raises(AuthenticationError):
        auth.authenticate("advisor-two", "advisor-two-password-123")
    with pytest.raises(PermissionDeniedError):
        management.list_members(advisor_two.id)
    management.set_account_enabled(operator.id, advisor_two.id, enabled=True)
    assert auth.authenticate("advisor-two", "advisor-two-password-123").id == advisor_two.id

    with pytest.raises(ManagementValidationError, match="当前正在操作"):
        management.set_account_enabled(operator.id, operator.id, enabled=False)
    management.set_account_enabled(operator.id, second_operator.id, enabled=False)
    with pytest.raises(PermissionDeniedError):
        management.list_advisors(second_operator.id)

    with database.connect() as connection:
        bindings = connection.execute(
            """
            SELECT id, status, ended_at FROM advisor_bindings
            WHERE member_user_id = ? ORDER BY id
            """,
            (alice.id,),
        ).fetchall()
        account_actions = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT action FROM audit_logs
                WHERE actor_user_id = ? AND entity_type = 'user'
                ORDER BY id
                """,
                (operator.id,),
            )
        ]
        task_owners = {
            int(row["id"]): int(row["advisor_user_id"])
            for row in connection.execute(
                """
                SELECT id, advisor_user_id FROM visit_tasks
                WHERE id IN (?, ?)
                """,
                (open_task, completed_task),
            )
        }
        replacement_audit = connection.execute(
            """
            SELECT details_json FROM audit_logs
            WHERE action = 'advisor_binding.replaced' AND entity_id = ?
            """,
            (replacement,),
        ).fetchone()
    assert str(bindings[0]["status"]) == "ended"
    assert bindings[0]["ended_at"] is not None
    assert str(bindings[1]["status"]) == "ended"
    assert str(bindings[2]["status"]) == "active"
    assert task_owners[open_task] == advisor_one.id
    assert task_owners[completed_task] == advisor_one.id
    assert json.loads(str(replacement_audit["details_json"]))["reassigned_open_task_count"] == 1
    assert "account.disabled" in account_actions
    assert "account.enabled" in account_actions


def test_exhausted_membership_benefit_rolls_back_visit_completion(
    management_fixture,
) -> None:
    database, _auth, management, operator, _second, advisor, _other, alice, _bob = (
        management_fixture
    )
    management.bind_advisor(operator.id, alice.id, advisor.id)
    management.save_membership(
        operator.id,
        alice.id,
        plan_code="一次上门方案",
        starts_at="2030-01-01T00:00:00+08:00",
        ends_at="2031-01-01T00:00:00+08:00",
        benefits={"visit_total": 1, "visit_used": 1},
    )
    task_id = management.create_visit_task(
        advisor.id,
        alice.id,
        title="超额上门检查",
        scheduled_at="2030-02-01T09:00:00+08:00",
    )

    with pytest.raises(ManagementValidationError, match="次数已用尽"):
        management.complete_visit_task(
            advisor.id,
            task_id,
            summary="这次不能计入权益",
            details={"uses_membership_benefit": True},
        )

    with database.connect() as connection:
        task_status = connection.execute(
            "SELECT status FROM visit_tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        record_count = connection.execute(
            "SELECT COUNT(*) FROM visit_records WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert task_status == "pending"
    assert record_count == 0
