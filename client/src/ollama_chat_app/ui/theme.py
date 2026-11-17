"""Warm, high-contrast desktop palette with per-account display controls."""

from __future__ import annotations

PALETTES = {
    # ``sage`` remains the persisted compatibility name used by existing
    # preference records.  Its values now implement the approved
    # "Warm cards / companion" direction so existing accounts receive the
    # redesign without a database migration.
    "sage": ("#68785a", "#536546", "#faf4ea", "#fffdf8", "#e5d9c8"),
    # Explicit alias for new UI code and future preference values.
    "warm": ("#68785a", "#536546", "#faf4ea", "#fffdf8", "#e5d9c8"),
    "blue": ("#365b71", "#294659", "#eef3f4", "#fcfefe", "#cddbe0"),
    "rose": ("#795151", "#603e42", "#f7f0ec", "#fffaf7", "#e2d3cd"),
}

# Semantic accents stay legible in every account theme; text and original
# pictograms identify the category without depending on color perception.
RECORD_TONES = {
    "diet": ("#f6e2c9", "#674325", "#bd966a", "#efd5b4"),
    "water": ("#deedf8", "#29516c", "#87aec8", "#cce2f2"),
    "activity": ("#e2edd8", "#395739", "#91ad7d", "#d4e4c6"),
    "sleep": ("#ece2f4", "#5b426e", "#b19ac5", "#dfd1eb"),
    "environment": ("#dcedea", "#2d5853", "#83ada6", "#cce3de"),
    "medical": ("#f7ded9", "#733f39", "#c69a92", "#efcbc3"),
}


def _brightness(color: str, percent: int) -> str:
    channels = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    factor = max(70, min(120, percent)) / 100
    return "#" + "".join(f"{min(255, max(0, round(value * factor))):02x}" for value in channels)


