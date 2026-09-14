"""Root window integration boundaries, with synthetic local identities only."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from test_desktop_redesign_integration import desktop as synthetic_desktop


@pytest.fixture
def desktop(qtbot, qapp, tmp_path, monkeypatch):
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    yield from synthetic_desktop.__wrapped__(qtbot, qapp, tmp_path, monkeypatch)


def test_registration_auto_login_does_not_silently_remember_offline(desktop, monkeypatch):
    window, users, _, _ = desktop
    calls = []
    monkeypatch.setattr(window, "_login", lambda *args, **kwargs: calls.append((args, kwargs)))
    window.cloud_mode = True
    window._registration_succeeded(users["demo-member-1"], "synthetic-password-not-sent")
    assert len(calls) == 1
    assert calls[0][0][0] == "demo-member-1"
    assert calls[0][1].get("remember_offline", False) is False


def test_new_profile_deferred_callback_does_not_open_for_other_member(desktop, monkeypatch):
    from ollama_chat_app.ui import main_window

    window, users, _, _ = desktop
    callbacks, opened = [], []

    class Timer:
        @staticmethod
        def singleShot(_delay, callback):
            callbacks.append(callback)

    monkeypatch.setattr(main_window, "QTimer", Timer)
    monkeypatch.setattr(window.member_workspace, "open_profile", lambda: opened.append(True))
    first, second = users["demo-member-1"], users["demo-member-2"]
    window._new_profile_user_id = first.id
    window._login_succeeded(first)
    assert len(callbacks) == 1
    window._logout()
    window._login_succeeded(second)
    callbacks[0]()
    assert not opened
    window._new_profile_user_id = second.id
    window._login_succeeded(second)
    callbacks[-1]()
    assert opened == [True]


def test_reminder_popup_is_plain_nonmodal_account_scoped_and_closed_on_logout(desktop, qapp):
    window, users, _, _ = desktop
    member = users["demo-member-1"]
    window._login_succeeded(member)
    window._reminder_due({"user_id": users["demo-member-2"].id, "message": "other synthetic"})
    assert not window._reminder_popups
    window._reminder_due({"user_id": member.id, "message": "<b>合成提醒</b>"})
    assert len(window._reminder_popups) == 1
    popup = window._reminder_popups[0]
    assert popup.textFormat() == Qt.TextFormat.PlainText
    assert popup.windowModality() == Qt.WindowModality.NonModal
    assert popup.text() == "<b>合成提醒</b>"
    window._logout()
    qapp.processEvents()
    assert not window._reminder_popups
    assert window.reminder_scheduler._user is None
    assert not window.reminder_scheduler._timer.isActive()


def test_network_probe_never_becomes_authentication_or_claims_completed_sync(desktop):
    window, users, _, _ = desktop
    calls = []
    assert not window.network_monitor.timer.isActive()
    window.cloud_sync_service = SimpleNamespace(sync_now=lambda: calls.append("refresh"))
    window._network_changed(
        {
            "state": "online",
            "latency_ms": 18,
            "host": "synthetic.test",
            "network": "本机合成适配器",
            "service_location": "属地未验证",
        }
    )
    assert calls == ["refresh"]
    assert window._current_user is None
    assert window.pages.currentWidget() is window.login_page
    assert "已同步" not in window.network_button.text()
    window._network_changed({"state": "online", "latency_ms": 20, "host": "synthetic.test"})
    assert calls == ["refresh"]
    window._network_changed({"state": "offline", "latency_ms": None, "host": "synthetic.test"})
    assert calls == ["refresh"]
    window.cloud_sync_service = None
