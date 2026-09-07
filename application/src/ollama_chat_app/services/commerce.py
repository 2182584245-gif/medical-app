from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..data.database import Database, timestamp_from_db, timestamp_to_db, utc_now

ROLE_MEMBER = "member"
ROLE_ADVISOR = "advisor"
ROLE_OPERATOR = "operator"
ACCOUNT_ACTIVE = "active"
SOURCE_TYPES = frozenset({"self_operated", "third_party"})
RECOMMENDATION_STATUSES = frozenset({"new", "viewed", "interested"})

_UNSET = object()
_SKU_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MEDICAL_OR_PRESSURE_PHRASES = (
    "治疗",
    "治愈",
    "疗效",
    "药效",
    "根治",
    "包治",
    "抗癌",
    "防癌",
    "预防疾病",
    "降血压",
    "降血糖",
    "替代药",
    "替代用药",
    "替代就医",
    "保证有效",
    "不买就",
    "不买会",
    "再不买",
    "必须购买",
    "必须马上",
    "赶紧购买",
    "最后机会",
    "马上抢",
    "错过就",
    "仅剩",
    "guaranteed cure",
    "medical effect",
    "treat disease",
    "prevent disease",
    "cure disease",
)


class CommerceError(RuntimeError):
    """Base error for the local product and simulated-order workflow."""


class CommercePermissionDeniedError(CommerceError):
    """Raised when an inactive or incorrectly scoped actor requests an operation."""

    def __init__(self) -> None:
        super().__init__("无权执行此操作")


class CommerceValidationError(CommerceError, ValueError):
    """Raised when product, recommendation or order input is invalid."""


class CommerceRecordNotFoundError(CommerceError, LookupError):
    """Raised when a privileged actor requests a missing commerce record."""


class InvalidOrderStateError(CommerceError):
    """Raised when a simulated order cannot make the requested transition."""