def build_style(font_size: int = 20, theme_color: str = "warm", brightness: int = 100) -> str:
    size = max(16, min(30, int(font_size)))
    accent, hover, background, card, border = PALETTES.get(theme_color, PALETTES["warm"])
    # Application surface brightness only; preserve foreground readability.
    background = _brightness(background, max(90, brightness))
    card = _brightness(card, max(94, brightness))
    record_style = "\n".join(
        f'''QPushButton[recordTone="{name}"] {{ background: {surface}; color: {ink};
            border: 1px solid {edge}; border-radius: 17px; font-weight: 600; }}
        QPushButton[recordTone="{name}"]:hover,
        QPushButton[recordTone="{name}"]:pressed {{ background: {active}; }}
        QPushButton[recordTone="{name}"]:focus {{ border: 2px solid {ink}; }}
        QPushButton[recordTone="{name}"]:disabled {{ color: #737c75; background: #eeeee8; }}'''
        for name, (surface, ink, edge, active) in RECORD_TONES.items()
    )
    return f"""
    QWidget {{ color: #2f312c; background: {background};
        font-family: "Microsoft YaHei UI", "Segoe UI";
        font-size: {size}px; }}
    QMainWindow, QDialog, QStackedWidget {{ background: {background}; }}
    QToolBar#GlobalToolbar {{ background: {card}; border: none;
        border-bottom: 1px solid {border}; padding: 8px 18px; spacing: 10px; }}
    QToolBar#GlobalToolbar::separator {{ width: 1px; background: {border};
        margin: 5px 4px; }}
    QLabel#ToolbarCaption {{ color: #73756f; font-size: {max(15, size - 3)}px;
        font-weight: 600; padding: 3px 4px; }}
    QLabel#CurrentUser {{ color: #536546; background: #f3ebdf;
        border: 1px solid {border}; border-radius: 12px; padding: 7px 12px;
        font-weight: 600; }}
    QPushButton#DeveloperButton {{ background: #f3ebdf; color: #536546;
        border: 1px solid {border}; border-radius: 14px; padding: 8px 14px;
        min-height: 32px; font-weight: 700; }}
    QPushButton#DeveloperButton:hover {{ background: #eadcc9; border-color: {accent}; }}
    QPushButton#NetworkButton {{ background: #eef4ea; color: #405b45;
        border: 1px solid #cbd9c4; border-radius: 14px; padding: 8px 13px;
        min-height: 32px; font-weight: 600; }}
    QPushButton#NetworkButton:hover {{ background: #e2eedf; border-color: {accent}; }}
    QPushButton#SettingsButton {{ background: transparent; border: 1px solid {border};
        border-radius: 14px; min-width: 44px; min-height: 40px; padding: 4px; }}
    QPushButton#SettingsButton:hover {{ background: #f1e8dc; border-color: {accent}; }}
    QProgressBar#GlobalProgress {{ background: #f0e7da; border: 1px solid {border};
        border-radius: 7px; min-height: 10px; max-height: 10px; text-align: center; }}
    QProgressBar#GlobalProgress::chunk {{ background: {accent}; border-radius: 6px; }}
    QStatusBar {{ border-top: 1px solid {border}; padding: 3px 12px; }}
    QFrame#Card, QFrame#HealthCard, QFrame#HealthNavigation {{ background: {card};
        border: 1px solid {border}; border-radius: 18px; }}
    QFrame#WorkspaceHeader {{ background: {card}; border: 1px solid {border};
        border-radius: 20px; }}
    QFrame#WarmCard {{ background: {card}; border: 1px solid {border};
        border-radius: 20px; }}
    QFrame#WarmCard[tone="sun"], QFrame#ReminderCard {{ background: #fff0df;
        border: 1px solid #efbd9b; border-radius: 20px; }}
    QFrame#WarmCard[tone="lavender"], QFrame#AICard {{ background: #f0ecfb;
        border: 1px solid #d4c9ef; border-radius: 20px; }}
    QFrame#WarmCard[tone="mint"] {{ background: #edf4ea; border: 1px solid #cbdcc5;
        border-radius: 20px; }}
    /* Scheme 3 semantic tones use the warmTone property; keep the old
       tone selector above for compatibility with existing staff cards. */
    QFrame[warmTone="reminder"], QFrame#ReminderBanner,
    QFrame#TodayReminderBanner {{ background: #fff0df; border: 1px solid #efbd9b;
        border-radius: 20px; }}
    QFrame[warmTone="reading"], QFrame#DailyReadingCard {{ background: #edf4ea;
        border: 1px solid #cbdcc5; border-radius: 20px; }}
    QFrame[warmTone="voice"], QFrame#VoiceRecordCard,
    QFrame#VoiceAndRecordsCard {{ background: #f0ecfb; border: 1px solid #d4c9ef;
        border-radius: 20px; }}
    QFrame[warmTone="ai"], QFrame#AIHeaderCard, QFrame#ConversationSidebar,
    QFrame#AIInputCard {{ background: #f0ecfb; border: 1px solid #d4c9ef;
        border-radius: 20px; }}
    QFrame[warmTone="timeline"], QFrame#TodayTimelineCard,
    QFrame#TimelineCard {{ background: {card}; border: 1px solid {border};
        border-radius: 20px; }}
    QFrame[warmTone="profile"], QFrame#ProfileCard {{ background: #edf4ea;
        border: 1px solid #cbdcc5; border-radius: 20px; }}
    QFrame#WarmDialog, QDialog#MealCaptureDialog, QDialog#MemberStatisticsDialog,
    QDialog#HealthRecordDialog, QDialog#VoiceInputDialog {{ background: {background}; }}
    QLabel#TodayGreeting {{ color: #536546; font-size: {size + 3}px; font-weight: 800; }}
    QLabel#TodayWeather {{ color: #73756f; font-size: {max(15, size - 2)}px; }}
    QLabel#AssistantTitle {{ color: #5c4f88; font-size: {size + 4}px; font-weight: 800; }}
    QLabel#AssistantSubtitle, QLabel#ChatUserLabel {{ color: #73756f; }}
    QLabel#WarmReminderText {{ color: #6c3c27; font-weight: 800; }}
    QLabel#WarmReadingTopic {{ color: #6c765c; font-weight: 700; }}
    QLabel#WarmReadingTitle {{ color: #536546; font-size: {size + 7}px; font-weight: 800; }}
    QLabel#WarmReadingSummary, QLabel#WarmVoiceSubtitle {{ color: #73756f; }}
    QLabel#WarmCardTitle, QLabel#WarmVoiceTitle, QLabel#ProfileName,
    QLabel#RecordMetricTitle {{ color: #536546; font-size: {size + 1}px; font-weight: 800; }}
    QLabel#WarmCardSubtitle, QLabel#WarmCardSubtitle, QLabel#WarmVoiceSubtitle,
    QLabel#RecordMetricHint, QLabel#ProfileDetail {{ color: #73756f; }}
    QPushButton#WarmReminderAction, QPushButton#WarmVoiceButton,
    QPushButton#WarmStatsButton {{ background: #68785a; color: white; border: none;
        border-radius: 16px; min-height: 42px; padding: 9px 18px; font-weight: 800; }}
    QPushButton#WarmReminderAction {{ background: #e59a6e; color: #4d2d20; }}
    QPushButton#WarmReminderAction:hover {{ background: #d98658; color: white; }}
    QPushButton#WarmVoiceButton {{ background: #8d80d8; }}
    QPushButton#WarmVoiceButton:hover {{ background: #776bc0; }}
    QPushButton#WarmStatsButton:hover {{ background: #536546; }}
    QPushButton#WarmSecondaryAction, QPushButton#SecondaryButton,
    QPushButton#AttachmentButton, QPushButton#LifeReportButton,
    QPushButton#ConversationsButton {{ background: {card}; color: #536546;
        border: 1px solid {border}; border-radius: 14px; min-height: 40px;
        padding: 8px 14px; font-weight: 700; }}
    QPushButton#WarmSecondaryAction:hover, QPushButton#SecondaryButton:hover,
    QPushButton#AttachmentButton:hover, QPushButton#LifeReportButton:hover,
    QPushButton#ConversationsButton:hover {{ background: #f1e8dc; border-color: #c5aa8d; }}
    QPushButton#AISettingsButton {{ background: #f0ecfb; color: #5c4f88;
        border: 1px solid #d4c9ef; border-radius: 14px; min-width: 44px; min-height: 40px; }}
    QPushButton#VoiceButton {{ background: #8d80d8; color: white; border: none;
        border-radius: 15px; min-width: 46px; min-height: 44px; font-size: {size + 4}px; }}
    QPlainTextEdit#AIMessageInput {{ background: #fffdf8; border: 1px solid #d4c9ef;
        border-radius: 14px; padding: 10px; }}
    QListWidget#ConversationList {{ background: #fffdf8; border: 1px solid #d4c9ef;
        border-radius: 14px; padding: 5px; }}
    QListWidget#ConversationList::item {{ border-radius: 10px; padding: 9px; margin: 2px; }}
    QListWidget#ConversationList::item:selected {{ background: #8d80d8; color: white; }}
    QProgressBar#StatisticsProgress {{ background: #f0e7da; border: 1px solid {border};
        border-radius: 8px; min-height: 14px; text-align: center; color: #536546; }}
    QProgressBar#StatisticsProgress::chunk {{ background: #8d80d8; border-radius: 7px; }}
    QGroupBox#StatisticsReportCard {{ background: #fffdf8; border: 1px solid {border};
        border-radius: 18px; margin-top: 12px; padding: 18px 12px 12px; }}
    QGroupBox#StatisticsReportCard::title {{ subcontrol-origin: margin; left: 16px;
        padding: 0 6px; color: #536546; font-weight: 800; }}
    QLabel#StatisticsSubtitle, QLabel#StatisticsStatus {{ color: #73756f; }}
    QFrame#TodayTimelineCard QTableWidget, QTableWidget#TodayTimelineTable,
    QTableWidget#StatisticsTable {{ border: none; background: transparent; alternate-background-color: #faf4ea;
        gridline-color: #e5d9c8; }}
    QLabel#Eyebrow {{ color: #8c8176; font-size: {max(14, size - 4)}px;
        font-weight: 700; letter-spacing: 1px; }}
    QLabel#CardTitle {{ color: #536546; font-size: {size + 2}px; font-weight: 800; }}
    QLabel#CardValue {{ color: #2f312c; font-size: {size + 8}px; font-weight: 800; }}
    QLabel#CardHint {{ color: #73756f; font-size: {max(15, size - 3)}px; }}
    QFrame#HealthCard[tone="sun"], QFrame#StaffCard[tone="sun"], QFrame#AdvisorCard[tone="sun"] {{
        background: #fcf2d9; border: 1px solid #dfce9f; border-radius: 18px; }}
    QFrame#HealthCard[tone="sky"], QFrame#StaffCard[tone="sky"], QFrame#AdvisorCard[tone="sky"] {{
        background: #eaf2f4; border: 1px solid #bbd3da; border-radius: 18px; }}
    QFrame#HealthCard[tone="flower"], QFrame#StaffCard[tone="flower"],
    QFrame#AdvisorCard[tone="flower"] {{
        background: #f4ecf4; border: 1px solid #d7c3d8; border-radius: 18px; }}
    QFrame#ChatMessageBubble {{ background: {card}; border: 1px solid {border};
        border-radius: 18px; }}
    QFrame#ChatMessageBubble[role="user"] {{ background: {border}; }}
    QFrame#TodayReminderBanner {{ background: #fff0df; border: 2px solid #e9a97e;
        border-radius: 18px; padding: 8px; color: #6c3c27; font-weight: 700; }}
    QFrame#DailyReadingCard {{ background: #edf4ea; border: 1px solid #cbdcc5;
        border-radius: 20px; padding: 8px; }}
    QLabel#ReadingTitle {{ font-size: {size + 7}px; font-weight: 800; color: {accent}; }}
    QLabel {{ background: transparent; }}
    QLabel#Title, QLabel#PageTitle {{ font-size: {size + 9}px; font-weight: 700; color: {accent}; }}
    QLabel#AppTitle, QLabel#SectionTitle {{ font-size: {size + 3}px;
        font-weight: 600; color: {accent}; }}
    QLabel#Subtitle, QLabel#Hint, QLabel#HealthHint {{ color: #73756f; }}
    QLabel#DerivedWeekday {{ color: {accent}; background: {border};
        border-radius: 10px; padding: 6px 4px; font-weight: 600; }}
    QLabel#Error, QLabel#HealthError {{ color: #982b27; background: #fff0eb;
        border-radius: 8px; padding: 8px; }}
    QLabel#StatusOk, QLabel#HealthSuccess {{ color: #3f7350; }}
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
    QDoubleSpinBox QLineEdit, QComboBox QLineEdit {{ border: none; padding: 0;
        background: transparent; }}
    QPushButton, QToolButton {{ background: {card}; border: 1px solid {border};
        border-radius: 14px; padding: 9px 14px; min-height: 32px; }}
    QPushButton:hover, QToolButton:hover {{ background: #e7ece2; border-color: {accent}; }}
    QPushButton:pressed {{ background: #d9e2d4; }}
    QPushButton:disabled, QToolButton:disabled {{ color: #737c75; background: #eeeee8; }}
    QPushButton#PrimaryButton, QPushButton:checked {{ background: {accent}; color: white;
        border: 1px solid {accent}; border-radius: 16px; font-weight: 700; }}
    QPushButton#PrimaryButton:hover, QPushButton:checked:hover {{ background: {hover}; }}
    QPushButton#DangerButton {{ color: #982b27; border-color: #cc9b90; }}
    QPushButton:focus, QToolButton:focus {{ border: 2px solid {accent}; }}
    QCheckBox, QRadioButton {{ spacing: 9px; min-height: 36px; background: transparent; }}
    QCheckBox::indicator {{ width: 22px; height: 22px; border: 1px solid {accent};
        border-radius: 6px; background: {card}; }}
    QCheckBox::indicator:checked {{ background: {accent}; border: 3px solid {border}; }}
    QToolButton#InlineVoiceInput {{ padding: 2px; border-radius: 9px; min-height: 0; }}
    QTabWidget::pane {{ background: {card}; border: 1px solid {border}; border-radius: 12px; }}
    QTabBar::tab {{ background: {background}; padding: 9px 14px;
        margin: 2px; border-radius: 10px; }}
    QTabBar::tab:selected {{ background: {accent}; color: white; }}
    QTableWidget, QTableView, QListWidget {{ background: {card};
        alternate-background-color: {background};
        border: 1px solid {border}; border-radius: 10px; gridline-color: {border};
        selection-background-color: {accent}; selection-color: white; }}
    QHeaderView::section {{ background: {border}; color: #27362c; padding: 10px;
        border: none; border-right: 1px solid {border}; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 16px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: #b4c4aa; border-radius: 8px; min-height: 38px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QToolBar {{ background: {card}; border: none; spacing: 8px; padding: 4px 12px; }}
    QToolButton#IconButton {{ border: none; background: transparent; border-radius: 14px;
        min-width: 44px; min-height: 44px; }}
    QToolButton#IconButton:hover {{ background: #f1e8dc; }}
    QMenu, QComboBox QAbstractItemView {{ background: {card}; color: #27362c; }}
    QMenu::item {{ padding: 10px 20px; }}
    QMenu::item:selected {{ background: {accent}; color: white; }}
    QCalendarWidget QWidget {{ background: {card}; }}
    QCalendarWidget QToolButton {{ color: {accent}; min-width: 24px; min-height: 28px; }}
    {record_style}
    """


APP_STYLE = build_style()
