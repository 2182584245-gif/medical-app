from __future__ import annotations

import json
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from ollama_chat_app.config import DEFAULT_ALIYUN_ENDPOINT, DEFAULT_SUPABASE_ENDPOINT
from ollama_chat_app.endpoint_settings import (
    EndpointSettings,
    EndpointSettingsError,
    EndpointSettingsStore,
    validated_endpoint,
)
from ollama_chat_app.ui.auth_pages import LoginPage
from ollama_chat_app.ui.endpoint_settings_dialog import EndpointSettingsDialog


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private"))
    return EndpointSettingsStore(tmp_path / "settings" / "connection-settings.json")


def test_defaults_are_public_https_routes_without_creating_files(store):
    settings = store.load()
    assert settings.mode == "cloud" and settings.selected_target == "aliyun"
    assert settings.base_url == DEFAULT_ALIYUN_ENDPOINT == "https://39.106.166.15/aliyun"
    assert settings.supabase_url == DEFAULT_SUPABASE_ENDPOINT == "https://39.106.166.15/supabase"
    assert settings.validated() == settings
    assert not store.path.parent.exists()


@pytest.mark.parametrize("url", [
    "http://39.106.166.15/aliyun", "https://name:private-password@example.com",
    "https://example.com/?token=private", "https://example.com/#private",
    "https://127.0.0.1", "https://192.168.1.1", "file:///private",
    "https://example.com/../admin", "https://example.com/%2e%2e/admin",
    "https://example.com\\evil", "https://example.com/\nprivate",
])
def test_unsafe_endpoints_never_echo_secrets(url):
    with pytest.raises(EndpointSettingsError) as error:
        validated_endpoint(url)
    assert "private" not in str(error.value)
    assert url not in str(error.value)


def test_two_custom_routes_and_local_selection_survive_reload(store):
    settings = EndpointSettings("local", "supabase", "https://one.example.com/api",
                                "https://two.example.com/health")
    store.save(settings)
    assert EndpointSettingsStore(store.path).load() == settings
    store.save_connection("cloud", settings.supabase_url)
    assert store.load() == replace(settings, mode="cloud")
    store.save_connection("local", "")
    assert store.load() == settings
    assert set(json.loads(store.path.read_text())["endpoints"]) == {"aliyun", "supabase"}


def test_both_routes_validate_before_any_existing_file_changes(store):
    store.save(EndpointSettings())
    original = store.path.read_bytes()
    with pytest.raises(EndpointSettingsError):
        store.save(EndpointSettings(supabase_url="https://password:private@example.com"))
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("raw", [
    b'{"private": "secret"}', b'{"version":1,"version":1}', b'not json', b'x' * 17000,
])
def test_malformed_existing_settings_are_not_silently_overwritten(store, raw):
    store.path.parent.mkdir()
    store.path.write_bytes(raw)
    with pytest.raises(EndpointSettingsError):
        store.load()
    with pytest.raises(EndpointSettingsError):
        store.save(EndpointSettings())
    assert store.path.read_bytes() == raw


def test_stale_dialog_cannot_replace_newer_saved_custom_settings(store):
    initial = store.load()
    updated = replace(initial, aliyun_url="https://new.example.com")
    store.save(updated)
    with pytest.raises(EndpointSettingsError, match="别处更新"):
        store.save(replace(initial, selected_target="supabase"), expected=initial)
    assert store.load() == updated


def test_dialog_cancel_and_restore_defaults_are_nonmutating(qtbot, store):
    custom = EndpointSettings("local", "supabase", "https://custom.example.com/aliyun",
                              "https://custom.example.com/supabase")
    store.save(custom)
    dialog = EndpointSettingsDialog(custom, store=store)
    qtbot.addWidget(dialog)
    dialog.restore_button.click()
    assert dialog.aliyun_url_input.text() == DEFAULT_ALIYUN_ENDPOINT
    assert dialog.supabase_url_input.text() == DEFAULT_SUPABASE_ENDPOINT
    assert "尚未保存" in dialog.change_notice.text()
    assert store.load() == custom
    dialog.cancel_button.click()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert store.load() == custom


def test_dialog_saves_both_urls_and_selected_route_only_after_confirmation(qtbot, store):
    dialog = EndpointSettingsDialog(store.load(), store=store)
    qtbot.addWidget(dialog)
    dialog.aliyun_url_input.setText("https://custom.example.com/a/")
    dialog.supabase_url_input.setText("https://custom.example.com/s/")
    dialog.supabase_button.click()
    assert not dialog.aliyun_button.isChecked()
    assert not store.path.exists()
    dialog.save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert store.load() == EndpointSettings("cloud", "supabase",
                                           "https://custom.example.com/a",
                                           "https://custom.example.com/s")


def test_invalid_inactive_route_blocks_save_without_disclosing_its_value(qtbot, store):
    dialog = EndpointSettingsDialog(store.load(), store=store)
    qtbot.addWidget(dialog)
    dialog.supabase_url_input.setText("https://user:private-secret@example.com")
    dialog.save_button.click()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not store.path.exists()
    assert "private-secret" not in dialog.error_label.text()


