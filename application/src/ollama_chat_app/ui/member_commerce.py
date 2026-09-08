from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..paths import application_dir


def shopping_icon(kind, selected=False):
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#78906b"), 2))
    if kind == "cart":
        painter.drawLine(2, 5, 6, 5)
        painter.drawLine(6, 5, 9, 19)
        painter.drawLine(9, 19, 24, 19)
        painter.drawLine(8, 8, 25, 8)
        painter.drawLine(25, 8, 22, 16)
        painter.drawLine(22, 16, 9, 16)
        painter.drawEllipse(9, 22, 3, 3)
        painter.drawEllipse(20, 22, 3, 3)
    else:
        path = QPainterPath()
        path.moveTo(14, 24)
        path.cubicTo(0, 14, 1, 2, 9, 4)
        path.cubicTo(12, 4, 14, 7, 14, 8)
        path.cubicTo(16, 3, 25, 1, 26, 10)
        path.cubicTo(27, 16, 19, 21, 14, 24)
        if selected:
            painter.setBrush(QColor("#78906b"))
        painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def product_illustration(product, size=82):
    """Use a provided image or a visibly illustrative locally drawn category icon."""
    path = str(product.get("image_path") or "")
    image_path = Path(path) if path else None
    if image_path and not image_path.is_absolute():
        resolved = (application_dir() / image_path).resolve()
        image_path = resolved if resolved.is_relative_to(application_dir().resolve()) else None
    if image_path and image_path.is_file():
        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            return pixmap.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor("#f1efd9"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 100, size / 100)
    painter.setPen(QPen(QColor("#4e7453"), 3))
    painter.setBrush(QColor("#a4bb87"))
    category = str(product.get("category", "")) + str(product.get("name", ""))
    if any(word in category for word in ("水", "杯", "壶", "饮")):
        painter.drawRoundedRect(QRectF(32, 24, 34, 52), 7, 7)
        painter.drawRoundedRect(QRectF(38, 15, 22, 12), 3, 3)
        painter.drawLine(36, 49, 62, 49)
    elif any(word in category for word in ("睡", "枕", "家纺", "毛巾")):
        painter.drawRoundedRect(QRectF(17, 29, 66, 39), 14, 14)
        painter.drawRoundedRect(QRectF(26, 36, 48, 25), 9, 9)
    elif any(word in category for word in ("食", "米", "麦", "粮", "果")):
        painter.drawRoundedRect(QRectF(26, 18, 48, 58), 6, 6)
        painter.setBrush(QColor("#ecdc99"))
        painter.drawEllipse(QRectF(37, 32, 25, 25))
        painter.drawLine(49, 29, 49, 64)
    elif any(word in category for word in ("运动", "健身", "弹力")):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(26, 22, 48, 48))
        painter.drawEllipse(QRectF(35, 31, 30, 30))
    else:
        painter.drawRoundedRect(QRectF(25, 23, 50, 49), 5, 5)
        painter.drawLine(25, 39, 75, 39)
        painter.drawLine(50, 23, 50, 72)
    painter.setPen(QColor("#627058"))
    font = painter.font()
    font.setPixelSize(11)
    painter.setFont(font)
    painter.drawText(QRectF(0, 82, 100, 15), Qt.AlignmentFlag.AlignCenter, "商品示意图")
    painter.end()
    return pixmap


