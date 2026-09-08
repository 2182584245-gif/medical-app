from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ollama_chat_app.data.database import SCHEMA_VERSION, Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.commerce import (
    CommercePermissionDeniedError,
    CommerceRecordNotFoundError,
    CommerceService,
    CommerceValidationError,
    InvalidOrderStateError,
)
from ollama_chat_app.services.service_management import ServiceManagementService


@pytest.fixture
def commerce_context(tmp_path: Path) -> dict[str, object]:
    database = Database(tmp_path / "app.db")
    database.initialize()
    auth = AuthService(database)
    management = ServiceManagementService(database)
    operator = auth.bootstrap_operator("operations", "operator-password-123")
    advisor_one = management.create_advisor(
        operator.id,
        "advisor-one",
        "advisor-one-password-123",
        display_name="王顾问",
    )
    advisor_two = management.create_advisor(
        operator.id,
        "advisor-two",
        "advisor-two-password-123",
        display_name="李顾问",
    )
    alice = auth.register("alice", "alice-member-password-123")
    bob = auth.register("bob", "bob-member-password-123")
    management.bind_advisor(operator.id, alice.id, advisor_one.id)
    management.bind_advisor(operator.id, bob.id, advisor_two.id)
    return {
        "database": database,
        "auth": auth,
        "management": management,
        "commerce": CommerceService(database),
        "operator": operator,
        "advisor_one": advisor_one,
        "advisor_two": advisor_two,
        "alice": alice,
        "bob": bob,
    }


def _create_product(context: dict[str, object], *, active: bool = True) -> dict[str, object]:
    commerce = context["commerce"]
    operator = context["operator"]
    return commerce.create_product(  # type: ignore[union-attr]
        operator.id,  # type: ignore[union-attr]
        sku="LIFE-CUP-001",
        name="易握水杯",
        category="饮水用品",
        brand="本地示例",
        specification="350 毫升",
        price_cents=1299,
        unit="个",
        image_path="images/products/cup.png",
        description="带防滑握柄的日常水杯",
        source_type="self_operated",
        is_active=active,
    )


def test_v2_to_current_migration_is_additive_transactional_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v2.db"
    created_at = "2026-01-02T03:04:05.000000Z"
    with sqlite3.connect(path) as connection:
        Database._create_schema_v2(connection)
        connection.execute(
            """
            INSERT INTO users (
                id, username, username_normalized, password_hash, created_at,
                last_login_at, role_code, account_status, updated_at
            ) VALUES (7, '旧会员', '旧会员', 'legacy-hash', ?, NULL,
                      'member', 'active', ?)
            """,
            (created_at, created_at),
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    database = Database(path)
    database.initialize()
    database.initialize()

    with database.connect() as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index'"
            ).fetchall()
        }
        user = connection.execute("SELECT username FROM users WHERE id = 7").fetchone()
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == SCHEMA_VERSION == 6
    assert {"products", "product_recommendations", "orders", "chat_attachments"} <= tables
    assert {
        "idx_products_active_category",
        "idx_recommendations_member_status",
        "idx_orders_member_date",
    } <= indexes
    assert str(user["username"]) == "旧会员"
    assert foreign_key_issues == []