class CommerceService:
    """Role-scoped local commerce simulation without payment or shipping.

    UI visibility is not authorization: every public operation reloads the
    actor, target and binding in its own connection or immediate transaction.
    Product and order money is always represented as integer CNY cents.
    """

    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()
        self.database.initialize()

    def list_products(
        self, actor_user_id: int, *, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        """List published products, or the complete catalogue for an operator."""

        if not isinstance(include_inactive, bool):
            raise CommerceValidationError("商品显示范围格式无效")
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            if include_inactive and str(actor["role_code"]) != ROLE_OPERATOR:
                raise CommercePermissionDeniedError
            where = "" if include_inactive else "WHERE p.is_active = 1"
            rows = connection.execute(
                f"""
                SELECT p.*
                FROM products p
                {where}
                ORDER BY p.is_active DESC, p.category, p.name, p.id
                """
            ).fetchall()
        return [self._product_from_row(row) for row in rows]

    def create_product(
        self,
        actor_user_id: int,
        *,
        sku: str,
        name: str,
        category: str,
        brand: str | None = None,
        specification: str | None = None,
        price_cents: Any = _UNSET,
        unit_price_cents: Any = _UNSET,
        unit: str | None = "件",
        image_path: str | None = None,
        description: str | None = None,
        source_type: str = "self_operated",
        is_active: bool = False,
    ) -> dict[str, Any]:
        """Create one catalogue product as an active operator."""

        values = self._product_values(
            sku=sku,
            name=name,
            category=category,
            brand=brand,
            specification=specification,
            price_cents=self._resolve_price(price_cents, unit_price_cents),
            unit=unit,
            image_path=image_path,
            description=description,
            source_type=source_type,
            is_active=is_active,
        )
        now = timestamp_to_db(utc_now())
        try:
            with self.database.transaction() as connection:
                actor = self._require_operator(connection, actor_user_id)
                cursor = connection.execute(
                    """
                    INSERT INTO products (
                        sku, name, category, brand, specification, price_cents, unit,
                        image_path, description, source_type, is_active,
                        created_by_user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["sku"],
                        values["name"],
                        values["category"],
                        values["brand"],
                        values["specification"],
                        values["price_cents"],
                        values["unit"],
                        values["image_path"],
                        values["description"],
                        values["source_type"],
                        int(values["is_active"]),
                        int(actor["id"]),
                        now,
                        now,
                    ),
                )
                product_id = int(cursor.lastrowid)
                self._audit(
                    connection,
                    int(actor["id"]),
                    "product.created",
                    "product",
                    product_id,
                    {
                        "sku": values["sku"],
                        "price_cents": values["price_cents"],
                        "is_active": values["is_active"],
                    },
                )
                row = self._product_row(connection, product_id)
        except sqlite3.IntegrityError as error:
            if "products.sku" in str(error):
                raise CommerceValidationError("商品 SKU 已存在") from error
            raise CommerceValidationError("商品数据不符合数据库约束") from error
        return self._product_from_row(row)

    def update_product(
        self,
        actor_user_id: int,
        product_id: int,
        *,
        sku: Any = _UNSET,
        name: Any = _UNSET,
        category: Any = _UNSET,
        brand: Any = _UNSET,
        specification: Any = _UNSET,
        price_cents: Any = _UNSET,
        unit_price_cents: Any = _UNSET,
        unit: Any = _UNSET,
        image_path: Any = _UNSET,
        description: Any = _UNSET,
        source_type: Any = _UNSET,
        is_active: Any = _UNSET,
    ) -> dict[str, Any]:
        """Update supplied catalogue fields while preserving all omitted values."""

        if all(
            value is _UNSET
            for value in (
                sku,
                name,
                category,
                brand,
                specification,
                price_cents,
                unit_price_cents,
                unit,
                image_path,
                description,
                source_type,
                is_active,
            )
        ):
            raise CommerceValidationError("至少需要修改一个商品字段")
        product_id_value = self._positive_id(product_id, "商品编号")
        try:
            with self.database.transaction() as connection:
                actor = self._require_operator(connection, actor_user_id)
                current = self._product_row(connection, product_id_value)
                resolved_price = (
                    int(current["price_cents"])
                    if price_cents is _UNSET and unit_price_cents is _UNSET
                    else self._resolve_price(price_cents, unit_price_cents)
                )
                values = self._product_values(
                    sku=current["sku"] if sku is _UNSET else sku,
                    name=current["name"] if name is _UNSET else name,
                    category=current["category"] if category is _UNSET else category,
                    brand=current["brand"] if brand is _UNSET else brand,
                    specification=(
                        current["specification"] if specification is _UNSET else specification
                    ),
                    price_cents=resolved_price,
                    unit=current["unit"] if unit is _UNSET else unit,
                    image_path=current["image_path"] if image_path is _UNSET else image_path,
                    description=current["description"] if description is _UNSET else description,
                    source_type=(current["source_type"] if source_type is _UNSET else source_type),
                    is_active=(bool(current["is_active"]) if is_active is _UNSET else is_active),
                )
                now = timestamp_to_db(utc_now())
                connection.execute(
                    """
                    UPDATE products SET
                        sku = ?, name = ?, category = ?, brand = ?, specification = ?,
                        price_cents = ?, unit = ?, image_path = ?, description = ?,
                        source_type = ?, is_active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        values["sku"],
                        values["name"],
                        values["category"],
                        values["brand"],
                        values["specification"],
                        values["price_cents"],
                        values["unit"],
                        values["image_path"],
                        values["description"],
                        values["source_type"],
                        int(values["is_active"]),
                        now,
                        product_id_value,
                    ),
                )
                self._audit(
                    connection,
                    int(actor["id"]),
                    "product.updated",
                    "product",
                    product_id_value,
                    {"sku": values["sku"], "price_cents": values["price_cents"]},
                )
                updated = self._product_row(connection, product_id_value)
        except sqlite3.IntegrityError as error:
            if "products.sku" in str(error):
                raise CommerceValidationError("商品 SKU 已存在") from error
            raise CommerceValidationError("商品数据不符合数据库约束") from error
        return self._product_from_row(updated)

    def set_product_active(
        self, actor_user_id: int, product_id: int, is_active: bool
    ) -> dict[str, Any]:
        """Publish or unpublish a product without changing historical records."""

        if not isinstance(is_active, bool):
            raise CommerceValidationError("上架状态格式无效")
        product_id_value = self._positive_id(product_id, "商品编号")
        with self.database.transaction() as connection:
            actor = self._require_operator(connection, actor_user_id)
            current = self._product_row(connection, product_id_value)
            now = timestamp_to_db(utc_now())
            connection.execute(
                "UPDATE products SET is_active = ?, updated_at = ? WHERE id = ?",
                (int(is_active), now, product_id_value),
            )
            if bool(current["is_active"]) != is_active:
                self._audit(
                    connection,
                    int(actor["id"]),
                    "product.activated" if is_active else "product.deactivated",
                    "product",
                    product_id_value,
                )
            updated = self._product_row(connection, product_id_value)
        return self._product_from_row(updated)

    def delete_product(self, actor_user_id: int, product_id: int) -> None:
        """Delete an unused, unpublished product; referenced products must be retained."""

        product_id_value = self._positive_id(product_id, "商品编号")
        with self.database.transaction() as connection:
            actor = self._require_operator(connection, actor_user_id)
            product = self._product_row(connection, product_id_value)
            if bool(product["is_active"]):
                raise CommerceValidationError("上架中的商品不能删除，请先下架")
            reference_count = int(
                connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM product_recommendations WHERE product_id = ?)
                        + (SELECT COUNT(*) FROM orders WHERE product_id = ?)
                    """,
                    (product_id_value, product_id_value),
                ).fetchone()[0]
            )
            if reference_count:
                raise CommerceValidationError("商品已有推荐或订单记录，不能删除，只能保持下架")
            connection.execute("DELETE FROM products WHERE id = ?", (product_id_value,))
            self._audit(
                connection,
                int(actor["id"]),
                "product.deleted",
                "product",
                product_id_value,
                {"sku": str(product["sku"]), "name": str(product["name"])},
            )

    def recommend_product(
        self,
        actor_user_id: int,
        member_user_id: int,
        product_id: int,
        reason: str,
    ) -> dict[str, Any]:
        """Recommend an active product to a currently bound member as their advisor."""

        member_id = self._positive_id(member_user_id, "会员编号")
        product_id_value = self._positive_id(product_id, "商品编号")
        cleaned_reason = self._recommendation_reason(reason)
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            advisor = self._require_role(connection, actor_user_id, ROLE_ADVISOR)
            advisor_id = int(advisor["id"])
            self._require_active_member(connection, member_id)
            self._require_active_binding(connection, member_id, advisor_id)
            product = self._product_row(connection, product_id_value, conceal=True)
            if not bool(product["is_active"]):
                raise CommerceValidationError("只能推荐当前上架的商品")
            cursor = connection.execute(
                """
                INSERT INTO product_recommendations (
                    product_id, member_user_id, advisor_user_id, reason,
                    recommendation_source, status,
                    viewed_at, interested_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'advisor', 'new', NULL, NULL, ?, ?)
                """,
                (product_id_value, member_id, advisor_id, cleaned_reason, now, now),
            )
            recommendation_id = int(cursor.lastrowid)
            self._audit(
                connection,
                advisor_id,
                "product_recommendation.created",
                "product_recommendation",
                recommendation_id,
                {"member_user_id": member_id, "product_id": product_id_value},
            )
            row = self._recommendation_row(connection, recommendation_id)
        return self._recommendation_from_row(row)

    def list_recommendations(
        self, actor_user_id: int, *, member_user_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return only recommendations inside the actor's current role scope."""

        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            actor_id = int(actor["id"])
            role = str(actor["role_code"])
            parameters: list[object] = []
            clauses: list[str] = []
            if role == ROLE_MEMBER:
                if member_user_id is not None and member_user_id != actor_id:
                    raise CommercePermissionDeniedError
                clauses.append("r.member_user_id = ?")
                parameters.append(actor_id)
            elif role == ROLE_ADVISOR:
                clauses.extend(
                    (
                        "r.advisor_user_id = ?",
                        "EXISTS (SELECT 1 FROM advisor_bindings b "
                        "WHERE b.member_user_id = r.member_user_id "
                        "AND b.advisor_user_id = ? AND b.status = 'active')",
                    )
                )
                parameters.extend((actor_id, actor_id))
                if member_user_id is not None:
                    member_id = self._positive_id(member_user_id, "会员编号")
                    self._require_active_binding(connection, member_id, actor_id)
                    clauses.append("r.member_user_id = ?")
                    parameters.append(member_id)
            elif role == ROLE_OPERATOR:
                if member_user_id is not None:
                    member_id = self._positive_id(member_user_id, "会员编号")
                    self._require_active_member(connection, member_id, require_active=False)
                    clauses.append("r.member_user_id = ?")
                    parameters.append(member_id)
            else:  # pragma: no cover - database role constraint
                raise CommercePermissionDeniedError
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                f"""
                SELECT r.*, p.sku, p.name AS product_name, p.category,
                       p.brand, p.specification, p.price_cents, p.unit,
                       p.image_path, p.description, p.source_type, p.is_active,
                       u.username AS advisor_username,
                       COALESCE(ap.display_name, u.username) AS advisor_display_name,
                       member.username AS member_username,
                       COALESCE(mp.display_name, member.username) AS member_name,
                       (
                           SELECT COUNT(*) FROM orders o
                           WHERE o.recommendation_id = r.id
                       ) AS order_count,
                       (
                           SELECT o.status FROM orders o
                           WHERE o.recommendation_id = r.id
                           ORDER BY o.created_at DESC, o.id DESC
                           LIMIT 1
                       ) AS latest_order_status
                FROM product_recommendations r
                JOIN products p ON p.id = r.product_id
                LEFT JOIN users u ON u.id = r.advisor_user_id
                LEFT JOIN advisor_profiles ap ON ap.user_id = r.advisor_user_id
                JOIN users member ON member.id = r.member_user_id
                LEFT JOIN member_profiles mp ON mp.user_id = r.member_user_id
                {where}
                ORDER BY r.created_at DESC, r.id DESC
                """,
                tuple(parameters),
            ).fetchall()
        return [self._recommendation_from_row(row) for row in rows]

    def record_product_view(self, actor_user_id: int, recommendation_id: int) -> dict[str, Any]:
        """Record that a member viewed one of their own recommendations."""

        recommendation_id_value = self._positive_id(recommendation_id, "推荐编号")
        with self.database.transaction() as connection:
            member = self._require_role(connection, actor_user_id, ROLE_MEMBER)
            member_id = int(member["id"])
            row = self._recommendation_row_for_member(
                connection, recommendation_id_value, member_id
            )
            now = timestamp_to_db(utc_now())
            connection.execute(
                """
                UPDATE product_recommendations
                SET status = CASE WHEN status = 'new' THEN 'viewed' ELSE status END,
                    viewed_at = COALESCE(viewed_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, recommendation_id_value),
            )
            if row["viewed_at"] is None:
                self._audit(
                    connection,
                    member_id,
                    "product_recommendation.viewed",
                    "product_recommendation",
                    recommendation_id_value,
                )
            updated = self._recommendation_row(connection, recommendation_id_value)
        return self._recommendation_from_row(updated)

    def mark_interested(self, actor_user_id: int, recommendation_id: int) -> dict[str, Any]:
        """Mark one of the member's recommendations interested.

        Direct catalogue interest must use :meth:`mark_product_interested` so
        overlapping recommendation and product IDs can never be confused.
        """

        recommendation_id_value = self._positive_id(recommendation_id, "推荐编号")
        with self.database.transaction() as connection:
            member = self._require_role(connection, actor_user_id, ROLE_MEMBER)
            member_id = int(member["id"])
            row = self._recommendation_row_for_member(
                connection, recommendation_id_value, member_id
            )
            now = timestamp_to_db(utc_now())
            connection.execute(
                """
                UPDATE product_recommendations
                SET status = 'interested', viewed_at = COALESCE(viewed_at, ?),
                    interested_at = COALESCE(interested_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, now, recommendation_id_value),
            )
            if str(row["status"]) != "interested":
                self._audit(
                    connection,
                    member_id,
                    "product_recommendation.interested",
                    "product_recommendation",
                    recommendation_id_value,
                )
            updated = self._recommendation_row(connection, recommendation_id_value)
        return self._recommendation_from_row(updated)

    mark_recommendation_interested = mark_interested

    def mark_product_interested(self, actor_user_id: int, product_id: int) -> dict[str, Any]:
        """Unambiguously mark a directly browsed catalogue product interested."""

        product_id_value = self._positive_id(product_id, "商品编号")
        with self.database.transaction() as connection:
            member = self._require_role(connection, actor_user_id, ROLE_MEMBER)
            member_id = int(member["id"])
            product = self._product_row(connection, product_id_value, conceal=True)
            if not bool(product["is_active"]):
                raise CommerceValidationError("商品当前未上架")
            return self._mark_catalogue_interest(connection, member_id, product_id_value)

    def simulate_purchase(
        self,
        actor_user_id: int,
        product_id: int,
        *,
        quantity: int = 1,
        recommendation_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a local simulated order; no payment or logistics call is made."""

        return self._create_order(
            actor_user_id,
            product_id,
            quantity=quantity,
            recommendation_id=recommendation_id,
            reordered_from_order_id=None,
        )

    create_order = simulate_purchase

    def reorder(
        self, actor_user_id: int, order_id: int, *, quantity: int | None = None
    ) -> dict[str, Any]:
        """Repeat a member's own order using the product's current published price."""

        order_id_value = self._positive_id(order_id, "订单编号")
        with self.database.connect() as connection:
            member = self._require_role(connection, actor_user_id, ROLE_MEMBER)
            previous = connection.execute(
                "SELECT * FROM orders WHERE id = ? AND member_user_id = ?",
                (order_id_value, int(member["id"])),
            ).fetchone()
            if previous is None:
                raise CommercePermissionDeniedError
            resolved_quantity = int(previous["quantity"]) if quantity is None else quantity
            product_id = int(previous["product_id"])
        return self._create_order(
            actor_user_id,
            product_id,
            quantity=resolved_quantity,
            recommendation_id=None,
            reordered_from_order_id=order_id_value,
        )

    reorder_order = reorder

    def list_orders(
        self, actor_user_id: int, *, member_user_id: int | None = None
    ) -> list[dict[str, Any]]:
        """List all simulated orders for operators or only one's own for members."""

        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            actor_id = int(actor["id"])
            role = str(actor["role_code"])
            if role == ROLE_MEMBER:
                if member_user_id is not None and member_user_id != actor_id:
                    raise CommercePermissionDeniedError
                where = "WHERE o.member_user_id = ?"
                parameters: tuple[object, ...] = (actor_id,)
            elif role == ROLE_OPERATOR:
                if member_user_id is None:
                    where = ""
                    parameters = ()
                else:
                    member_id = self._positive_id(member_user_id, "会员编号")
                    self._require_active_member(connection, member_id, require_active=False)
                    where = "WHERE o.member_user_id = ?"
                    parameters = (member_id,)
            else:
                raise CommercePermissionDeniedError
            rows = connection.execute(
                f"""
                SELECT o.*, u.username AS member_username,
                       COALESCE(mp.display_name, u.username) AS member_name
                FROM orders o
                JOIN users u ON u.id = o.member_user_id
                LEFT JOIN member_profiles mp ON mp.user_id = o.member_user_id
                {where}
                ORDER BY o.created_at DESC, o.id DESC
                """,
                parameters,
            ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def mark_order_delivered(self, actor_user_id: int, order_id: int) -> dict[str, Any]:
        """Mark a local simulated order delivered; this does not call logistics."""

        order_id_value = self._positive_id(order_id, "订单编号")
        with self.database.transaction() as connection:
            operator = self._require_operator(connection, actor_user_id)
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id_value,)
            ).fetchone()
            if order is None:
                raise CommerceRecordNotFoundError("订单不存在")
            if str(order["status"]) != "created":
                raise InvalidOrderStateError("只有待交付的模拟订单可以标记为已交付")
            now = timestamp_to_db(utc_now())
            connection.execute(
                """
                UPDATE orders SET status = 'delivered', delivered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, order_id_value),
            )
            self._audit(
                connection,
                int(operator["id"]),
                "order.delivered",
                "order",
                order_id_value,
                {"member_user_id": int(order["member_user_id"])},
            )
            updated = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id_value,)
            ).fetchone()
        return self._order_from_row(updated)

    def _create_order(
        self,
        actor_user_id: int,
        product_id: int,
        *,
        quantity: int,
        recommendation_id: int | None,
        reordered_from_order_id: int | None,
    ) -> dict[str, Any]:
        product_id_value = self._positive_id(product_id, "商品编号")
        quantity_value = self._quantity(quantity)
        recommendation_id_value = (
            None if recommendation_id is None else self._positive_id(recommendation_id, "推荐编号")
        )
        with self.database.transaction() as connection:
            member = self._require_role(connection, actor_user_id, ROLE_MEMBER)
            member_id = int(member["id"])
            product = self._product_row(connection, product_id_value, conceal=True)
            if not bool(product["is_active"]):
                raise CommerceValidationError("商品当前未上架，不能生成订单")
            recommendation = None
            if recommendation_id_value is not None:
                recommendation = self._recommendation_row_for_member(
                    connection, recommendation_id_value, member_id
                )
                if int(recommendation["product_id"]) != product_id_value:
                    raise CommerceValidationError("推荐记录与所选商品不一致")
            if reordered_from_order_id is not None:
                original = connection.execute(
                    "SELECT product_id FROM orders WHERE id = ? AND member_user_id = ?",
                    (reordered_from_order_id, member_id),
                ).fetchone()
                if original is None or int(original["product_id"]) != product_id_value:
                    raise CommercePermissionDeniedError
            price_cents = int(product["price_cents"])
            total_amount_cents = price_cents * quantity_value
            now = timestamp_to_db(utc_now())
            order_no = f"SIM-{utc_now():%Y%m%d}-{uuid4().hex[:12].upper()}"
            cursor = connection.execute(
                """
                INSERT INTO orders (
                    order_no, member_user_id, product_id, recommendation_id,
                    reordered_from_order_id, product_name_snapshot, quantity,
                    unit_price_cents, total_amount_cents, currency, status,
                    delivered_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CNY', 'created', NULL, ?, ?)
                """,
                (
                    order_no,
                    member_id,
                    product_id_value,
                    recommendation_id_value,
                    reordered_from_order_id,
                    str(product["name"]),
                    quantity_value,
                    price_cents,
                    total_amount_cents,
                    now,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid)
            if recommendation is not None and str(recommendation["status"]) != "interested":
                connection.execute(
                    """
                    UPDATE product_recommendations
                    SET status = 'interested', viewed_at = COALESCE(viewed_at, ?),
                        interested_at = COALESCE(interested_at, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, now, recommendation_id_value),
                )
                self._audit(
                    connection,
                    member_id,
                    "product_recommendation.interested",
                    "product_recommendation",
                    recommendation_id_value,
                    {"via_order_id": order_id},
                )
            self._audit(
                connection,
                member_id,
                "order.created",
                "order",
                order_id,
                {
                    "product_id": product_id_value,
                    "quantity": quantity_value,
                    "unit_price_cents": price_cents,
                    "total_amount_cents": total_amount_cents,
                    "is_reorder": reordered_from_order_id is not None,
                },
            )
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._order_from_row(row)

    @staticmethod
    def _active_actor(connection: sqlite3.Connection, actor_user_id: int) -> sqlite3.Row:
        actor_id = CommerceService._positive_id(actor_user_id, "操作者编号")
        row = connection.execute(
            "SELECT id, role_code, account_status FROM users WHERE id = ?", (actor_id,)
        ).fetchone()
        if row is None or str(row["account_status"]) != ACCOUNT_ACTIVE:
            raise CommercePermissionDeniedError
        return row

    @classmethod
    def _require_role(
        cls, connection: sqlite3.Connection, actor_user_id: int, role_code: str
    ) -> sqlite3.Row:
        actor = cls._active_actor(connection, actor_user_id)
        if str(actor["role_code"]) != role_code:
            raise CommercePermissionDeniedError
        return actor

    @classmethod
    def _require_operator(cls, connection: sqlite3.Connection, actor_user_id: int) -> sqlite3.Row:
        return cls._require_role(connection, actor_user_id, ROLE_OPERATOR)

    @staticmethod
    def _require_active_member(
        connection: sqlite3.Connection, member_user_id: int, *, require_active: bool = True
    ) -> None:
        row = connection.execute(
            "SELECT role_code, account_status FROM users WHERE id = ?", (member_user_id,)
        ).fetchone()
        if row is None or str(row["role_code"]) != ROLE_MEMBER:
            raise CommerceRecordNotFoundError("会员不存在")
        if require_active and str(row["account_status"]) != ACCOUNT_ACTIVE:
            raise CommerceValidationError("会员账号已停用")

    @staticmethod
    def _require_active_binding(
        connection: sqlite3.Connection, member_user_id: int, advisor_user_id: int
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM advisor_bindings
            WHERE member_user_id = ? AND advisor_user_id = ? AND status = 'active'
            """,
            (member_user_id, advisor_user_id),
        ).fetchone()
        if row is None:
            raise CommercePermissionDeniedError

    @staticmethod
    def _product_row(
        connection: sqlite3.Connection, product_id: int, *, conceal: bool = False
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            if conceal:
                raise CommercePermissionDeniedError
            raise CommerceRecordNotFoundError("商品不存在")
        return row

    @staticmethod
    def _recommendation_row(connection: sqlite3.Connection, recommendation_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM product_recommendations WHERE id = ?", (recommendation_id,)
        ).fetchone()
        if row is None:
            raise CommerceRecordNotFoundError("推荐记录不存在")
        return row

    @classmethod
    def _recommendation_row_for_member(
        cls, connection: sqlite3.Connection, recommendation_id: int, member_user_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM product_recommendations
            WHERE id = ? AND member_user_id = ?
            """,
            (recommendation_id, member_user_id),
        ).fetchone()
        if row is None:
            raise CommercePermissionDeniedError
        return row

    @classmethod
    def _mark_catalogue_interest(
        cls, connection: sqlite3.Connection, member_user_id: int, product_id: int
    ) -> dict[str, Any]:
        existing = connection.execute(
            """
            SELECT * FROM product_recommendations
            WHERE member_user_id = ? AND product_id = ?
              AND recommendation_source = 'catalogue'
            """,
            (member_user_id, product_id),
        ).fetchone()
        if existing is not None:
            return cls._recommendation_from_row(existing)
        now = timestamp_to_db(utc_now())
        cursor = connection.execute(
            """
            INSERT INTO product_recommendations (
                product_id, member_user_id, advisor_user_id, reason,
                recommendation_source, status, viewed_at, interested_at,
                created_at, updated_at
            ) VALUES (?, ?, NULL, ?, 'catalogue', 'interested', ?, ?, ?, ?)
            """,
            (
                product_id,
                member_user_id,
                "会员从商品目录标记为感兴趣",
                now,
                now,
                now,
                now,
            ),
        )
        recommendation_id = int(cursor.lastrowid)
        cls._audit(
            connection,
            member_user_id,
            "product_recommendation.interested",
            "product_recommendation",
            recommendation_id,
            {"product_id": product_id, "source": "catalogue"},
        )
        created = cls._recommendation_row(connection, recommendation_id)
        return cls._recommendation_from_row(created)

    @staticmethod
    def _resolve_price(price_cents: Any, unit_price_cents: Any) -> int:
        if price_cents is _UNSET and unit_price_cents is _UNSET:
            raise CommerceValidationError("商品价格不能为空")
        if (
            price_cents is not _UNSET
            and unit_price_cents is not _UNSET
            and price_cents != unit_price_cents
        ):
            raise CommerceValidationError("商品价格字段不一致")
        value = unit_price_cents if price_cents is _UNSET else price_cents
        if isinstance(value, bool) or not isinstance(value, int):
            raise CommerceValidationError("商品价格必须使用整数分")
        if not 0 <= value <= 100_000_000_000:
            raise CommerceValidationError("商品价格超出允许范围")
        return value

    @classmethod
    def _product_values(cls, **values: Any) -> dict[str, Any]:
        is_active = values["is_active"]
        if not isinstance(is_active, bool):
            raise CommerceValidationError("上架状态格式无效")
        source_type = values["source_type"]
        if not isinstance(source_type, str) or source_type not in SOURCE_TYPES:
            raise CommerceValidationError("商品来源只能是自营或第三方")
        image_path = cls._image_path(values["image_path"])
        sku = cls._required_text(values["sku"], "商品 SKU", 64)
        if _SKU_PATTERN.fullmatch(sku) is None:
            raise CommerceValidationError("商品 SKU 只能包含字母、数字、点、横线和下划线")
        product = {
            "sku": sku.upper(),
            "name": cls._required_text(values["name"], "商品名称", 120),
            "category": cls._required_text(values["category"], "商品类别", 80),
            "brand": cls._optional_text(values["brand"], "品牌", 80),
            "specification": cls._optional_text(values["specification"], "规格", 120),
            "price_cents": cls._resolve_price(values["price_cents"], _UNSET),
            "unit": cls._optional_text(values["unit"], "计价单位", 24),
            "image_path": image_path,
            "description": cls._optional_text(values["description"], "商品说明", 2000),
            "source_type": source_type,
            "is_active": is_active,
        }
        for key, label in (
            ("name", "商品名称"),
            ("category", "商品类别"),
            ("brand", "品牌"),
            ("specification", "规格"),
            ("description", "商品说明"),
        ):
            cls._reject_unsafe_sales_language(product[key], label)
        return product

    @staticmethod
    def _image_path(value: Any) -> str | None:
        if value is None or value == "":
            return None
        cleaned = CommerceService._required_text(value, "商品图片路径", 500).replace("\\", "/")
        parts = cleaned.split("/")
        if cleaned.startswith("/") or ":" in cleaned or ".." in parts:
            raise CommerceValidationError("商品图片必须使用应用目录内的安全相对路径")
        return cleaned

    @staticmethod
    def _recommendation_reason(value: Any) -> str:
        reason = CommerceService._required_text(value, "推荐理由", 500)
        CommerceService._reject_unsafe_sales_language(reason, "推荐理由")
        return reason

    @staticmethod
    def _reject_unsafe_sales_language(value: str | None, label: str) -> None:
        if value is None:
            return
        normalized = value.casefold()
        if any(phrase in normalized for phrase in _MEDICAL_OR_PRESSURE_PHRASES):
            raise CommerceValidationError(
                f"{label}只能说明生活用途，不能包含医疗疗效承诺或制造焦虑用语"
            )

    @staticmethod
    def _quantity(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999:
            raise CommerceValidationError("商品数量必须是 1 到 999 之间的整数")
        return value

    @staticmethod
    def _positive_id(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CommerceValidationError(f"{label}无效")
        return value

    @staticmethod
    def _required_text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise CommerceValidationError(f"{label}格式无效")
        cleaned = " ".join(value.split())
        if not cleaned:
            raise CommerceValidationError(f"{label}不能为空")
        if len(cleaned) > maximum:
            raise CommerceValidationError(f"{label}不能超过 {maximum} 个字符")
        return cleaned

    @classmethod
    def _optional_text(cls, value: Any, label: str, maximum: int) -> str | None:
        if value is None or value == "":
            return None
        return cls._required_text(value, label, maximum)

    @staticmethod
    def _product_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["price_cents"] = int(row["price_cents"])
        result["unit_price_cents"] = int(row["price_cents"])
        result["is_active"] = bool(row["is_active"])
        result["created_at"] = timestamp_from_db(row["created_at"])
        result["updated_at"] = timestamp_from_db(row["updated_at"])
        return result

    @staticmethod
    def _recommendation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("viewed_at", "interested_at", "created_at", "updated_at"):
            result[field] = timestamp_from_db(row[field])
        if "is_active" in result:
            result["is_active"] = bool(result["is_active"])
        if "price_cents" in result:
            result["price_cents"] = int(result["price_cents"])
        if "order_count" in result:
            result["order_count"] = int(result["order_count"])
        return result

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("delivered_at", "created_at", "updated_at"):
            result[field] = timestamp_from_db(row[field])
        for field in ("quantity", "unit_price_cents", "total_amount_cents"):
            result[field] = int(result[field])
        result["product_name"] = str(result["product_name_snapshot"])
        result["price_cents"] = result["unit_price_cents"]
        result["total_cents"] = result["total_amount_cents"]
        return result

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        details_json = (
            None
            if details is None
            else json.dumps(
                dict(details), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                entity_type,
                entity_id,
                details_json,
                timestamp_to_db(utc_now()),
            ),
        )


__all__ = [
    "CommerceError",
    "CommercePermissionDeniedError",
    "CommerceRecordNotFoundError",
    "CommerceService",
    "CommerceValidationError",
    "InvalidOrderStateError",
]
