from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from ollama_chat_app.ui.health_workspace import HealthWorkspace


class FakeChatPage(QWidget):
    logout_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.started_with = None
        self.ended = False

    def start_session(self, user: object) -> None:
        self.started_with = user

    def end_session(self) -> None:
        self.ended = True


class FakeHealthService:
    def __init__(self) -> None:
        self.saved_profile: tuple[int, dict[str, str]] | None = None
        self.deleted_records: list[tuple[int, int]] = []
        self.reminder_toggles: list[tuple[int, int, bool]] = []

    def get_dashboard(self, user_id: int) -> dict[str, object]:
        assert user_id == 7
        return {
            "facts": [{"label": "居住情况", "value": "与家人同住"}],
            "today_records": [
                {
                    "id": 1,
                    "category": "water",
                    "content": "喝了一杯水",
                    "occurred_at": "2026-09-04T09:10:00",
                }
            ],
            "reminders": [
                {
                    "id": 5,
                    "title": "散步",
                    "scheduled_at": "2026-09-04T18:00:00",
                }
            ],
        }

    def list_life_records(
        self, user_id: int, category: str | None = None
    ) -> list[dict[str, object]]:
        records = [
            {
                "id": 1,
                "category": "water",
                "content": "喝了一杯水",
                "occurred_at": "2026-09-04T09:10:00",
            },
            {
                "id": 2,
                "category": "sleep",
                "content": "昨晚约十点休息",
                "occurred_at": "2026-09-03T22:00:00",
            },
        ]
        return [item for item in records if not category or item["category"] == category]

    def add_life_record(
        self,
        user_id: int,
        category: str,
        content: str,
        occurred_at: str,
    ) -> None:
        del user_id, category, content, occurred_at

    def delete_life_record(self, user_id: int, record_id: int) -> None:
        self.deleted_records.append((user_id, record_id))

    def get_profile(self, user_id: int) -> dict[str, str]:
        assert user_id == 7
        return {
            "display_name": "李明",
            "birth_date": "1960-01-02",
            "gender": "male",
            "phone": "13800000000",
            "living_situation": "与家人同住",
            "emergency_contact_name": "李华",
            "emergency_contact_phone": "13900000000",
            "ai_preferred_name": "李叔叔",
            "reminder_frequency": "normal",
        }

    def save_profile(self, user_id: int, values: dict[str, str]) -> None:
        self.saved_profile = (user_id, values)

    def list_reminders(self, user_id: int) -> list[dict[str, object]]:
        assert user_id == 7
        return [
            {
                "id": 5,
                "title": "散步",
                "scheduled_at": "2026-09-04T18:00:00",
                "enabled": True,
            }
        ]

    def add_reminder(
        self,
        user_id: int,
        title: str,
        scheduled_at: str,
        reminder_type: str = "custom",
    ) -> None:
        del user_id, title, scheduled_at, reminder_type

    def toggle_reminder(self, user_id: int, reminder_id: int, enabled: bool) -> None:
        self.reminder_toggles.append((user_id, reminder_id, enabled))

    def delete_reminder(self, user_id: int, reminder_id: int) -> None:
        del user_id, reminder_id

    def get_service_summary(self, user_id: int) -> dict[str, object]:
        assert user_id == 7
        return {
            "membership": "基础会员｜有效",
            "advisor": "顾问：王老师",
            "next_visit": "2026-09-08 14:00",
        }


