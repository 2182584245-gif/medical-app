from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QDialog

from ollama_chat_app.ui.advisor_workspace import (
    AdvisorVisitCompletionDialog,
    AdvisorWorkspace,
)


class FakeServiceManagement:
    def __init__(self) -> None:
        self.task_status = "pending"
        self.started: list[tuple[int, int]] = []
        self.completed: list[tuple[int, int, dict[str, object]]] = []
        self.history: list[dict[str, object]] = [
            {
                "id": 31,
                "task_id": 90,
                "member_user_id": 7,
                "advisor_user_id": 23,
                "member_name": "李明",
                "advisor_name": "王顾问",
                "visited_at": "2026-08-01T10:00:00+08:00",
                "summary": "完成首次建档",
                "details": {"duration_minutes": 60, "inspected_areas": "厨房"},
            }
        ]

    def get_dashboard(self, actor_user_id: int) -> dict[str, object]:
        assert actor_user_id == 23
        return {
            "role_code": "advisor",
            "active_member_count": 1,
            "pending_visit_count": 1 if self.task_status != "completed" else 0,
            "completed_visit_count": len(self.history),
        }

    def list_members(self, actor_user_id: int) -> list[dict[str, object]]:
        assert actor_user_id == 23
        return [
            {
                "id": 7,
                "username": "member-li",
                "display_name": "李明",
                "phone": "13800000000",
                "living_situation": "与家人同住",
                "next_visit_at": "2026-10-01T09:00:00+08:00",
            }
        ]

    def get_member_overview(
        self,
        actor_user_id: int,
        member_user_id: int,
        *,
        recent_record_limit: int,
    ) -> dict[str, object]:
        assert (actor_user_id, member_user_id, recent_record_limit) == (23, 7, 20)
        return {
            "profile": {
                "id": 7,
                "username": "member-li",
                "display_name": "李明",
                "birth_date": "1960-01-02",
                "gender": "male",
                "phone": "13800000000",
                "living_situation": "与家人同住",
                "emergency_contact_name": "李华",
                "emergency_contact_phone": "13900000000",
                "health_goals": "保持规律作息",
                "dietary_preferences": "清淡",
            },
            "confirmed_facts": [
                {
                    "id": 1,
                    "fact_key": "常用饮水杯容量",
                    "value": {"ml": 300},
                    "source": "member",
                    "effective_at": "2026-09-01T08:00:00+08:00",
                }
            ],
            "recent_life_records": [
                {
                    "id": 2,
                    "category": "water",
                    "occurred_at": "2026-09-04T09:10:00+08:00",
                    "content": "喝了一杯水",
                    "source": "member",
                }
            ],
        }

    def list_visit_tasks(self, actor_user_id: int) -> list[dict[str, object]]:
        assert actor_user_id == 23
        if self.task_status == "completed":
            return []
        return [
            {
                "id": 91,
                "member_user_id": 7,
                "advisor_user_id": 23,
                "member_name": "李明",
                "advisor_name": "王顾问",
                "title": "定期上门",
                "scheduled_at": "2026-10-01T09:00:00+08:00",
                "status": self.task_status,
                "notes": "核对近期记录",
            }
        ]

    def list_visit_records(self, actor_user_id: int) -> list[dict[str, object]]:
        assert actor_user_id == 23
        return list(self.history)

    def start_visit_task(self, actor_user_id: int, task_id: int) -> None:
        self.started.append((actor_user_id, task_id))
        self.task_status = "in_progress"

    def complete_visit_task(
        self, actor_user_id: int, task_id: int, **values: object
    ) -> dict[str, int | None]:
        self.completed.append((actor_user_id, task_id, values))
        self.task_status = "completed"
        self.history.insert(
            0,
            {
                "id": 32,
                "task_id": task_id,
                "member_user_id": 7,
                "advisor_user_id": actor_user_id,
                "member_name": "李明",
                "advisor_name": "王顾问",
                "visited_at": "2026-09-04T10:00:00+08:00",
                "summary": values["summary"],
                "details": values["details"],
            },
        )
        return {
            "task_id": task_id,
            "visit_record_id": 32,
            "next_task_id": 92,
            "reminder_id": 12,
        }