def test_operator_product_crud_visibility_integer_price_and_audit(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    operator = commerce_context["operator"]
    advisor = commerce_context["advisor_one"]
    alice = commerce_context["alice"]

    product = _create_product(commerce_context, active=False)
    assert product["price_cents"] == product["unit_price_cents"] == 1299
    assert product["is_active"] is False
    assert commerce.list_products(alice.id) == []  # type: ignore[union-attr]
    assert [item["id"] for item in commerce.list_products(operator.id, include_inactive=True)] == [  # type: ignore[union-attr]
        product["id"]
    ]

    with pytest.raises(CommercePermissionDeniedError):
        commerce.list_products(advisor.id, include_inactive=True)  # type: ignore[union-attr]
    with pytest.raises(CommercePermissionDeniedError):
        commerce.create_product(  # type: ignore[union-attr]
            advisor.id,
            sku="DENIED",
            name="不会创建",
            category="测试",
            price_cents=1,
        )
    with pytest.raises(CommerceValidationError, match="整数分"):
        commerce.update_product(operator.id, product["id"], price_cents=12.5)  # type: ignore[union-attr]

    updated = commerce.update_product(  # type: ignore[union-attr]
        operator.id,
        product["id"],
        brand="新品牌",
        price_cents=1599,
    )
    assert updated["brand"] == "新品牌"
    assert updated["price_cents"] == 1599
    published = commerce.set_product_active(operator.id, product["id"], True)  # type: ignore[union-attr]
    assert published["is_active"] is True
    assert commerce.list_products(alice.id)[0]["id"] == product["id"]  # type: ignore[union-attr]

    with pytest.raises(CommerceValidationError, match="先下架"):
        commerce.delete_product(operator.id, product["id"])  # type: ignore[union-attr]
    commerce.set_product_active(operator.id, product["id"], False)  # type: ignore[union-attr]
    commerce.delete_product(operator.id, product["id"])  # type: ignore[union-attr]
    with pytest.raises(CommerceRecordNotFoundError):
        commerce.set_product_active(operator.id, product["id"], True)  # type: ignore[union-attr]

    database = commerce_context["database"]
    with database.connect() as connection:  # type: ignore[union-attr]
        actions = {
            str(row[0])
            for row in connection.execute(
                "SELECT action FROM audit_logs WHERE entity_type = 'product'"
            )
        }
    assert {
        "product.created",
        "product.updated",
        "product.activated",
        "product.deactivated",
        "product.deleted",
    } <= actions


@pytest.mark.parametrize(
    "reason",
    [
        "这个杯子能够治疗高血压",
        "必须马上购买，否则错过就危险了",
        "Guaranteed cure for disease",
    ],
)
def test_recommendation_rejects_medical_claims_and_pressure_language(
    commerce_context: dict[str, object], reason: str
) -> None:
    commerce = commerce_context["commerce"]
    advisor = commerce_context["advisor_one"]
    alice = commerce_context["alice"]
    product = _create_product(commerce_context)

    with pytest.raises(CommerceValidationError, match="医疗疗效承诺或制造焦虑"):
        commerce.recommend_product(advisor.id, alice.id, product["id"], reason)  # type: ignore[union-attr]

    database = commerce_context["database"]
    with database.connect() as connection:  # type: ignore[union-attr]
        assert connection.execute("SELECT COUNT(*) FROM product_recommendations").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("name", "治疗高血压水杯"),
        ("description", "保证有效，不买会后悔"),
        ("specification", "替代用药特别版"),
    ],
)
def test_product_copy_rejects_medical_claims_and_pressure_language(
    commerce_context: dict[str, object], field: str, unsafe_value: str
) -> None:
    commerce = commerce_context["commerce"]
    operator = commerce_context["operator"]
    values: dict[str, object] = {
        "sku": f"UNSAFE-{field.upper()}",
        "name": "日常水杯",
        "category": "日常用品",
        "description": "用于日常饮水",
        "price_cents": 100,
        "is_active": True,
    }
    values[field] = unsafe_value

    with pytest.raises(CommerceValidationError, match="医疗疗效承诺或制造焦虑"):
        commerce.create_product(operator.id, **values)  # type: ignore[union-attr]


