from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton, QSpinBox, QWidget

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
        self, user_id: int, category: str | None = None, **_filters
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
        self.cart_quantity = 0
        self.favorite = False

    def list_cart(self, user_id):
        return (
            [
                {
                    **self.list_products(user_id)[0],
                    "quantity": self.cart_quantity,
                    "total_amount_cents": self.cart_quantity * 2990,
                }
            ]
            if self.cart_quantity
            else []
        )

    def list_favorites(self, user_id):
        return self.list_products(user_id) if self.favorite else []

    def add_to_cart(self, user_id, product_id):
        assert user_id == 7 and product_id == 11
        self.cart_quantity += 1

    def set_cart_quantity(self, user_id, product_id, quantity):
        assert user_id == 7 and product_id == 11
        self.cart_quantity = quantity

    def set_favorite(self, user_id, product_id, favorite):
        assert user_id == 7 and product_id == 11
        self.favorite = favorite

    def checkout_cart(self, user_id):
        assert user_id == 7 and self.cart_quantity > 0
        result = self.simulate_purchase(user_id, 11, self.cart_quantity)
        self.cart_quantity = 0
        return [result]

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
    assert workspace.stack.indexOf(chat_page) == 3
    assert chat_page.started_with is user
    assert "alice" in workspace.user_label.text()
    assert workspace.today_page.user_id == 7
    assert workspace.profile_editor.full_name_input.text() == "李明"
    assert not hasattr(workspace, "export_button")
    assert workspace.import_button.isEnabled()

    workspace.show_page(3)
    assert workspace.stack.currentWidget() is chat_page

    workspace.end_session()
    assert chat_page.ended
    assert workspace.current_user is None


def test_profile_save_and_reminder_toggle_use_health_service(qtbot) -> None:
    service = FakeHealthService()
    workspace = HealthWorkspace(service, FakeChatPage())
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=7, username="alice"))

    workspace.profile_editor.full_name_input.setText("李明（已核对）")
    workspace.profile_editor.save()
    assert service.saved_profile is not None
    assert service.saved_profile[0] == 7
    assert service.saved_profile[1]["display_name"] == "李明（已核对）"
    assert "已保存" in workspace.profile_editor.status_label.text()

    # Reminder controls now live in Today; inject the fixture's chosen day row.
    workspace.today_page._fill(
        workspace.today_page.reminders_table, service.list_reminders(7), record=False
    )
    workspace.today_page.reminders_table.selectRow(0)
    workspace.today_page.toggle_reminder()
    assert service.reminder_toggles == [(7, 5, False)]


def test_member_commerce_ui_cart_favorites_and_confirmed_virtual_order(qtbot, monkeypatch) -> None:
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
    assert workspace.navigation_buttons[2].text() == "服务"
    assert workspace.service_page.section_tabs.count() == 2
    assert workspace.service_page.section_tabs.tabText(1) == "商品"
    row = panel.recommendation_list.itemWidget(panel.recommendation_list.item(0))
    assert any(button.text() == "防滑浴室垫" for button in row.findChildren(QPushButton))
    assert any("近期记录中提到" in label.text() for label in row.findChildren(QLabel))
    assert any("不付款、不发货" in label.text() for label in panel.findChildren(QLabel))
    panel._toggle_favorite(11)
    assert commerce.favorite and panel.favorites == {11}
    panel._toggle_favorite(11)
    assert not commerce.favorite and not panel.favorites
    panel._add_cart(11)
    assert commerce.cart_quantity == 1 and "1 件" in panel.cart_button.text()
    assert commerce.orders == []  # Browsing, favorite and cart actions never place an order.

    confirmations, completed = [], []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kw: confirmations.append(_args[2]) or QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_args, **_kw: completed.append(_args[2])
    )

    def interact(dialog):
        assert "本地虚拟购买" in dialog.windowTitle()
        quantity = dialog.findChild(QSpinBox)
        quantity.setValue(2)
        quantity.editingFinished.emit()
        next(
            button for button in dialog.findChildren(QPushButton) if button.text() == "确认虚拟购买"
        ).click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", interact)
    panel.show_cart()
    assert commerce.purchases == [(7, 11, 2, None)]
    assert len(commerce.orders) == 1 and commerce.cart_quantity == 0
    assert len(confirmations) == 1 and "虚拟订单" in confirmations[0]
    assert len(completed) == 1 and "没有实际扣款" in completed[0]


def test_member_commerce_ui_is_safe_when_service_is_not_connected(qtbot) -> None:
    workspace = HealthWorkspace(FakeHealthService(), FakeChatPage())
    qtbot.addWidget(workspace)
    workspace.start_session(SimpleNamespace(id=7, username="alice"))

    panel = workspace.service_page.commerce_panel
    assert "尚未连接" in panel.status_label.text()
    assert "暂无顾问推荐" in panel.recommendation_list.item(0).text()
    assert panel.products == panel.recommendations == panel.cart == []
    assert panel.favorites == set()