class FakeCommerceService:
    def __init__(self) -> None:
        self.views: list[tuple[int, int]] = []
        self.interests: list[tuple[int, int]] = []
        self.product_interests: list[tuple[int, int]] = []
        self.purchases: list[tuple[int, int, int, int | None]] = []
        self.reorders: list[tuple[int, int, int]] = []
        self.orders: list[dict[str, object]] = []
        self.recommendation_status = "new"
        self.recommendation_reads = 0

    def list_products(
        self, user_id: int, include_inactive: bool = False
    ) -> list[dict[str, object]]:
        assert user_id == 7
        assert not include_inactive
        return [
            {
                "id": 11,
                "name": "防滑浴室垫",
                "description": "放在浴室入口，帮助改善湿滑环境。",
                "category": "居家生活",
                "price_cents": 2990,
            }
        ]

    def list_recommendations(self, user_id: int) -> list[dict[str, object]]:
        assert user_id == 7
        self.recommendation_reads += 1
        return [
            {
                "id": 31,
                "product_id": 11,
                "product_name": "防滑浴室垫",
                "reason": "近期记录中提到浴室地面容易湿滑。",
                "description": "放在浴室入口，帮助改善湿滑环境。",
                "category": "居家生活",
                "price_cents": 2990,
                "status": self.recommendation_status,
            }
        ]

    def record_product_view(self, user_id: int, product_id: int) -> None:
        self.views.append((user_id, product_id))
        self.recommendation_status = "viewed"

    def mark_interested(self, user_id: int, recommendation_or_product_id: int) -> None:
        self.interests.append((user_id, recommendation_or_product_id))
        self.recommendation_status = "interested"

    def mark_product_interested(self, user_id: int, product_id: int) -> None:
        self.product_interests.append((user_id, product_id))

    def simulate_purchase(
        self,
        user_id: int,
        product_id: int,
        quantity: int = 1,
        recommendation_id: int | None = None,
    ) -> dict[str, object]:
        self.purchases.append((user_id, product_id, quantity, recommendation_id))
        if recommendation_id is not None:
            self.recommendation_status = "interested"
        order = {
            "id": 81,
            "order_no": "MO-000081",
            "product_name_snapshot": "防滑浴室垫",
            "quantity": quantity,
            "amount_cents": 2990 * quantity,
            "status": "created",
            "created_at": "2026-09-05T10:30:00",
        }
        self.orders.insert(0, order)
        return order

    def reorder(self, user_id: int, order_id: int, quantity: int = 1) -> dict[str, object]:
        self.reorders.append((user_id, order_id, quantity))
        order = {
            "id": 82,
            "order_no": "MO-000082",
            "product_name_snapshot": "防滑浴室垫",
            "quantity": quantity,
            "amount_cents": 2990 * quantity,
            "status": "created",
            "reordered_from_order_id": order_id,
            "created_at": "2026-09-05T10:40:00",
        }
        self.orders.insert(0, order)
        return order

    def list_orders(self, user_id: int) -> list[dict[str, object]]:
        assert user_id == 7
        return self.orders


def test_workspace_starts_member_session_and_embeds_chat(qtbot) -> None:
    service = FakeHealthService()
    chat_page = FakeChatPage()
    workspace = HealthWorkspace(service, chat_page, backup_service=object())
    qtbot.addWidget(workspace)
    user = SimpleNamespace(id=7, username="alice")

    workspace.start_session(user)

    assert workspace.stack.count() == 5
    assert workspace.stack.currentWidget() is workspace.today_page
    assert workspace.navigation_buttons[0].isChecked()
    assert workspace.stack.indexOf(chat_page) == 4
    assert chat_page.started_with is user
    assert "alice" in workspace.user_label.text()
    assert "与家人同住" in workspace.today_page.facts_label.text()
    assert "喝了一杯水" in workspace.today_page.records_label.text()
    assert workspace.export_button.isEnabled()
    assert workspace.import_button.isEnabled()

    workspace.show_page(4)
    assert workspace.stack.currentWidget() is chat_page

    workspace.end_session()
    assert chat_page.ended
    assert workspace.current_user is None


def test_profile_save_and_reminder_toggle_use_health_service(qtbot) -> None:
    service = FakeHealthService()
    workspace = HealthWorkspace(service, FakeChatPage())
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=7, username="alice"))

    workspace.profile_page.full_name_input.setText("李明（已核对）")
    workspace.profile_page.save()
    assert service.saved_profile is not None
    assert service.saved_profile[0] == 7
    assert service.saved_profile[1]["display_name"] == "李明（已核对）"
    assert "已保存" in workspace.profile_page.status_label.text()

    workspace.service_page.reminder_list.setCurrentRow(0)
    workspace.service_page._toggle_selected()
    assert service.reminder_toggles == [(7, 5, False)]
    assert "散步" in workspace.service_page.reminder_list.item(0).text()