def test_product_update_rejects_unsafe_copy_without_partial_change(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    operator = commerce_context["operator"]
    product = _create_product(commerce_context)

    with pytest.raises(CommerceValidationError, match="医疗疗效承诺或制造焦虑"):
        commerce.update_product(  # type: ignore[union-attr]
            operator.id,
            product["id"],
            name="包治百病水杯",
            description="不买就会错过最后机会",
        )

    unchanged = commerce.list_products(operator.id, include_inactive=True)[0]  # type: ignore[union-attr]
    assert unchanged["name"] == "易握水杯"
    assert unchanged["description"] == "带防滑握柄的日常水杯"


def test_interest_methods_do_not_guess_between_product_and_recommendation_ids(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    operator = commerce_context["operator"]
    advisor = commerce_context["advisor_one"]
    alice = commerce_context["alice"]
    first_product = _create_product(commerce_context)
    recommendation = commerce.recommend_product(  # type: ignore[union-attr]
        advisor.id, alice.id, first_product["id"], "日常在餐桌旁使用"
    )
    second_product = commerce.create_product(  # type: ignore[union-attr]
        operator.id,
        sku="LIFE-LAMP-002",
        name="床边小夜灯",
        category="居家照明",
        price_cents=2399,
        is_active=True,
    )
    assert recommendation["id"] == 1
    assert second_product["id"] == 2

    with pytest.raises(CommercePermissionDeniedError):
        commerce.mark_interested(alice.id, second_product["id"])  # type: ignore[union-attr]

    catalogue_interest = commerce.mark_product_interested(  # type: ignore[union-attr]
        alice.id, second_product["id"]
    )
    assert catalogue_interest["product_id"] == second_product["id"]
    assert catalogue_interest["recommendation_source"] == "catalogue"
    assert commerce.list_recommendations(alice.id)[-1]["status"] == "new"  # type: ignore[union-attr]


def test_advisor_can_only_recommend_active_product_to_current_bound_member(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    advisor_one = commerce_context["advisor_one"]
    advisor_two = commerce_context["advisor_two"]
    alice = commerce_context["alice"]
    bob = commerce_context["bob"]
    operator = commerce_context["operator"]
    inactive = _create_product(commerce_context, active=False)

    with pytest.raises(CommerceValidationError, match="上架"):
        commerce.recommend_product(  # type: ignore[union-attr]
            advisor_one.id, alice.id, inactive["id"], "会员日常喝水时更容易握持"
        )
    commerce.set_product_active(operator.id, inactive["id"], True)  # type: ignore[union-attr]
    with pytest.raises(CommercePermissionDeniedError):
        commerce.recommend_product(  # type: ignore[union-attr]
            advisor_one.id, bob.id, inactive["id"], "会员平时在餐桌旁使用"
        )
    with pytest.raises(CommercePermissionDeniedError):
        commerce.recommend_product(  # type: ignore[union-attr]
            alice.id, alice.id, inactive["id"], "会员本人从目录看到"
        )

    recommendation = commerce.recommend_product(  # type: ignore[union-attr]
        advisor_one.id,
        alice.id,
        inactive["id"],
        "会员日常喝水时更容易握持，适合放在餐桌旁",
    )
    assert recommendation["status"] == "new"
    assert [item["id"] for item in commerce.list_recommendations(alice.id)] == [  # type: ignore[union-attr]
        recommendation["id"]
    ]
    assert commerce.list_recommendations(bob.id) == []  # type: ignore[union-attr]
    assert [
        item["id"]
        for item in commerce.list_recommendations(advisor_one.id)  # type: ignore[union-attr]
    ] == [recommendation["id"]]
    assert commerce.list_recommendations(advisor_two.id) == []  # type: ignore[union-attr]


def test_member_view_interest_catalogue_interest_and_ownership_are_persisted(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    advisor = commerce_context["advisor_one"]
    alice = commerce_context["alice"]
    bob = commerce_context["bob"]
    product = _create_product(commerce_context)
    recommendation = commerce.recommend_product(  # type: ignore[union-attr]
        advisor.id, alice.id, product["id"], "会员常在书桌边喝水，带握柄方便拿取"
    )

    viewed = commerce.record_product_view(alice.id, recommendation["id"])  # type: ignore[union-attr]
    assert viewed["status"] == "viewed"
    assert viewed["viewed_at"] is not None
    interested = commerce.mark_interested(alice.id, recommendation["id"])  # type: ignore[union-attr]
    assert interested["status"] == "interested"
    assert interested["interested_at"] is not None
    with pytest.raises(CommercePermissionDeniedError):
        commerce.record_product_view(bob.id, recommendation["id"])  # type: ignore[union-attr]

    second = commerce.create_product(  # type: ignore[union-attr]
        commerce_context["operator"].id,  # type: ignore[union-attr]
        sku="LIFE-LAMP-002",
        name="床边小夜灯",
        category="居家照明",
        price_cents=2399,
        is_active=True,
    )
    catalogue_interest = commerce.mark_product_interested(alice.id, second["id"])  # type: ignore[union-attr]
    repeated_interest = commerce.mark_product_interested(alice.id, second["id"])  # type: ignore[union-attr]
    assert catalogue_interest["recommendation_source"] == "catalogue"
    assert catalogue_interest["advisor_user_id"] is None
    assert catalogue_interest["status"] == "interested"
    assert repeated_interest["id"] == catalogue_interest["id"]


def test_simulated_order_uses_price_snapshot_reorders_at_current_price_and_delivers(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    operator = commerce_context["operator"]
    advisor = commerce_context["advisor_one"]
    alice = commerce_context["alice"]
    bob = commerce_context["bob"]
    product = _create_product(commerce_context)
    recommendation = commerce.recommend_product(  # type: ignore[union-attr]
        advisor.id, alice.id, product["id"], "会员日常饮水使用，握柄便于拿取"
    )

    first = commerce.simulate_purchase(  # type: ignore[union-attr]
        alice.id, product["id"], quantity=3, recommendation_id=recommendation["id"]
    )
    assert first["status"] == "created"
    assert first["currency"] == "CNY"
    assert first["unit_price_cents"] == 1299
    assert first["total_amount_cents"] == 3897
    assert first["order_no"].startswith("SIM-")
    recommendation_after_order = commerce.list_recommendations(alice.id)[0]  # type: ignore[union-attr]
    assert recommendation_after_order["status"] == "interested"
    assert recommendation_after_order["order_count"] == 1
    assert recommendation_after_order["latest_order_status"] == "created"

    commerce.update_product(operator.id, product["id"], price_cents=1499)  # type: ignore[union-attr]
    repeated = commerce.reorder(alice.id, first["id"], quantity=2)  # type: ignore[union-attr]
    assert repeated["reordered_from_order_id"] == first["id"]
    assert repeated["unit_price_cents"] == 1499
    assert repeated["total_amount_cents"] == 2998
    assert commerce.list_orders(alice.id)[-1]["unit_price_cents"] == 1299  # type: ignore[union-attr]

    with pytest.raises(CommercePermissionDeniedError):
        commerce.list_orders(advisor.id)  # type: ignore[union-attr]
    with pytest.raises(CommercePermissionDeniedError):
        commerce.reorder(bob.id, first["id"])  # type: ignore[union-attr]
    with pytest.raises(CommercePermissionDeniedError):
        commerce.mark_order_delivered(alice.id, first["id"])  # type: ignore[union-attr]

    delivered = commerce.mark_order_delivered(operator.id, first["id"])  # type: ignore[union-attr]
    assert delivered["status"] == "delivered"
    assert delivered["delivered_at"] is not None
    recommendation_after_delivery = commerce.list_recommendations(advisor.id)[0]  # type: ignore[union-attr]
    assert recommendation_after_delivery["order_count"] == 1
    assert recommendation_after_delivery["latest_order_status"] == "delivered"
    with pytest.raises(InvalidOrderStateError):
        commerce.mark_order_delivered(operator.id, first["id"])  # type: ignore[union-attr]

    database = commerce_context["database"]
    with database.connect() as connection:  # type: ignore[union-attr]
        audit_rows = connection.execute(
            """
            SELECT action, details_json FROM audit_logs
            WHERE entity_type IN ('order', 'product_recommendation')
            ORDER BY id
            """
        ).fetchall()
    actions = [str(row["action"]) for row in audit_rows]
    assert actions.count("order.created") == 2
    assert "order.delivered" in actions
    first_order_details = next(
        json.loads(str(row["details_json"]))
        for row in audit_rows
        if str(row["action"]) == "order.created"
    )
    assert first_order_details["total_amount_cents"] == 3897


def test_invalid_recommendation_cannot_leave_partial_order(
    commerce_context: dict[str, object],
) -> None:
    commerce = commerce_context["commerce"]
    advisor_two = commerce_context["advisor_two"]
    alice = commerce_context["alice"]
    bob = commerce_context["bob"]
    product = _create_product(commerce_context)
    bobs_recommendation = commerce.recommend_product(  # type: ignore[union-attr]
        advisor_two.id, bob.id, product["id"], "会员在餐桌旁日常使用"
    )

    with pytest.raises(CommercePermissionDeniedError):
        commerce.simulate_purchase(  # type: ignore[union-attr]
            alice.id,
            product["id"],
            quantity=2,
            recommendation_id=bobs_recommendation["id"],
        )

    database = commerce_context["database"]
    with database.connect() as connection:  # type: ignore[union-attr]
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT status FROM product_recommendations WHERE id = ?",
                (bobs_recommendation["id"],),
            ).fetchone()[0]
            == "new"
        )


def test_schema_enforces_money_status_and_foreign_key_constraints(tmp_path: Path) -> None:
    database = Database(tmp_path / "constraints.db")
    database.initialize()
    created_at = "2026-01-02T03:04:05.000000Z"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO products (
                sku, name, category, price_cents, source_type, is_active,
                created_at, updated_at
            ) VALUES ('VALID', '商品', '类别', 1, 'self_operated', 1, ?, ?)
            """,
            (created_at, created_at),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO products (
                    sku, name, category, price_cents, source_type, is_active,
                    created_at, updated_at
                ) VALUES ('NEGATIVE', '商品', '类别', -1, 'self_operated', 1, ?, ?)
                """,
                (created_at, created_at),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO product_recommendations (
                    product_id, member_user_id, advisor_user_id, reason,
                    recommendation_source, status, created_at, updated_at
                ) VALUES (999, 999, 999, '无效外键', 'advisor', 'new', ?, ?)
                """,
                (created_at, created_at),
            )