def test_login_normal_view_hides_address_preserves_context_without_writing(qtbot, store):
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    page.resize(980, 680)
    page.show()
    assert page.cloud_url_input.isHidden()
    assert page.change_address_button.isVisible()
    assert not page.connection_apply_button.isVisible()
    events = []
    page.connection_change_requested.connect(lambda *args: events.append(args))
    page.set_connection_context("cloud", "https://existing.example.com/custom")
    assert page.cloud_url_input.text() == "https://existing.example.com/custom"
    assert not store.path.exists()
    assert events == []
    page.password_input.setText("synthetic-password")
    page.connection_mode.setCurrentIndex(0)
    assert page.connection_apply_button.isVisible()
    page.connection_apply_button.click()
    assert events == [("local", "https://existing.example.com/custom")]
    assert not page.password_input.text()


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_login_dialog_only_changes_connection_after_save(qtbot, store, monkeypatch, mode):
    settings = replace(store.load(), mode=mode)
    store.save(settings)
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    events = []
    page.connection_change_requested.connect(lambda *args: events.append(args))
    page.password_input.setText("synthetic-password")

    def save_dialog(dialog):
        dialog.supabase_button.click()
        dialog.save_button.click()
        return dialog.result()

    monkeypatch.setattr(EndpointSettingsDialog, "exec", save_dialog)
    page.change_address_button.click()
    assert store.load().selected_target == "supabase"
    assert page.cloud_url_input.text() == DEFAULT_SUPABASE_ENDPOINT
    assert page.password_input.text() == ""
    assert events == ([("cloud", DEFAULT_SUPABASE_ENDPOINT)] if mode == "cloud" else [])


def test_login_cancel_does_not_clear_password_or_emit_connection_change(qtbot, store, monkeypatch):
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    events = []
    page.connection_change_requested.connect(lambda *args: events.append(args))
    page.password_input.setText("synthetic-password")
    monkeypatch.setattr(EndpointSettingsDialog, "exec", lambda _dialog: QDialog.DialogCode.Rejected)
    page.change_address_button.click()
    assert page.password_input.text() == "synthetic-password"
    assert not store.path.exists() and not events


def test_bad_existing_settings_preserve_local_login_but_cannot_send_cloud_credentials(qtbot, store):
    store.path.parent.mkdir()
    store.path.write_text("invalid settings", encoding="utf-8")
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    events = []
    page.login_requested.connect(lambda *args: events.append(args))
    page.password_input.setText("synthetic-password")
    page.set_busy(False)
    page._submit()
    assert page.login_button.isEnabled()
    assert len(events) == 1 and page.connection_mode.currentData() == "local"
    page.connection_mode.setCurrentIndex(1)
    page._submit()
    assert not page.login_button.isEnabled() and len(events) == 1
    assert store.path.read_text() == "invalid settings"


def test_login_cannot_submit_to_old_mode_when_change_is_not_applied(qtbot, store):
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    events = []
    page.login_requested.connect(lambda *args: events.append(args))
    page.connection_mode.setCurrentIndex(0)
    page._submit()
    assert not events and "应用所选模式" in page.error_label.text()


def test_failed_endpoint_replacement_cannot_login_or_register_on_previous_service(
    qtbot, store, monkeypatch,
):
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    events = []
    page.login_requested.connect(lambda *args: events.append(args))

    def save_dialog(dialog):
        dialog.supabase_button.click()
        dialog.save_button.click()
        return dialog.result()

    monkeypatch.setattr(EndpointSettingsDialog, "exec", save_dialog)
    page.change_address_button.click()  # No controller accepts the emitted change.
    page._submit()
    assert not events
    assert not page.login_button.isEnabled() and not page.register_button.isEnabled()
    assert not page.connection_apply_button.isHidden()
    page.set_connection_context("cloud", DEFAULT_SUPABASE_ENDPOINT)
    assert page.login_button.isEnabled() and page.register_button.isEnabled()


def test_large_font_small_dialog_buttons_stay_visible(qtbot, store):
    dialog = EndpointSettingsDialog(store.load(), store=store)
    qtbot.addWidget(dialog)
    font = dialog.font()
    font.setPointSize(24)
    dialog.setFont(font)
    dialog.resize(640, 480)
    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.size().width() <= 640
    assert dialog.size().height() <= 480
    for button in (dialog.save_button, dialog.cancel_button):
        position = button.mapTo(dialog, button.rect().bottomRight())
        assert dialog.rect().contains(position)
    assert dialog.findChild(type(dialog.aliyun_url_input)).minimumWidth() == 0
    assert dialog.layout().itemAt(0).widget().horizontalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )


def test_extended_login_options_are_opt_in_explicit_and_never_double_emit(qtbot, store):
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    legacy, extended = [], []
    page.login_requested.connect(lambda *args: legacy.append(args))
    page.login_options_requested.connect(lambda *args: extended.append(args))
    page.username_input.setText("synthetic-member")
    page.password_input.setText("synthetic-password")
    page._submit()
    assert len(legacy) == 1 and not extended
    page.set_login_options_enabled()
    assert not page.offline_options_frame.isHidden()
    assert not page.allow_offline_checkbox.isChecked()
    assert not page.remember_offline_checkbox.isChecked()
    page._submit()
    assert len(legacy) == 1 and extended[-1][2] == {
        "allow_offline": False, "remember_offline": False,
    }
    page.allow_offline_checkbox.setChecked(True)
    page.remember_offline_checkbox.setChecked(True)
    page._submit()
    assert len(legacy) == 1 and extended[-1][2] == {
        "allow_offline": True, "remember_offline": True,
    }
    page.clear_password()
    assert not page.allow_offline_checkbox.isChecked()
    assert not page.remember_offline_checkbox.isChecked()
    assert not store.path.exists()


def test_offline_options_cannot_apply_to_local_mode_or_busy_login(qtbot, store):
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    page.set_login_options_enabled()
    page.allow_offline_checkbox.setChecked(True)
    page.remember_offline_checkbox.setChecked(True)
    page.set_busy(True)
    assert not page.allow_offline_checkbox.isEnabled()
    page.set_busy(False)
    page.set_connection_context("local")
    assert page.offline_options_frame.isHidden()
    events = []
    page.login_options_requested.connect(lambda *args: events.append(args))
    page._submit()
    assert events[-1][2] == {"allow_offline": False, "remember_offline": False}
