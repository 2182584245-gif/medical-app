"""Shared warm-card styling for advisor, operator and developer workspaces.

The staff screens deliberately use a small, explicit design system instead of
letting each page invent its own colours.  This keeps the three role-specific
workspaces visually consistent with the member-facing ``warm`` direction while
leaving all service and permission code untouched.
"""

from .theme import PALETTES, _brightness, build_style


# The warm-card palette mirrors the approved preview direction.  Keep the
# values here as a local presentation layer: changing the staff appearance
# must not change account settings or service semantics.
WARM_PALETTE = {
    "accent": "#68785A",       # olive green
    "hover": "#536546",        # deep olive
    "background": "#FAF4EA",   # warm ivory
    "card": "#FFFDF8",         # soft white
    "border": "#E5D9C8",       # warm beige border
    "reminder": "#F39A7A",     # warm apricot
    "ai": "#8D80D8",           # soft lavender
    "success": "#7BAA82",
    "ink": "#2F312C",
    "muted": "#73756F",
}


def build_staff_style(preferences: dict) -> str:
    preferences = preferences or {}
    size = max(18, min(30, int(preferences.get("font_size", 20))))
    brightness = int(preferences.get("brightness", 100))
    theme = str(preferences.get("theme_color", "warm"))

    # Preserve an explicit blue/rose choice for users who already saved one,
    # but make the new warm direction the default (and map the old sage
    # default to the warmer olive variant rather than a visual jump).
    if theme in {"blue", "rose"}:
        accent, hover, background, card, border = PALETTES[theme]
        reminder, ai = WARM_PALETTE["reminder"], WARM_PALETTE["ai"]
        success, ink, muted = WARM_PALETTE["success"], "#27362c", WARM_PALETTE["muted"]
    else:
        accent = WARM_PALETTE["accent"]
        hover = WARM_PALETTE["hover"]
        background = WARM_PALETTE["background"]
        card = WARM_PALETTE["card"]
        border = WARM_PALETTE["border"]
        reminder, ai = WARM_PALETTE["reminder"], WARM_PALETTE["ai"]
        success, ink, muted = WARM_PALETTE["success"], WARM_PALETTE["ink"], WARM_PALETTE["muted"]
    background = _brightness(background, max(90, brightness))
    card = _brightness(card, max(94, brightness))
    return (
        build_style(size, theme if theme in PALETTES else "sage", brightness)
        + f"""
    /* Root role surfaces */
    QWidget#OperatorWorkspace, QWidget#AdvisorWorkspace,
    QDialog#DeveloperWorkspaceDialog {{
        background: {background}; color: {ink};
        font-family: "Microsoft YaHei UI", "Segoe UI";
        font-size: {size}px;
    }}
    QFrame#StaffHeader, QFrame#StaffCard, QFrame#AdvisorHeader, QFrame#AdvisorCard {{
        background: {card}; border: 1px solid {border}; border-radius: 20px;
        padding: 2px;
    }}
    QLabel#StaffTitle, QLabel#AdvisorTitle, QDialog#DeveloperWorkspaceDialog QLabel#PageTitle {{
        font-size: {size + 8}px; font-weight: 700; color: {accent};
    }}
    QLabel#MetricValue, QLabel#AdvisorMetricValue {{
        font-size: {size + 10}px; font-weight: 700; color: {accent};
    }}
    QLabel#AdvisorSectionTitle, QDialog#DeveloperWorkspaceDialog QLabel#SectionTitle {{
        font-size: {size + 2}px; font-weight: 700; color: {accent};
    }}
    QLabel#AdvisorHint, QLabel#BeijingTimeHint, QLabel#HealthHint,
    QLabel#CommerceNotice {{ color: {muted}; line-height: 1.3em; }}
    QLabel#DialogHeading {{ font-size: {size + 3}px; font-weight: 700; color: {accent}; }}
    QLabel#WorkspaceStatusSuccess, QLabel#AdvisorStatusSuccess {{
        color: #2d6647; background: #edf6ed; padding: 10px 12px; border-radius: 12px;
    }}
    QLabel#WorkspaceStatusError, QLabel#AdvisorStatusError {{
        color: #982b27; background: #fff0eb; padding: 10px 12px; border-radius: 12px;
    }}
    QTabWidget::pane {{ background: {card}; border: 1px solid {border}; border-radius: 16px; top: -1px; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{ background: #f3eadc; color: {ink}; border: 1px solid transparent;
        border-radius: 12px; padding: 12px 18px; margin: 3px 3px 0 0; font-weight: 700; }}
    QTabBar::tab:hover {{ background: #f6dccd; border-color: {reminder}; }}
    QTabBar::tab:selected {{ background: {accent}; color: white; }}
    QListWidget, QTableWidget, QTableView {{ background: {card}; color: {ink};
        alternate-background-color: #fbf5eb; border: 1px solid {border};
        border-radius: 14px; gridline-color: {border};
        selection-background-color: #dfe8d7; selection-color: {ink}; }}
    QListWidget::item {{ padding: 13px 14px; min-height: 28px; border-radius: 10px; }}
    QListWidget::item:hover {{ background: #f6e4d8; }}
    QListWidget::item:selected {{ background: #dfe8d7; color: {ink}; border-left: 4px solid {accent}; }}
    QHeaderView::section {{ background: #efe5d6; color: {ink}; padding: 12px 10px;
        border: none; border-right: 1px solid {border}; font-weight: 700; }}
    QTableWidget::item {{ padding: 8px; }}
    QTableCornerButton::section {{ background: #efe5d6; border: 0; }}
    QSplitter::handle {{ background: {border}; border-radius: 4px; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QDateTimeEdit, QDateEdit, QTimeEdit {{ background: {card}; color: {ink};
        border: 1px solid {border}; border-radius: 12px; padding: 9px 11px;
        selection-background-color: {accent}; selection-color: white; }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
    QSpinBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus, QDateEdit:focus,
    QTimeEdit:focus {{ border: 2px solid {accent}; }}
    QPushButton, QToolButton {{ background: {card}; color: {ink}; border: 1px solid {border};
        border-radius: 14px; padding: 10px 16px; min-height: 30px; font-weight: 600; }}
    QPushButton:hover, QToolButton:hover {{ background: #f6dccd; border-color: {reminder}; }}
    QPushButton:pressed {{ background: #eec2ad; }}
    QPushButton#PrimaryButton {{ background: {accent}; color: white; border-color: {accent};
        border-radius: 16px; font-weight: 700; }}
    QPushButton#PrimaryButton:hover {{ background: {hover}; }}
    QPushButton#DangerButton {{ color: #982b27; border-color: #d39b8c; }}
    QDialogButtonBox QPushButton {{ min-width: 100px; min-height: 38px; }}
    QCheckBox, QRadioButton {{ spacing: 9px; min-height: 34px; }}
    QCheckBox::indicator {{ width: 22px; height: 22px; border: 1px solid {accent};
        border-radius: 6px; background: {card}; }}
    QCheckBox::indicator:checked {{ background: {accent}; border: 3px solid {border}; }}
    QProgressBar {{ background: #f0e7db; color: {ink}; border: none; border-radius: 8px;
        min-height: 14px; text-align: center; }}
    QProgressBar::chunk {{ background: {reminder}; border-radius: 8px; }}
    QFrame#HealthCard, QFrame#StaffCard[tone="sun"] {{ background: #fff1df; border-color: #edc5a9; }}
    QFrame#StaffCard[tone="sky"] {{ background: #eee9f9; border-color: #d1c5ec; }}
    QFrame#StaffCard[tone="flower"] {{ background: #e8f1e4; border-color: #c4d8ba; }}
    QWidget#OperatorAiConfigPanel QFrame#HealthCard {{ background: #eee9f9; border-color: #d1c5ec; }}
    QLabel#StructuredAiEnabled, QLabel#MemberAiEnabled, QLabel#AdvisorAiEnabled {{ color: {ai}; }}
    QLabel#NetworkStatus, QLabel#WorkspaceStatusSuccess {{ color: #39724b; }}
    QLabel#OperatorPerformancePanel[statusTone="success"],
    QLabel[statusTone="success"] {{ color: #39724b; background: #edf6ed; padding: 8px 10px; border-radius: 10px; }}
    QLabel[statusTone="error"] {{ color: #982b27; background: #fff0eb; padding: 8px 10px; border-radius: 10px; }}
    """
    )
