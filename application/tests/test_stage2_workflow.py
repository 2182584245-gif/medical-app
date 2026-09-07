from __future__ import annotations

import os
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.backup import PortableBackupService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService
from ollama_chat_app.ui.advisor_workspace import AdvisorWorkspace
from ollama_chat_app.ui.main_window import MainWindow
from ollama_chat_app.ui.operator_workspace import OperatorWorkspace


def test_stage2_roles_share_one_portable_database_and_member_sees_service(
    qtbot, tmp_path: Path
) -> None:
    database = Database(tmp_path / "portable-data" / "app.db")
    database.initialize()
    auth = AuthService(database)
    management = ServiceManagementService(database)
    health = HealthService(database)
    backup = PortableBackupService(database)

    operator = auth.bootstrap_operator("operator", "operator-password-123")
    member = auth.register("member", "member-password-123")
    advisor = management.create_advisor(
        operator.id,
        "advisor",
        "advisor-password-123",
        display_name="王老师",
        organization="家庭生活服务站",
        specialty="居家生活整理",
    )
    management.save_membership(
        operator.id,
        member.id,
        plan_code="家庭基础会员",
        starts_at="2030-01-01T00:00:00+08:00",
        ends_at="2031-01-01T00:00:00+08:00",
        benefits={"visit_total": 4, "visit_used": 0, "first_filing": False},
    )
    management.bind_advisor(operator.id, member.id, advisor.id)
    management.create_visit_task(
        operator.id,
        member.id,
        title="首次上门建档",
        scheduled_at="2030-01-10T09:00:00+08:00",
        advisor_user_id=advisor.id,
    )

    advisor_workspace = AdvisorWorkspace(management, backup)
    operator_workspace = OperatorWorkspace(management, backup)
    window = MainWindow(
        auth,
        ChatService(database),
        SecretStore(),
        backup,
        health,
        management,
        advisor_workspace,
        operator_workspace,
    )
    qtbot.addWidget(window)

    window._login_succeeded(advisor)
    assert window.pages.currentWidget() is advisor_workspace
    assert "首次上门建档" in advisor_workspace.task_list.item(0).text()
    assert "王老师" not in advisor_workspace.member_list.item(0).text()
    window._logout()

    window._login_succeeded(operator)
    assert window.pages.currentWidget() is operator_workspace
    assert "家庭基础会员" in operator_workspace.member_list.item(0).text()
    assert "王老师" in operator_workspace.member_list.item(0).text()
    window._logout()

    window._login_succeeded(member)
    assert window.pages.currentWidget() is window.member_workspace
    window.health_workspace.show_page(3)
    assert "家庭基础会员" in window.health_workspace.service_page.membership_label.text()
    assert "王老师" in window.health_workspace.service_page.advisor_label.text()
    assert "首次上门建档" in window.health_workspace.service_page.visit_label.text()
