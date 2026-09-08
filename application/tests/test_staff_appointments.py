from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ollama_chat_app.data.database import Database, timestamp_to_db, utc_now
from ollama_chat_app.security.passwords import verify_and_rehash
from ollama_chat_app.services.appointment_service import AppointmentService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.service_management import (
    InvalidVisitStateError,
    ManagementValidationError,
    PermissionDeniedError,
    ServiceManagementService,
)


@pytest.fixture
def staff(tmp_path):
    database = Database(tmp_path / "synthetic_staff.db")
    auth = AuthService(database)
    management = ServiceManagementService(database)
    operator = auth.bootstrap_operator("synthetic-operator", "synthetic-operator-pass")
    advisor = management.create_advisor(
        operator.id,
        "synthetic-advisor",
        "synthetic-advisor-pass",
        display_name="合成顾问",
        valid_until=utc_now() + timedelta(days=365),
    )
    member = auth.register("synthetic-member", "synthetic-member-pass")
    other = auth.register("synthetic-other", "synthetic-other-pass")
    management.bind_advisor(operator.id, member.id, advisor.id)
    return database, management, AppointmentService(database), operator, advisor, member, other


def test_member_request_operator_reschedule_and_advisor_completion_share_one_record(staff):
    database, management, appointments, operator, advisor, member, other = staff
    task_id = appointments.create_request(
        member.id,
        "首次建档",
        "2030-01-02T10:00:00+08:00",
        "门铃联系",
        "合成地址 1 号",
        31.2,
        121.4,
    )
    assert appointments.list_for_member(other.id) == []
    assert management.list_visit_tasks(operator.id)[0]["id"] == task_id
    appointments.update_request(
        task_id,
        operator.id,
        scheduled_at="2030-01-03T11:00:00+08:00",
        service_type="居家整理",
        notes="已联系确认",
    )
    received = appointments.list_for_member(member.id)[0]
    assert received["service_type"] == "居家整理"
    assert received["address"] == "合成地址 1 号"
    assert received["advisor_id"] == advisor.id
    assert received["scheduled_at"].hour == 3
    management.start_visit_task(advisor.id, task_id)
    assert appointments.list_for_member(member.id)[0]["status"] == "in_progress"
    management.complete_visit_task(
        advisor.id,
        task_id,
        summary="合成服务已完成",
        visited_at="2030-01-03T12:00:00+08:00",
        details={"duration_minutes": 60},
    )
    assert appointments.list_for_member(member.id)[0]["status"] == "completed"
    with pytest.raises(InvalidVisitStateError):
        appointments.update_request(task_id, operator.id, status="disabled")
    with database.connect() as connection:
        actions = {row[0] for row in connection.execute("SELECT action FROM audit_logs")}
    assert {"appointment.requested", "appointment.updated", "visit_task.completed"} <= actions


def test_appointment_statuses_permissions_and_coordinates(staff):
    _, management, appointments, operator, advisor, member, other = staff
    task = appointments.create_request(member.id, "生活协助")
    with pytest.raises(PermissionDeniedError):
        appointments.update_request(task, member.id, status="disabled")
    with pytest.raises(PermissionDeniedError):
        management.get_member_overview(advisor.id, other.id)
    for state in ("incomplete", "pending", "disabled"):
        appointments.update_request(task, operator.id, status=state)
        assert appointments.list_for_member(member.id)[0]["status"] == state
        assert management.list_visit_tasks(operator.id, status=state)[0]["id"] == task
    with pytest.raises(InvalidVisitStateError):
        management.start_visit_task(advisor.id, task)
    with pytest.raises(ManagementValidationError):
        appointments.create_request(member.id, "生活协助", latitude=float("nan"), longitude=1)
    appointments.update_request(task, operator.id, advisor_id=None)
    assert appointments.list_for_member(member.id)[0]["advisor_id"] is None