def test_member_commerce_ui_records_interest_and_simulated_order(qtbot, monkeypatch) -> None:
    service = FakeHealthService()
    commerce = FakeCommerceService()
    workspace = HealthWorkspace(
        service,
        FakeChatPage(),
        commerce_service=commerce,
    )
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=7, username="alice"))
    panel = workspace.service_page.commerce_panel

    assert workspace.stack.count() == 5
    assert workspace.navigation_buttons[3].text() == "服务"
    assert workspace.service_page.section_tabs.count() == 3
    assert workspace.service_page.section_tabs.tabText(2) == "文件与健康报告"
    assert "[新推荐]" in panel.recommendation_list.item(0).text()
    assert "防滑浴室垫" in panel.recommendation_list.item(0).text()
    assert "防滑浴室垫" in panel.product_list.item(0).text()
    assert "还没有模拟订单" in panel.order_list.item(0).text()

    panel._select_item(panel.recommendation_list.item(0))
    assert commerce.views == [(7, 31)]
    assert "[已查看]" in panel.recommendation_list.item(0).text()
    assert panel.recommendation_list.currentItem() is not None
    assert "不会付款" in panel.notice_label.text()
    assert "近期记录中提到" in panel.detail_label.text()
    assert panel.purchase_button.isEnabled()

    confirmation_texts: list[str] = []

    def accept_confirmation(*args, **kwargs) -> QMessageBox.StandardButton:
        del kwargs
        confirmation_texts.append(str(args[2]))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(
        QMessageBox,
        "question",
        accept_confirmation,
    )
    reads_before_purchase = commerce.recommendation_reads
    panel.quantity_input.setValue(2)
    panel._simulate_purchase()

    assert commerce.purchases == [(7, 11, 2, 31)]
    assert commerce.recommendation_reads == reads_before_purchase + 1
    assert "[已感兴趣]" in panel.recommendation_list.item(0).text()
    assert panel.recommendation_list.currentItem() is not None
    assert "MO-000081" in panel.order_list.item(0).text()
    assert "已创建" in panel.order_list.item(0).text()
    assert "无真实物流" in panel.order_list.item(0).text()
    assert "没有发生真实付款" in panel.status_label.text()
    assert panel.order_list.currentItem() is not None
    assert panel.reorder_button.isEnabled()

    reads_before_interest = commerce.recommendation_reads
    panel._mark_interested()
    assert commerce.interests == [(7, 31)]
    assert commerce.recommendation_reads == reads_before_interest + 1
    assert "[已感兴趣]" in panel.recommendation_list.item(0).text()
    assert "不会自动购买" in panel.status_label.text()

    reads_before_reorder = commerce.recommendation_reads
    panel.reorder_quantity_input.setValue(3)
    panel._reorder_selected()
    assert commerce.reorders == [(7, 81, 3)]
    assert commerce.recommendation_reads == reads_before_reorder + 1
    assert "MO-000082" in panel.order_list.item(0).text()
    assert "再次购买自：MO-000081" in panel.order_list.item(0).text()
    assert "已创建" in panel.order_list.item(0).text()
    assert panel.order_list.currentItem() is panel.order_list.item(0)
    assert "没有发生真实付款、扣款或发货" in panel.status_label.text()
    assert len(confirmation_texts) == 2
    for text in confirmation_texts:
        assert "付款" in text
        assert "扣款" in text
        assert "发货" in text

    panel._select_item(panel.product_list.item(0))
    panel._mark_interested()
    assert commerce.interests == [(7, 31)]
    assert commerce.product_interests == [(7, 11)]
    assert commerce.views == [(7, 31)]


def test_member_commerce_ui_is_safe_when_service_is_not_connected(qtbot) -> None:
    workspace = HealthWorkspace(FakeHealthService(), FakeChatPage())
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=7, username="alice"))

    panel = workspace.service_page.commerce_panel
    assert "尚未接入" in panel.status_label.text()
    assert not panel.purchase_button.isEnabled()
    assert "当前没有推荐商品" in panel.recommendation_list.item(0).text()
