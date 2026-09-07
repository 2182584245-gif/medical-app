from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QMessageBox

from ollama_chat_app.ui.operator_workspace import (
    MembershipDialog,
    OperatorWorkspace,
    ProductDialog,
)


class FakeManagementService:
    def get_dashboard(self, actor_user_id: int) -> dict[str, int]:
        assert actor_user_id == 9
        return {
            "member_count": 1,
            "advisor_count": 1,
            "active_membership_count": 1,
            "pending_visit_count": 1,
            "completed_visit_count": 2,
        }

    def list_members(self, actor_user_id: int) -> list[dict[str, Any]]:
        assert actor_user_id == 9
        return [
            {
                "id": 1,
                "username": "member-one",
                "display_name": "李阿姨",
                "account_status": "active",
                "plan_code": "家庭基础会员",
                "membership_status": "active",
                "membership_ends_at": datetime(2027, 9, 1, tzinfo=UTC),
                "advisor_user_id": 2,
                "advisor_name": "王老师",
                "next_visit_at": datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
                "phone": "13800000000",
                "living_situation": "与家人同住",
            }
        ]

    def list_advisors(self, actor_user_id: int) -> list[dict[str, Any]]:
        assert actor_user_id == 9
        return [
            {
                "id": 2,
                "username": "advisor-one",
                "display_name": "王老师",
                "account_status": "active",
                "organization": "家庭生活服务站",
                "specialty": "居家生活整理",
                "active_member_count": 1,
            }
        ]

    def list_visit_tasks(self, actor_user_id: int) -> list[dict[str, Any]]:
        assert actor_user_id == 9
        return [
            {
                "id": 5,
                "title": "首次上门建档",
                "status": "pending",
                "scheduled_at": datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
                "member_name": "李阿姨",
                "advisor_name": "王老师",
                "notes": "确认生活档案",
            }
        ]

    def list_visit_records(self, actor_user_id: int) -> list[dict[str, Any]]:
        assert actor_user_id == 9
        return [
            {
                "id": 6,
                "visited_at": datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
                "member_name": "李阿姨",
                "advisor_name": "王老师",
                "summary": "完成初步沟通",
                "details": {"duration_minutes": 45},
            }
        ]


class FakeCommerceService:
    def __init__(self) -> None:
        self.products = [
            {
                "id": 41,
                "sku": "CUP-001",
                "name": "家用保温杯",
                "category": "daily",
                "description": "便于在家中随手饮水和记录杯数",
                "price_cents": 3990,
                "unit": "个",
                "is_active": True,
            }
        ]
        self.orders = [
            {
                "id": 51,
                "member_username": "member-one",
                "product_name_snapshot": "家用保温杯",
                "quantity": 2,
                "total_amount_cents": 7980,
                "status": "created",
                "created_at": "2026-09-05T10:00:00+08:00",
            }
        ]
        self.active_changes: list[tuple[int, int, bool]] = []
        self.delivered: list[tuple[int, int]] = []

    def list_products(self, actor_user_id: int, *, include_inactive: bool) -> list[dict[str, Any]]:
        assert (actor_user_id, include_inactive) == (9, True)
        return [dict(product) for product in self.products]

    def list_orders(self, actor_user_id: int) -> list[dict[str, Any]]:
        assert actor_user_id == 9
        return [dict(order) for order in self.orders]

    def set_product_active(self, actor_user_id: int, product_id: int, is_active: bool) -> None:
        self.active_changes.append((actor_user_id, product_id, is_active))
        self.products[0]["is_active"] = is_active

    def mark_order_delivered(self, actor_user_id: int, order_id: int) -> None:
        self.delivered.append((actor_user_id, order_id))
        self.orders[0]["status"] = "delivered"


