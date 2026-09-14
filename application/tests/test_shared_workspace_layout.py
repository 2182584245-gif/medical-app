"""Real widget rendering with synthetic identities, no external data or AI."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QDialogButtonBox
from test_desktop_redesign_integration import desktop as synthetic_desktop

from ollama_chat_app.ui.member_appointment import AppointmentDialog
from ollama_chat_app.ui.operator_workspace import MembershipDialog


@pytest.fixture
def desktop(qtbot, qapp, tmp_path, monkeypatch):
    # Finish prior qtbot deferred widget disposal before changing application
    # fonts/style during the synthetic desktop build (Windows Qt lifecycle).
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    yield from synthetic_desktop.__wrapped__(qtbot, qapp, tmp_path, monkeypatch)


@pytest.mark.parametrize("font_size", [20, 24])
def test_short_workspace_and_real_render(desktop, qtbot, qapp, tmp_path, font_size):
    window, users, _database, _calls = desktop
    root = Path(os.environ.get("SHARED_UI_REVIEW_OUTPUT", str(tmp_path / "renders")))
    root.mkdir(parents=True, exist_ok=True)
    for role, username in (
        ("member", "demo-member-1"),
        ("advisor", "demo-advisor-1"),
        ("operator", "demo-operator"),
    ):
        user = users[username]
        window.preferences_service.update(user.id, {"font_size": font_size, "theme_color": "sage"})
        window._login_succeeded(user)
        workspace = window._active_workspace
        pages = (0, 1, 2, 4) if role == "member" else (0, 1, 2)
        for index in pages:
            if role == "member":
                workspace.show_page(index)
            else:
                workspace.tabs.setCurrentIndex(index)
            # Assess our workspace independently of the root window's geometry
            # policy, which is integrated in a parallel change.
            window.resize(1138, 499)
            qapp.processEvents()
            qtbot.wait(20)
            assert window.grab().save(str(root / f"{role}-{index}-1138x499-font{font_size}.png"))
            assert window.width() <= 1138
            assert window.height() <= 499
            if role != "member":
                page = workspace.tabs.currentWidget()
                assert page.content_scroll.verticalScrollBar().maximum() > 0
                if index == (0 if role == "operator" else 1):
                    assert workspace.member_list.height() >= 160
                    # Scroll the actual business view, not a separate mock page.
                    page.content_scroll.ensureWidgetVisible(workspace.member_list, 0, 0)
                    qapp.processEvents()
                    assert window.grab().save(
                        str(root / f"{role}-member-list-scrolled-font{font_size}.png")
                    )
        window._logout()


def test_profile_entry_and_cancel_do_not_overwrite_settings(desktop, qtbot, qapp, tmp_path):
    window, users, database, _calls = desktop
    member = users["demo-member-1"]
    window._login_succeeded(member)
    workspace = window._active_workspace
    workspace.show_page(4)
    panel = workspace.my_platform_page
    panel.nickname.setText("尚未保存的合成昵称")
    before = window.health_service.get_profile(member.id)
    seen = []

    def inspect_and_close():
        dialog = panel._profile_dialog
        assert dialog is not None and dialog.isVisible()
        assert workspace.profile_editor.isVisible()
        seen.append(True)
        root = Path(os.environ.get("SHARED_UI_REVIEW_OUTPUT", str(tmp_path / "renders")))
        root.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(root / "profile-real-dialog.png"))
        dialog.reject()

    QTimer.singleShot(25, inspect_and_close)
    panel.open_profile()
    assert seen
    assert panel.nickname.text() == "尚未保存的合成昵称"
    assert window.health_service.get_profile(member.id) == before
    assert workspace.profile_editor.parentWidget() is None
    assert not workspace.profile_editor.isVisible()


def test_shared_dialog_screenshots(desktop, qtbot, qapp, tmp_path):
    window, users, _database, _calls = desktop
    user = users["demo-member-1"]
    window.preferences_service.update(user.id, {"font_size": 24})
    window._login_succeeded(user)
    root = Path(os.environ.get("SHARED_UI_REVIEW_OUTPUT", str(tmp_path / "renders")))
    root.mkdir(parents=True, exist_ok=True)
    for label, dialog in (
        ("appointment", AppointmentDialog()),
        ("membership", MembershipDialog("合成测试会员")),
    ):
        qtbot.addWidget(dialog)
        dialog.resize(760, 460)
        dialog.show()
        qapp.processEvents()
        assert dialog.grab().save(str(root / f"{label}-760x460-font24.png"))
        assert dialog.height() <= 460
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.mapTo(dialog, buttons.rect().bottomRight()).y() < dialog.height()
        dialog.close()