class FakeCommerceService:
    def __init__(self) -> None:
        self.recommended: list[tuple[int, int, int, str]] = []

    def list_products(
        self, actor_user_id: int, *, include_inactive: bool
    ) -> list[dict[str, object]]:
        assert (actor_user_id, include_inactive) == (23, False)
        return [
            {
                "id": 41,
                "name": "家用保温杯",
                "category": "daily",
                "description": "便于在客厅随手饮水和记录杯数",
                "price_cents": 3990,
                "unit": "个",
                "is_active": True,
            }
        ]

    def list_recommendations(self, actor_user_id: int) -> list[dict[str, object]]:
        assert actor_user_id == 23
        return [
            {
                "id": 61,
                "member_name": "李明",
                "product_name": "家用保温杯",
                "reason": "便于在客厅随手记录每日饮水量",
                "status": "new" if not self.recommended else "interested",
                "created_at": "2026-09-05T10:00:00+08:00",
            }
        ]

    def recommend_product(
        self,
        actor_user_id: int,
        member_user_id: int,
        product_id: int,
        reason: str,
    ) -> None:
        self.recommended.append((actor_user_id, member_user_id, product_id, reason))


def test_advisor_workspace_loads_only_service_scoped_data(qtbot) -> None:
    service = FakeServiceManagement()
    workspace = AdvisorWorkspace(service, backup_service=object())
    qtbot.addWidget(workspace)

    workspace.start_session(SimpleNamespace(id=23, username="advisor-wang"))

    assert "advisor-wang" in workspace.user_label.text()
    assert workspace.metric_labels["active_member_count"].text() == "1"
    assert workspace.metric_labels["pending_visit_count"].text() == "1"
    assert workspace.member_list.count() == 1
    assert "李明" in workspace.member_list.item(0).text()
    assert "与家人同住" in workspace.profile_text.toPlainText()
    assert "常用饮水杯容量" in workspace.facts_text.toPlainText()
    assert "喝了一杯水" in workspace.life_records_text.toPlainText()
    assert "定期上门" in workspace.task_list.item(0).text()
    assert "首次建档" in workspace.history_list.item(0).text()
    assert workspace.export_button.isEnabled()
    assert workspace.import_button.isEnabled()

    with qtbot.waitSignal(workspace.export_requested):
        workspace.export_button.click()
    with qtbot.waitSignal(workspace.import_requested):
        workspace.import_button.click()

    workspace.end_session()
    assert workspace.actor_user_id is None
    assert workspace.member_list.count() == 0
    assert workspace.task_list.count() == 0


def test_advisor_can_start_and_complete_selected_task(qtbot, monkeypatch) -> None:
    service = FakeServiceManagement()
    workspace = AdvisorWorkspace(service)
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=23, username="advisor-wang"))
    workspace.task_list.setCurrentRow(0)

    workspace._start_selected_task()

    assert service.started == [(23, 91)]
    assert "进行中" in workspace.task_list.item(0).text()
    assert not workspace.start_button.isEnabled()
    assert workspace.complete_button.isEnabled()

    completion_values: dict[str, object] = {
        "summary": "已核对近期生活记录",
        "details": {
            "duration_minutes": 75,
            "inspected_areas": "厨房、客厅",
            "user_questions": "饮水记录方式",
            "environment_changes": "无明显变化",
            "profile_changes": "建议确认新的作息时间",
            "follow_ups": "下次查看饮水记录",
            "gifts": "大米一袋",
            "notes": "沟通顺利",
        },
        "next_visit_at": "2026-10-05T09:00:00+08:00",
        "next_visit_title": "定期复访",
        "create_member_reminder": True,
    }

    class FakeCompletionDialog:
        def __init__(self, task: dict[str, Any], parent: object) -> None:
            assert task["id"] == 91
            assert parent is workspace

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        @property
        def values(self) -> dict[str, object]:
            return completion_values

    monkeypatch.setattr(
        "ollama_chat_app.ui.advisor_workspace.AdvisorVisitCompletionDialog",
        FakeCompletionDialog,
    )
    workspace._complete_selected_task()

    assert service.completed == [(23, 91, completion_values)]
    assert workspace.tabs.currentIndex() == 3
    assert "已核对近期生活记录" in workspace.history_list.item(0).text()
    assert "会员提醒已创建" in workspace.status_label.text()
    assert "没有待处理" in workspace.task_list.item(0).text()


