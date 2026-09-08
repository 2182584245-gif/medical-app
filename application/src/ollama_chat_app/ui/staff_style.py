"""Staff object names follow the shared user-selectable application palette."""

from .theme import PALETTES, _brightness, build_style


def build_staff_style(preferences: dict) -> str:
    size = max(16, min(30, int(preferences.get("font_size", 20))))
    brightness = int(preferences.get("brightness", 100))
    theme = str(preferences.get("theme_color", "sage"))
    accent, _hover, background, card, border = PALETTES.get(theme, PALETTES["sage"])
    background = _brightness(background, max(90, brightness))
    card = _brightness(card, max(94, brightness))
    return (
        build_style(size, theme, brightness)
        + f"""
    QWidget#OperatorWorkspace, QWidget#AdvisorWorkspace {{ background: {background}; }}
    QFrame#StaffHeader, QFrame#StaffCard, QFrame#AdvisorHeader, QFrame#AdvisorCard {{
        background: {card}; border: 1px solid {border}; border-radius: 14px;
    }}
    QLabel#StaffTitle, QLabel#AdvisorTitle {{
        font-size: {size + 7}px; font-weight: 700; color: {accent};
    }}
    QLabel#MetricValue, QLabel#AdvisorMetricValue {{
        font-size: {size + 10}px; font-weight: 700; color: {accent};
    }}
    QLabel#AdvisorSectionTitle {{ font-size: {size + 2}px; font-weight: 600; color: {accent}; }}
    QLabel#WorkspaceStatusSuccess, QLabel#AdvisorStatusSuccess {{
        color: #2d6647; padding: 8px; border-radius: 8px;
    }}
    QLabel#WorkspaceStatusError, QLabel#AdvisorStatusError {{
        color: #982b27; background: #fff0eb; padding: 8px; border-radius: 8px;
    }}
    QTabBar {{ background: {background}; }}
    QTabBar::tab {{ font-weight: 600; }}
    QListWidget::item {{ padding: 10px; }}
    QScrollArea > QWidget > QWidget {{ background: {card}; }}
    QTableCornerButton::section {{ background: {background}; border: 0; }}
    QSplitter::handle {{ background: {border}; }}
    """
    )