class CommercePanel(QWidget):
    data_changed = Signal()

    def __init__(self, commerce_service=None, parent=None):
        super().__init__(parent)
        self.commerce_service = commerce_service
        self.user_id = None
        self.products = []
        self.recommendations = []
        self.favorites = set()
        self.cart = []
        root = QVBoxLayout(self)
        notice = QLabel("本地虚拟购买 · 不收取费用，不付款、不发货。")
        notice.setObjectName("CommerceNotice")
        root.addWidget(notice)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索商品名称、类别、品牌")
        self.search_input.textChanged.connect(self._render_products)
        root.addWidget(self.search_input)
        self.product_tabs = QTabWidget()
        self.recommendation_list = QListWidget()
        self.product_list = QListWidget()
        self.product_tabs.addTab(self.recommendation_list, "为我推荐")
        self.product_tabs.addTab(self.product_list, "全部商品")
        root.addWidget(self.product_tabs, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        bottom = QFrame()
        bottom.setObjectName("HealthCard")
        bottom_row = QHBoxLayout(bottom)
        self.cart_button = QPushButton("购物车  ¥0.00")
        self.cart_button.setIcon(shopping_icon("cart"))
        self.cart_button.setIconSize(QSize(28, 28))
        self.cart_button.setMinimumHeight(52)
        self.cart_button.setObjectName("PrimaryButton")
        self.cart_button.clicked.connect(self.show_cart)
        self.favorites_button = QPushButton("收藏")
        self.favorites_button.setIcon(shopping_icon("favorite"))
        self.favorites_button.setMinimumHeight(52)
        self.favorites_button.clicked.connect(self.show_favorites)
        bottom_row.addWidget(self.cart_button, 1)
        bottom_row.addWidget(self.favorites_button)
        root.addWidget(bottom)

    def set_user(self, user_id):
        self.user_id = user_id
        self.refresh()

    def refresh(self):
        self.products, self.recommendations, self.cart, self.favorites = [], [], [], set()
        if self.user_id is not None and self.commerce_service is not None:
            try:
                self.products = self.commerce_service.list_products(self.user_id)
                self.recommendations = self.commerce_service.list_recommendations(self.user_id)
                self.cart = self.commerce_service.list_cart(self.user_id)
                self.favorites = {
                    int(row["id"]) for row in self.commerce_service.list_favorites(self.user_id)
                }
                self.status_label.clear()
            except Exception as error:
                self.status_label.setText(f"读取商品失败：{error}")
        elif self.commerce_service is None:
            self.status_label.setText("商品服务尚未连接")
        self._render_products()
        total = sum(int(item.get("total_amount_cents", 0)) for item in self.cart)
        quantity = sum(int(item.get("quantity", 0)) for item in self.cart)
        self.cart_button.setText(f"购物车（{quantity} 件）  ¥{total / 100:.2f}")
        self.favorites_button.setText(f"收藏（{len(self.favorites)}）")

    def _matches(self, product):
        query = self.search_input.text().strip().casefold()
        return (
            not query
            or query
            in " ".join(
                str(product.get(key) or "") for key in ("name", "category", "brand", "description")
            ).casefold()
        )

    def _render_products(self, *_args):
        self.product_list.clear()
        self.recommendation_list.clear()
        for product in self.products:
            if self._matches(product):
                self._add_row(self.product_list, product)
        by_id = {int(product["id"]): product for product in self.products}
        for recommendation in self.recommendations:
            product = recommendation.get("product") or by_id.get(
                int(recommendation.get("product_id", 0))
            )
            if product and self._matches(product):
                self._add_row(self.recommendation_list, product, recommendation)
        for widget, text in [
            (self.product_list, "暂无符合搜索条件的商品"),
            (self.recommendation_list, "暂无顾问推荐，可到“全部商品”浏览"),
        ]:
            if not widget.count():
                item = QListWidgetItem(text)
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                widget.addItem(item)

    def _add_row(self, target, product, recommendation=None):
        item = QListWidgetItem()
        item.setSizeHint(QSize(620, 114))
        target.addItem(item)
        row = QWidget()
        layout = QHBoxLayout(row)
        illustration = QLabel()
        illustration.setPixmap(product_illustration(product))
        illustration.setFixedSize(86, 86)
        layout.addWidget(illustration)
        names = QVBoxLayout()
        name = QPushButton(str(product.get("name", "商品")))
        name.setFlat(True)
        name.setStyleSheet("text-align: left; font-weight: 600;")
        name.clicked.connect(lambda: self.show_product(product, recommendation))
        names.addWidget(name)
        subtitle = str(product.get("specification") or product.get("category") or "")
        if recommendation and recommendation.get("reason"):
            subtitle += " · " + str(recommendation["reason"])
        caption = QLabel(subtitle)
        caption.setWordWrap(True)
        names.addWidget(caption)
        layout.addLayout(names, 1)
        price = QLabel(
            f"¥{int(product.get('price_cents', product.get('unit_price_cents', 0))) / 100:.2f}"
        )
        price.setStyleSheet("font-weight: 600;")
        layout.addWidget(price)
        add = QPushButton("加购物车")
        add.clicked.connect(lambda: self._add_cart(int(product["id"])))
        add.setEnabled(bool(product.get("is_active", True)))
        layout.addWidget(add)
        is_favorite = int(product["id"]) in self.favorites
        favorite = QPushButton("已收藏" if is_favorite else "收藏")
        favorite.setIcon(shopping_icon("favorite", is_favorite))
        favorite.clicked.connect(lambda: self._toggle_favorite(int(product["id"])))
        layout.addWidget(favorite)
        target.setItemWidget(item, row)

    def _add_cart(self, product_id):
        if self.user_id is None:
            return
        try:
            self.commerce_service.add_to_cart(self.user_id, product_id)
            self.refresh()
            self.status_label.setText("已加入购物车，可在下方查看和调整数量。")
        except Exception as error:
            self.status_label.setText(f"加入失败：{error}")

    def _toggle_favorite(self, product_id):
        if self.user_id is None:
            return
        try:
            self.commerce_service.set_favorite(
                self.user_id, product_id, product_id not in self.favorites
            )
            self.refresh()
        except Exception as error:
            self.status_label.setText(f"收藏失败：{error}")

    def show_product(self, product, recommendation=None):
        dialog = QDialog(self)
        dialog.setWindowTitle(str(product.get("name", "商品详情")))
        dialog.resize(590, 600)
        root = QVBoxLayout(dialog)
        illustration = QLabel()
        illustration.setPixmap(product_illustration(product, 190))
        illustration.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(illustration)
        price_cents = int(product.get("price_cents", product.get("unit_price_cents", 0)))
        lines = [
            str(product.get("name", "商品")),
            f"¥{price_cents / 100:.2f} / {product.get('unit') or '件'}",
        ]
        for key, label in [
            ("brand", "品牌"),
            ("specification", "规格"),
            ("category", "类别"),
            ("description", "介绍"),
        ]:
            if product.get(key):
                lines.append(f"{label}：{product[key]}")
        if recommendation:
            lines.append(f"顾问推荐理由：{recommendation.get('reason', '未填写')}")
            try:
                self.commerce_service.record_product_view(self.user_id, recommendation["id"])
            except Exception as error:
                self.status_label.setText(f"浏览状态未更新：{error}")
        description = QLabel("\n\n".join(lines))
        description.setWordWrap(True)
        root.addWidget(description, 1)
        actions = QHBoxLayout()
        add = QPushButton("加购物车")
        add.clicked.connect(lambda: self._add_cart(int(product["id"])))
        actions.addWidget(add)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def show_favorites(self):
        if self.user_id is None or self.commerce_service is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("我的收藏")
        dialog.resize(870, 550)
        root = QVBoxLayout(dialog)
        listing = QListWidget()
        root.addWidget(listing)

        def populate():
            listing.clear()
            try:
                products = self.commerce_service.list_favorites(self.user_id)
                for product in products:
                    self._add_row(listing, product)
                if not products:
                    listing.addItem("还没有收藏的商品。")
            except Exception as error:
                listing.addItem(f"读取失败：{error}")

        refresh = QPushButton("更新收藏列表")
        refresh.clicked.connect(populate)
        root.addWidget(refresh)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(dialog.reject)
        root.addWidget(close)
        populate()
        dialog.exec()

    def show_cart(self):
        if self.user_id is None or self.commerce_service is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("购物车明细 · 本地虚拟购买")
        dialog.resize(820, 550)
        root = QVBoxLayout(dialog)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["商品", "数量（0 为移除）", "单价", "小计"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(table, 1)
        total = QLabel()
        total.setObjectName("SectionTitle")
        root.addWidget(total)
        notice = QLabel("确认后仅创建本地虚拟订单，不会扣款或发货。订单历史保留在本机。")
        notice.setWordWrap(True)
        root.addWidget(notice)

        def populate():
            self.refresh()
            table.setRowCount(len(self.cart))
            for row, product in enumerate(self.cart):
                table.setItem(row, 0, QTableWidgetItem(str(product["name"])))
                quantity = QSpinBox()
                quantity.setRange(0, 999)
                quantity.setValue(int(product["quantity"]))
                quantity.setProperty("product_id", int(product["id"]))
                quantity.editingFinished.connect(lambda field=quantity: update_quantity(field))
                table.setCellWidget(row, 1, quantity)
                table.setItem(row, 2, QTableWidgetItem(f"¥{int(product['price_cents']) / 100:.2f}"))
                table.setItem(
                    row, 3, QTableWidgetItem(f"¥{int(product['total_amount_cents']) / 100:.2f}")
                )
            total.setText(
                f"合计  ¥{sum(int(p['total_amount_cents']) for p in self.cart) / 100:.2f}"
            )
            buy.setEnabled(bool(self.cart))

        def update_quantity(field):
            try:
                self.commerce_service.set_cart_quantity(
                    self.user_id, int(field.property("product_id")), field.value()
                )
                populate()
            except Exception as error:
                notice.setText(f"数量调整失败：{error}")

        def checkout():
            # Focus-out commits the latest spinbox edit before the confirmation.
            table.setFocus()
            try:
                self.refresh()
                amount = sum(int(p["total_amount_cents"]) for p in self.cart)
                if (
                    QMessageBox.question(
                        dialog, "确认虚拟购买", f"共 ¥{amount / 100:.2f}，确认创建虚拟订单吗？"
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    return
                orders = self.commerce_service.checkout_cart(self.user_id)
                self.refresh()
                self.data_changed.emit()
                QMessageBox.information(
                    dialog, "虚拟购买完成", f"已创建 {len(orders)} 笔本地虚拟订单，没有实际扣款。"
                )
                dialog.accept()
            except Exception as error:
                notice.setText(f"购买未完成：{error}")

        buttons = QHBoxLayout()
        buy = QPushButton("确认虚拟购买")
        buy.setObjectName("PrimaryButton")
        buy.clicked.connect(checkout)
        buttons.addWidget(buy)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.reject)
        buttons.addWidget(close)
        root.addLayout(buttons)
        populate()
        dialog.exec()
