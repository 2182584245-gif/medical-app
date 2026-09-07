APP_STYLE = """
QWidget {
    background: #f5f7fb;
    color: #1f2937;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 16px;
}
QMainWindow {
    background: #f5f7fb;
}
QFrame#Card {
    background: #ffffff;
    border: 1px solid #dce2eb;
    border-radius: 14px;
}
QLabel#Title {
    font-size: 26px;
    font-weight: 600;
    color: #111827;
}
QLabel#Subtitle, QLabel#Hint {
    color: #64748b;
}
QLabel#Error {
    color: #b42318;
    background: #fff1f0;
    border: 1px solid #ffd5d2;
    border-radius: 8px;
    padding: 8px;
}
QLabel#StatusOk {
    color: #067647;
}
QLabel#StatusBusy {
    color: #175cd3;
}
QLabel#StatusError {
    color: #b42318;
}
QLineEdit, QPlainTextEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
    border: 1px solid #2563eb;
}
QDateTimeEdit, QDateEdit {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 6px 28px 6px 10px;
    min-height: 28px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QDateTimeEdit:focus, QDateEdit:focus {
    border: 1px solid #2563eb;
}
QDateTimeEdit QLineEdit, QDateEdit QLineEdit {
    border: none;
    border-radius: 0;
    padding: 0;
    background: transparent;
}
QCalendarWidget QToolButton {
    min-width: 24px;
    min-height: 28px;
    color: #1f2937;
    background: #ffffff;
}
QPushButton {
    background: #ffffff;
    color: #1f2937;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 8px 14px;
    min-height: 28px;
}
QPushButton:hover {
    background: #f1f5f9;
}
QPushButton:pressed {
    background: #e2e8f0;
}
QPushButton:disabled {
    color: #94a3b8;
    background: #f8fafc;
}
QPushButton#PrimaryButton {
    background: #2563eb;
    color: #ffffff;
    border: 1px solid #2563eb;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover {
    background: #1d4ed8;
}
QPushButton#DangerButton {
    color: #b42318;
    border-color: #f0aaa4;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
"""