def test_operator_workspace_loads_role_scoped_service_data(qtbot) -> None:
    workspace = OperatorWorkspace(FakeManagementService(), backup_service=object())
    qtbot.addWidget(workspace)

    workspace.start_session(SimpleNamespace(id=9, username="operator"))

    assert workspace.actor_user_id == 9
    assert workspace.metric_labels["member_count"].text() == "1"
    assert workspace.metric_labels["completed_visit_count"].text() == "2"
    assert workspace.member_list.count() == 1
    assert "李阿姨" in workspace.member_list.item(0).text()
    assert "王老师" in workspace.member_detail.text()
    assert workspace.advisor_list.count() == 1
    assert "家庭生活服务站" in workspace.advisor_list.item(0).text()
    assert "首次上门建档" in workspace.task_list.item(0).text()
    assert "完成初步沟通" in workspace.visit_record_list.item(0).text()
    assert workspace.export_button.isEnabled()
    assert workspace.import_button.isEnabled()

    workspace.end_session()
    assert workspace.actor_user_id is None
    assert workspace.current_user is None


def test_operator_workspace_emits_shell_actions(qtbot) -> None:
    workspace = OperatorWorkspace(FakeManagementService(), backup_service=object())
    qtbot.addWidget(workspace)
    exported: list[bool] = []
    imported: list[bool] = []
    workspace.export_requested.connect(lambda: exported.append(True))
    workspace.import_requested.connect(lambda: imported.append(True))

    workspace.export_button.click()
    workspace.import_button.click()

    assert exported == [True]
    assert imported == [True]


def test_membership_dialog_edits_existing_record_without_resetting_usage(qtbot) -> None:
    dialog = MembershipDialog(
        "李阿姨",
        {
            "membership_id": 18,
            "plan_code": "家庭基础会员",
            "membership_status": "active",
            "membership_starts_at": datetime(2026, 1, 1, tzinfo=UTC),
            "membership_ends_at": datetime(2027, 1, 1, tzinfo=UTC),
            "membership_benefits": {
                "visit_total": 6,
                "visit_used": 2,
                "first_filing": True,
                "gift": "米面油",
            },
        },
    )
    qtbot.addWidget(dialog)

    values = dialog.values
    assert values["membership_id"] == 18
    assert values["benefits"] == {
        "visit_total": 6,
        "visit_used": 2,
        "first_filing": True,
        "gift": "米面油",
    }


def test_product_dialog_returns_cents_and_non_medical_description(qtbot) -> None:
    dialog = ProductDialog()
    qtbot.addWidget(dialog)
    dialog.sku_input.setText("cup-001")
    dialog.name_input.setText("家用保温杯")
    dialog.category_input.setCurrentIndex(dialog.category_input.findData("daily"))
    dialog.description_input.setPlainText("便于在家中随手饮水和记录杯数")
    dialog.price_input.setValue(39.90)
    dialog.unit_input.setText("个")

    assert dialog.values == {
        "sku": "CUP-001",
        "name": "家用保温杯",
        "category": "daily",
        "brand": None,
        "specification": None,
        "description": "便于在家中随手饮水和记录杯数",
        "price_cents": 3990,
        "unit": "个",
        "source_type": "self_operated",
        "is_active": True,
    }


def test_operator_can_manage_products_and_simulate_delivery(qtbot, monkeypatch) -> None:
    commerce = FakeCommerceService()
    workspace = OperatorWorkspace(
        FakeManagementService(), backup_service=None, commerce_service=commerce
    )
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=9, username="operator"))

    assert "家用保温杯" in workspace.product_list.item(0).text()
    assert "已上架" in workspace.product_list.item(0).text()
    assert workspace.tabs.tabText(5) == "模拟订单"
    assert any(
        "没有真实支付" in label.text()
        for label in workspace.tabs.widget(5).findChildren(type(workspace.status_label))
    )

    workspace.product_list.setCurrentRow(0)
    workspace._toggle_product()
    assert commerce.active_changes == [(9, 41, False)]
    assert "已下架" in workspace.product_list.item(0).text()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    workspace.order_list.setCurrentRow(0)
    workspace._mark_order_delivered()
    assert commerce.delivered == [(9, 51)]
    assert "已模拟交付" in workspace.order_list.item(0).text()
