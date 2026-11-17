"""Keep long forms scrollable and confirmation buttons reachable on short screens."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QFormLayout, QScrollArea, QVBoxLayout, QWidget


def fit_dialog(dialog, *, width=760, height=680):
    screen = dialog.screen()
    if screen:
        bounds = screen.availableGeometry()
        dialog.setMinimumSize(0, 0)
        dialog.resize(
            min(width, max(320, bounds.width() - 48)),
            min(height, max(280, int(bounds.height() * 0.80))),
        )


def scroll_form_dialog(dialog, form):
    """Move an existing top-level form into a viewport; preserve its widgets."""
    buttons = next(
        (child for child in dialog.findChildren(QDialogButtonBox) if form.indexOf(child) >= 0), None
    )
    if buttons:
        row = form.getWidgetPosition(buttons)[0]
        form.takeRow(row)
    body = QWidget()
    body.setLayout(form)  # Qt detaches the layout from the dialog safely.
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    outer = QVBoxLayout(dialog)
    scroll = QScrollArea(dialog)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(body)
    outer.addWidget(scroll, 1)
    if buttons:
        outer.addWidget(buttons)
    dialog.form_scroll = scroll
    fit_dialog(dialog)


def scroll_page(page, *, minimum_height=420):
    """Preserve a business page's identity while giving its content usable height."""
    body = QWidget()
    body.setLayout(page.layout())
    body.setMinimumHeight(minimum_height)
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea(page)
    scroll.setWidgetResizable(True)
    scroll.setWidget(body)
    outer.addWidget(scroll)
    page.content_scroll = scroll
    return page
