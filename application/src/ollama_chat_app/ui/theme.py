"""Warm, high-contrast desktop palette with per-account display controls."""
from __future__ import annotations

PALETTES = {
    "sage": ("#405b45", "#314a37", "#f4f1e7", "#fffdf6", "#d6dccd"),
    "blue": ("#365b71", "#294659", "#eef3f4", "#fcfefe", "#cddbe0"),
    "rose": ("#795151", "#603e42", "#f7f0ec", "#fffaf7", "#e2d3cd"),
}


def _brightness(color: str, percent: int) -> str:
    channels = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    factor = max(70, min(120, percent)) / 100
    return "#" + "".join(f"{min(255, max(0, round(value * factor))):02x}" for value in channels)


def build_style(font_size: int = 20, theme_color: str = "sage", brightness: int = 100) -> str:
    size = max(16, min(30, int(font_size)))
    accent, hover, background, card, border = PALETTES.get(theme_color, PALETTES["sage"])
    # Application surface brightness only; preserve foreground readability.
    background = _brightness(background, max(90, brightness))
    card = _brightness(card, max(94, brightness))
    return f"""
    QWidget {{ color: #27362c; background: {background};
        font-family: "Microsoft YaHei UI", "Segoe UI";
        font-size: {size}px; }}
    QMainWindow, QDialog, QStackedWidget {{ background: {background}; }}
    QFrame#Card, QFrame#HealthCard, QFrame#HealthNavigation {{ background: {card};
        border: 1px solid {border}; border-radius: 18px; }}
    QFrame#ChatMessageBubble {{ background: {card}; border: 1px solid {border};
        border-radius: 18px; }}
    QFrame#ChatMessageBubble[role="user"] {{ background: {border}; }}
    QLabel {{ background: transparent; }}
    QLabel#Title, QLabel#PageTitle {{ font-size: {size + 9}px; font-weight: 700; color: {accent}; }}
    QLabel#AppTitle, QLabel#SectionTitle {{ font-size: {size + 3}px;
        font-weight: 600; color: {accent}; }}
    QLabel#Subtitle, QLabel#Hint, QLabel#HealthHint {{ color: #56645b; }}
    QLabel#Error, QLabel#HealthError {{ color: #982b27; background: #fff0eb;
        border-radius: 8px; padding: 8px; }}
    QLabel#StatusOk, QLabel#HealthSuccess {{ color: #2d6647; }}
    QLabel#StatusBusy {{ color: {accent}; }}
    QLabel#StatusError {{ color: #982b27; }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QDateTimeEdit, QDateEdit, QTimeEdit {{ background: {card}; border: 1px solid {border};
        border-radius: 10px; padding: 8px 10px; min-height: 28px;
        selection-background-color: {accent}; selection-color: white; }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
    QDateTimeEdit:focus, QDateEdit:focus {{ border: 2px solid {accent}; }}
    QDateTimeEdit, QDateEdit, QTimeEdit {{ padding-right: 30px; }}
    QDateTimeEdit QLineEdit, QDateEdit QLineEdit, QSpinBox QLineEdit,
    QDoubleSpinBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
    QPushButton, QToolButton {{ background: {card}; border: 1px solid {border};
        border-radius: 12px; padding: 9px 14px; min-height: 28px; }}
    QPushButton:hover, QToolButton:hover {{ background: #e7ece2; border-color: {accent}; }}
    QPushButton:pressed {{ background: #d9e2d4; }}
    QPushButton:disabled, QToolButton:disabled {{ color: #737c75; background: #eeeee8; }}
    QPushButton#PrimaryButton, QPushButton:checked {{ background: {accent}; color: white;
        border: 1px solid {accent}; font-weight: 600; }}
    QPushButton#PrimaryButton:hover, QPushButton:checked:hover {{ background: {hover}; }}
    QPushButton#DangerButton {{ color: #982b27; border-color: #cc9b90; }}
    QToolButton#InlineVoiceInput {{ padding: 2px; border-radius: 9px; min-height: 0; }}
    QTabWidget::pane {{ background: {card}; border: 1px solid {border}; border-radius: 12px; }}
    QTabBar::tab {{ background: {background}; padding: 12px 18px;
        margin: 2px; border-radius: 10px; }}
    QTabBar::tab:selected {{ background: {accent}; color: white; }}
    QTableWidget, QTableView, QListWidget {{ background: {card};
        alternate-background-color: {background};
        border: 1px solid {border}; border-radius: 10px; gridline-color: {border};
        selection-background-color: {accent}; selection-color: white; }}
    QHeaderView::section {{ background: #e5eade; color: #304336; padding: 10px;
        border: none; border-right: 1px solid {border}; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #a9b7a2; border-radius: 5px; min-height: 32px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QToolBar {{ background: {card}; border: none; spacing: 8px; padding: 4px 12px; }}
    QStatusBar {{ background: {background}; color: #56645b; font-size: {max(14, size - 3)}px; }}
    QMenu, QComboBox QAbstractItemView {{ background: {card}; color: #27362c; }}
    QMenu::item {{ padding: 10px 20px; }}
    QMenu::item:selected {{ background: {accent}; color: white; }}
    QCalendarWidget QWidget {{ background: {card}; }}
    QCalendarWidget QToolButton {{ color: {accent}; min-width: 24px; min-height: 28px; }}
    """


APP_STYLE = build_style()