def test_completion_dialog_returns_all_required_visit_fields(qtbot) -> None:
    dialog = AdvisorVisitCompletionDialog({"id": 91, "member_name": "李明", "title": "定期上门"})
    qtbot.addWidget(dialog)
    dialog.summary_input.setPlainText("完成生活记录核对")
    dialog.duration_input.setValue(80)
    dialog.inspected_areas_input.setPlainText("厨房")
    dialog.user_questions_input.setPlainText("如何记录饮水")
    dialog.environment_changes_input.setPlainText("更换净水器")
    dialog.profile_changes_input.setPlainText("建议确认饮水来源")
    dialog.follow_ups_input.setPlainText("下次核对")
    dialog.gifts_input.setPlainText("礼品一份")
    dialog.notes_input.setPlainText("会员已理解")
    dialog.create_next_visit_input.setChecked(True)

    values = dialog.values
    details = values["details"]

    assert values["summary"] == "完成生活记录核对"
    assert isinstance(details, dict)
    assert details == {
        "duration_minutes": 80,
        "inspected_areas": "厨房",
        "user_questions": "如何记录饮水",
        "environment_changes": "更换净水器",
        "profile_changes": "建议确认饮水来源",
        "follow_ups": "下次核对",
        "gifts": "礼品一份",
        "notes": "会员已理解",
        "uses_membership_benefit": True,
        "first_filing_completed": False,
    }
    assert isinstance(values["next_visit_at"], str)
    assert values["create_member_reminder"] is True


def test_advisor_can_recommend_active_product_to_bound_member(qtbot) -> None:
    commerce = FakeCommerceService()
    workspace = AdvisorWorkspace(
        FakeServiceManagement(), backup_service=None, commerce_service=commerce
    )
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=23, username="advisor-wang"))

    assert workspace.tabs.tabText(4) == "商品推荐"
    assert "家用保温杯" in workspace.product_list.item(0).text()
    assert workspace.recommend_member_combo.currentData() == 7
    assert "不构成医疗建议" in " ".join(
        label.text()
        for label in workspace.tabs.widget(4).findChildren(type(workspace.status_label))
    )

    workspace.product_list.setCurrentRow(0)
    workspace.recommend_reason_input.setPlainText("便于在客厅随手记录每日饮水量")
    workspace._recommend_product()

    assert commerce.recommended == [(23, 7, 41, "便于在客厅随手记录每日饮水量")]
    assert "没有自动创建订单" in workspace.status_label.text()
    assert "会员感兴趣" in workspace.recommendation_list.item(0).text()


def test_recommendation_list_derives_interest_order_and_delivery_status(qtbot) -> None:
    class StatusCommerceService(FakeCommerceService):
        def list_recommendations(self, actor_user_id: int) -> list[dict[str, object]]:
            assert actor_user_id == 23
            shared = {
                "member_user_id": 7,
                "member_name": "李明",
                "product_name": "家用保温杯",
                "reason": "便于在客厅随手饮水",
                "status": "interested",
                "created_at": "2026-09-05T10:00:00+08:00",
            }
            return [
                {"id": 71, **shared, "order_count": 0, "latest_order_status": None},
                {
                    "id": 72,
                    **shared,
                    "order_count": 1,
                    "latest_order_status": "created",
                },
                {
                    "id": 73,
                    **shared,
                    "order_count": 1,
                    "latest_order_status": "delivered",
                },
            ]

    workspace = AdvisorWorkspace(
        FakeServiceManagement(),
        backup_service=None,
        commerce_service=StatusCommerceService(),
    )
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=23, username="advisor-wang"))

    assert "会员感兴趣" in workspace.recommendation_list.item(0).text()
    assert "会员已创建模拟订单" in workspace.recommendation_list.item(1).text()
    assert "模拟订单已交付" in workspace.recommendation_list.item(2).text()