def test_password_reset_is_one_way_and_list_is_safe(staff):
    database, management, _, operator, _, member, _ = staff
    management.reset_account_password(operator.id, member.id, "synthetic-replacement-pass")
    with database.transaction() as connection:
        now = timestamp_to_db(utc_now())
        connection.execute(
            "INSERT INTO user_preferences (user_id, preferences_json, updated_at) VALUES (?, ?, ?)",
            (member.id, json.dumps({"nickname": "合成昵称", "api_key": "never-export"}), now),
        )
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?", (member.id,)
        ).fetchone()[0]
        audit = str([tuple(row) for row in connection.execute("SELECT * FROM audit_logs")])
    assert verify_and_rehash(stored, "synthetic-replacement-pass").valid
    exported = management.list_members(operator.id)
    target = next(row for row in exported if row["id"] == member.id)
    assert target["nickname"] == "合成昵称" and target["password_set"] == 1
    assert "password_hash" not in target and "preferences_json" not in target
    assert "never-export" not in str(exported)
    assert "synthetic-replacement-pass" not in audit and stored not in audit


def test_expired_advisor_cannot_reuse_logged_in_session(staff):
    database, management, _, operator, advisor, member, _ = staff
    with database.transaction() as connection:
        connection.execute(
            "UPDATE staff_account_terms SET starts_at = ?, ends_at = ? WHERE user_id = ?",
            ("2020-01-01T00:00:00.000000Z", "2021-01-01T00:00:00.000000Z", advisor.id),
        )
    with pytest.raises(PermissionDeniedError):
        management.get_member_overview(advisor.id, member.id)
    assert management.list_members(operator.id)


def test_work_statistics_combines_member_advisor_and_half_open_time_filters(staff):
    _, management, appointments, operator, advisor, member, _ = staff
    for day in (2, 3):
        visited = datetime(2030, 1, day, tzinfo=UTC)
        task = appointments.create_request(member.id, "生活协助", visited)
        management.complete_visit_task(
            advisor.id,
            task,
            summary="合成记录",
            visited_at=visited,
            details={"duration_minutes": 30},
        )
    stats = management.get_filtered_work_statistics(
        operator.id,
        advisor_user_id=advisor.id,
        member_user_id=member.id,
        starts_at="2030-01-02T00:00:00Z",
        ends_at="2030-01-03T00:00:00Z",
    )
    assert stats["advisors"][0]["completed_visit_record_count"] == 1
    assert stats["advisors"][0]["recorded_visit_minutes"] == 30
    assert stats["daily"] == [{"date": "2030-01-02", "count": 1}]


def test_staff_workspace_table_navigation_and_charts_use_synthetic_records(staff, qtbot, tmp_path):
    from ollama_chat_app.ui.operator_workspace import CreateAdvisorDialog, OperatorWorkspace

    _, management, appointments, operator, advisor, member, _ = staff
    task = appointments.create_request(member.id, "生活协助", "2030-01-02T10:00:00+08:00")
    management.complete_visit_task(
        advisor.id,
        task,
        summary="合成记录",
        visited_at="2030-01-02T11:00:00+08:00",
        details={"duration_minutes": 45},
    )
    workspace = OperatorWorkspace(management)
    qtbot.addWidget(workspace)
    workspace.resize(1440, 960)
    workspace.start_session(operator)
    workspace.show()
    labels = [workspace.tabs.tabText(index) for index in range(workspace.tabs.count())]
    assert "概览" not in labels and "AI 配置" not in labels
    assert "顾问管理与上门服务" in labels
    workspace.view_members_button.click()
    assert workspace.member_views.currentIndex() == 1
    assert workspace.member_table.rowCount() == 2
    workspace.member_table.selectRow(1)
    assert workspace.member_list.currentRow() == 1
    assert workspace.member_table.item(1, 2).text() in workspace.member_detail.text()
    assert "password_hash" not in workspace.member_detail.text()
    qtbot.wait(30)
    assert workspace.grab().save(str(tmp_path / "staff-members.png"))
    workspace.tabs.setCurrentIndex(1)
    assert workspace.grab().save(str(tmp_path / "staff-visits.png"))
    panel = workspace.performance_panel
    workspace.tabs.setCurrentWidget(panel)
    for mode in ("table", "bar", "line", "pie"):
        panel.view_filter.setCurrentIndex(panel.view_filter.findData(mode))
        qtbot.wait(10)
        assert workspace.grab().save(str(tmp_path / f"staff-{mode}.png"))
    dialog = CreateAdvisorDialog()
    qtbot.addWidget(dialog)
    assert dialog.layout().rowCount() == 5  # Four requested fields plus actions.
    assert "valid_until" in dialog.values
    workspace.end_session()
    assert workspace.member_table.rowCount() == 0
    assert not workspace.performance_panel.chart.values
    print(f"Synthetic staff UI screenshots: {tmp_path}")
