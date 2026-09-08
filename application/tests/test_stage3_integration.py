from __future__ import annotations

import os
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from ollama_chat_app.data.database import SCHEMA_VERSION, Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.backup import PortableBackupService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.commerce import CommerceService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService
from ollama_chat_app.ui.advisor_workspace import AdvisorWorkspace
from ollama_chat_app.ui.main_window import MainWindow
from ollama_chat_app.ui.operator_workspace import OperatorWorkspace


def _create_stage3_records(database: Database) -> dict[str, object]:
    auth = AuthService(database)
    management = ServiceManagementService(database)
    commerce = CommerceService(database)
    operator = auth.bootstrap_operator("operator", "operator-password-123")
    advisor = management.create_advisor(
        operator.id,
        "advisor",
        "advisor-password-123",
        display_name="王顾问",
    )
    member = auth.register("member", "member-password-123")
    management.bind_advisor(operator.id, member.id, advisor.id)

    product = commerce.create_product(
        operator.id,
        sku="LIFE-CUP-001",
        name="易握水杯",
        category="日常生活用品",
        brand="本地示例",
        specification="350 毫升",
        price_cents=1299,
        unit="个",
        description="带防滑握柄，适合放在日常饮水位置",
        source_type="self_operated",
        is_active=False,
    )
    commerce.set_product_active(operator.id, int(product["id"]), True)
    recommendation = commerce.recommend_product(
        advisor.id,
        member.id,
        int(product["id"]),
        "会员常在餐桌边喝水，握柄便于日常拿取",
    )
    commerce.record_product_view(member.id, int(recommendation["id"]))
    commerce.mark_interested(member.id, int(recommendation["id"]))
    first_order = commerce.simulate_purchase(
        member.id,
        int(product["id"]),
        quantity=2,
        recommendation_id=int(recommendation["id"]),
    )
    commerce.mark_order_delivered(operator.id, int(first_order["id"]))
    second_order = commerce.reorder(member.id, int(first_order["id"]), quantity=1)
    return {
        "operator": operator,
        "advisor": advisor,
        "member": member,
        "product": product,
        "recommendation": recommendation,
        "first_order": first_order,
        "second_order": second_order,
    }


def test_stage3_full_path_persists_through_data_export_and_import(tmp_path: Path) -> None:
    source = Database(tmp_path / "source" / "app.db")
    records = _create_stage3_records(source)
    archive = tmp_path / "stage3-data.zip"
    PortableBackupService(source).create_archive(archive)

    destination = Database(tmp_path / "destination" / "app.db")
    destination.initialize()
    PortableBackupService(destination).import_archive(archive)

    auth = AuthService(destination)
    member = auth.authenticate("member", "member-password-123")
    advisor = auth.authenticate("advisor", "advisor-password-123")
    operator = auth.authenticate("operator", "operator-password-123")
    commerce = CommerceService(destination)

    products = commerce.list_products(member.id)
    recommendations = commerce.list_recommendations(advisor.id)
    member_orders = commerce.list_orders(member.id)
    operator_orders = commerce.list_orders(operator.id)

    assert [product["sku"] for product in products] == ["LIFE-CUP-001"]
    assert recommendations[0]["status"] == "interested"
    assert recommendations[0]["order_count"] == 1
    assert recommendations[0]["latest_order_status"] == "delivered"
    assert len(member_orders) == len(operator_orders) == 2
    assert member_orders[0]["id"] == records["second_order"]["id"]  # type: ignore[index]
    assert member_orders[1]["status"] == "delivered"
    assert member_orders[1]["total_amount_cents"] == 2598
    assert member_orders[0]["reordered_from_order_id"] == records["first_order"]["id"]  # type: ignore[index]

    with destination.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        actions = {
            str(row[0])
            for row in connection.execute(
                "SELECT action FROM audit_logs WHERE entity_type IN "
                "('product', 'product_recommendation', 'order')"
            )
        }
    assert {
        "product.created",
        "product.activated",
        "product_recommendation.created",
        "product_recommendation.viewed",
        "product_recommendation.interested",
        "order.created",
        "order.delivered",
    } <= actions


def test_main_window_injects_one_commerce_service_into_all_role_workspaces(
    tmp_path: Path, qtbot
) -> None:
    database = Database(tmp_path / "app.db")
    records = _create_stage3_records(database)
    commerce = CommerceService(database)
    management = ServiceManagementService(database)
    window = MainWindow(
        auth_service=AuthService(database),
        chat_service=ChatService(database),
        secret_store=SecretStore(),
        backup_service=PortableBackupService(database),
        health_service=HealthService(database),
        service_management_service=management,
        commerce_service=commerce,
    )
    qtbot.addWidget(window)

    assert isinstance(window.advisor_workspace, AdvisorWorkspace)
    assert isinstance(window.operator_workspace, OperatorWorkspace)
    assert window.advisor_workspace.commerce is commerce
    assert window.operator_workspace.commerce is commerce
    assert window.health_workspace is not None
    assert window.health_workspace.service_page.commerce_panel.commerce_service is commerce

    member = records["member"]
    window._login_succeeded(member)
    commerce_panel = window.health_workspace.service_page.commerce_panel
    assert commerce_panel.product_list.count() == 1
    assert commerce_panel.recommendation_list.count() == 1
    # The new service page is cart/favorites-focused; historical order data is preserved.
    assert len(commerce.list_orders(member.id)) == 2
    window._logout()

    advisor = records["advisor"]
    window._login_succeeded(advisor)
    assert window.advisor_workspace.product_list.count() == 1
    assert window.advisor_workspace.recommendation_list.count() == 1
    window._logout()

    operator = records["operator"]
    window._login_succeeded(operator)
    assert window.operator_workspace.product_list.count() == 1
    assert window.operator_workspace.order_list.count() == 2
