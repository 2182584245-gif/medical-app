from __future__ import annotations

import os
from copy import deepcopy
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidgetItem, QWidget

from ollama_chat_app.services.cloud_sync import RESOURCE_TYPES, CloudMirrorStore, CloudSyncService
from ollama_chat_app.time_utils import beijing_now
from ollama_chat_app.ui.advisor_workspace import AdvisorWorkspace
from ollama_chat_app.ui.health_workspace import HealthWorkspace
from ollama_chat_app.ui.operator_workspace import OperatorWorkspace
from ollama_chat_app.ui.snapshot_refresh import refresh_current_workspace
from ollama_chat_app.ui.theme import build_style


class NoReads:
    def __getattr__(self, method):
        def forbidden(*args, **kwargs):
            raise AssertionError(f"Snapshot refresh must not call {method}")

        return forbidden


def make_snapshot(actor, resources=None, revision="one"):
    values = {key: kind() for key, kind in RESOURCE_TYPES.items()}
    values["profile"] = {"user_id": actor, "display_name": "合成云端姓名"}
    values.update(resources or {})
    return {
        "schema_version": 1,
        "server_instance_id": "synthetic-refresh-instance",
        "actor_id": actor,
        "revision": revision,
        "complete": True,
        "resources": values,
        "excluded": {"chat_attachments": 0, "user_files": 0, "report_files": 0},
    }


def fixture_window(qtbot, tmp_path, role="member"):
    service = NoReads()
    user = SimpleNamespace(id=7, role_code=role, username="synthetic")
    if role == "member":
        workspace = HealthWorkspace(service, QWidget(), appointment_service=service)
        for page in (workspace.today_page, workspace.profile_page, workspace.service_page):
            page.user_id = user.id
    else:
        workspace = (AdvisorWorkspace if role == "advisor" else OperatorWorkspace)(service)
        workspace.actor_user_id = user.id
        if role == "operator":
            workspace.performance_panel.actor_user_id = user.id
    qtbot.addWidget(workspace)
    workspace.current_user = user
    client = SimpleNamespace(base_url="https://example.com/aliyun", is_authenticated=True)
    client.rpc = service.rpc
    sync = CloudSyncService(client, CloudMirrorStore(tmp_path / "mirror"))
    sync.actor_id, sync.actor_role = user.id, role
    window = SimpleNamespace(
        cloud_mode=True, _current_user=user, _active_workspace=workspace, cloud_sync_service=sync
    )
    return window, workspace


def publish(window, snapshot):
    sync = window.cloud_sync_service
    sync._accept(sync.cache_generation, window._current_user.id, snapshot)
    return refresh_current_workspace(window, snapshot)


def item(widget, row, data):
    entry = QListWidgetItem(str(data["id"]))
    entry.setData(Qt.ItemDataRole.UserRole, data)
    widget.addItem(entry)
    previous = widget.blockSignals(True)
    widget.setCurrentRow(row)
    widget.blockSignals(previous)


def test_member_snapshot_updates_timelines_and_preserves_editors(qtbot, tmp_path):
    window, workspace = fixture_window(qtbot, tmp_path)
    workspace.profile_editor.full_name_input.setText("未保存的姓名")
    draft = QLineEdit(workspace.chat_page)
    draft.setText("未发送的聊天草稿")
    resources = {
        "life_records": [
            {
                "id": 1,
                "user_id": 7,
                "category": "water",
                "occurred_at": beijing_now(),
                "content": "云端新增饮水",
                "details": {"amount_ml": 450},
            }
        ],
        "reminders": [
            {
                "id": 2,
                "user_id": 7,
                "title": "新的今日提醒",
                "scheduled_at": beijing_now(),
                "enabled": True,
            }
        ],
    }
    data = make_snapshot(7, resources)
    assert publish(window, data) == ("今日记录", "今日提醒")
    assert "450" in workspace.today_page.records_table.item(0, 1).text()
    assert workspace.today_page.reminders_table.item(0, 1).text() == "新的今日提醒"
    workspace.today_page.records_table.selectRow(0)
    workspace.today_page.records_table.setCurrentCell(0, 0)
    publish(window, data)
    assert workspace.today_page.records_table.currentRow() == 0
    assert workspace.profile_editor.full_name_input.text() == "未保存的姓名"
    assert draft.text() == "未发送的聊天草稿"
    workspace.stack.setCurrentWidget(workspace.chat_page)
    assert refresh_current_workspace(window, data) == ()
    workspace.stack.setCurrentWidget(workspace.my_platform_page)
    assert refresh_current_workspace(window, data) == ()


def test_same_revision_applies_after_modal_closes_and_auth_is_required(qtbot, tmp_path):
    window, workspace = fixture_window(qtbot, tmp_path)
    data = make_snapshot(7)
    modal = QDialog(workspace)
    qtbot.addWidget(modal)
    modal.setModal(True)
    modal.show()
    qtbot.waitUntil(lambda: modal.isVisible())
    assert publish(window, data) == ()
    modal.close()
    assert refresh_current_workspace(window, data) == ("今日记录", "今日提醒")
    wrong_actor = deepcopy(data)
    wrong_actor["actor_id"] = 8
    assert refresh_current_workspace(window, wrong_actor) == ()
    # Caller-provided fields cannot inject content into a valid mirror identity.
    caller_modified = deepcopy(data)
    caller_modified["resources"]["reminders"] = [{"title": "untrusted"}]
    assert refresh_current_workspace(window, caller_modified)
    assert "untrusted" not in workspace.today_page.reminders_table.item(0, 1).text()
    window.cloud_sync_service.mirror.mark_stale()
    assert refresh_current_workspace(window, data) == ()
    window.cloud_sync_service.mirror.revoke()
    assert refresh_current_workspace(window, data) == ()


def test_appointments_update_without_commerce_or_weather_requests(qtbot, tmp_path):
    window, workspace = fixture_window(qtbot, tmp_path)
    workspace.stack.setCurrentWidget(workspace.service_page)
    appointment = {
        "id": 40,
        "member_user_id": 7,
        "advisor_user_id": 9,
        "scheduled_at": beijing_now(),
        "status": "pending",
        "title": "合成预约",
        "address": "合成地址",
    }
    data = make_snapshot(7, {"appointments": [appointment]})
    assert publish(window, data) == ("会员服务", "上门预约")
    assert "预约中" in workspace.service_page.appointment_list.item(0).text()
    assert workspace.service_page.appointment_button.isHidden()
    appointment["status"] = "completed"
    data["revision"] = "two"
    publish(window, data)
    assert "已完成" in workspace.service_page.appointment_list.item(0).text()
    assert not workspace.service_page.appointment_button.isHidden()
    workspace.service_page.section_tabs.setCurrentIndex(1)
    assert refresh_current_workspace(window, data) == ()


def test_operator_lists_preserve_member_task_selection_and_never_fetch_detail(qtbot, tmp_path):
    window, workspace = fixture_window(qtbot, tmp_path, "operator")
    member = {"id": 22, "username": "合成会员", "display_name": "修改前"}
    item(workspace.member_list, 0, member)
    workspace.member_detail.setText("尚未重读的详情")
    data = make_snapshot(
        7,
        {"members": [{"id": 21, "username": "另一合成会员"}, {**member, "display_name": "修改后"}]},
    )
    assert publish(window, data) == ("运营会员列表",)
    assert workspace.member_list.currentItem().data(Qt.ItemDataRole.UserRole)["id"] == 22
    assert "修改后" in workspace.member_list.currentItem().text()
    assert workspace.member_detail.text() == "尚未重读的详情"
    data["resources"]["members"] = [{"id": 21, "username": "另一合成会员"}]
    data["revision"] = "two"
    publish(window, data)
    assert workspace.member_list.currentRow() == -1
    assert workspace.member_table.currentRow() == -1
    assert workspace.member_detail.text() == "请从左侧选择会员。"
    workspace.tabs.setCurrentIndex(1)
    task = {"id": 31, "member_user_id": 21, "status": "pending", "title": "合成任务"}
    item(workspace.task_list, 0, task)
    data["resources"]["appointments"] = [{**task, "status": "completed"}]
    data["revision"] = "three"
    assert publish(window, data) == ("顾问列表", "运营上门任务")
    assert workspace.task_list.currentItem().data(Qt.ItemDataRole.UserRole)["id"] == 31
    assert "已完成" in workspace.task_list.currentItem().text()


def test_advisor_binding_removal_clears_details_without_switching_member(qtbot, tmp_path):
    window, workspace = fixture_window(qtbot, tmp_path, "advisor")
    workspace.tabs.setCurrentIndex(1)
    member = {"id": 21, "advisor_user_id": 7, "username": "合成会员"}
    item(workspace.member_list, 0, member)
    workspace.profile_text.setPlainText("合成旧详情")
    data = make_snapshot(7, {"members": [member]})
    assert publish(window, data) == ("已绑定会员列表",)
    assert workspace.member_list.currentRow() == 0
    assert workspace.profile_text.toPlainText() == "合成旧详情"
    data["resources"]["members"] = [{"id": 22, "advisor_user_id": 7, "username": "另一个会员"}]
    data["revision"] = "two"
    publish(window, data)
    assert workspace.member_list.currentRow() == -1
    assert workspace.profile_text.toPlainText() == ""
    data["resources"]["members"][0]["advisor_user_id"] = 999
    data["revision"] = "three"
    assert publish(window, data) == ()


def test_statistics_preserve_filters_and_do_not_substitute_wrong_aggregate(qtbot, tmp_path):
    window, workspace = fixture_window(qtbot, tmp_path, "operator")
    workspace.tabs.setCurrentIndex(4)
    panel = workspace.performance_panel
    panel.advisor_filter.addItem("全部顾问", None)
    panel.member_filter.addItem("全部会员", None)
    data = make_snapshot(
        7,
        {
            "work_statistics": {
                "advisors": [
                    {
                        "advisor_user_id": 11,
                        "username": "合成顾问",
                        "completed_visit_record_count": 3,
                    }
                ]
            }
        },
    )
    assert publish(window, data) == ("顾问累计工作统计",)
    assert panel.table.item(0, 5).text() == "3"
    panel.date_filter.setChecked(True)
    old_date = panel.start_date.date()
    data["resources"]["work_statistics"]["advisors"][0]["completed_visit_record_count"] = 9
    data["revision"] = "two"
    assert publish(window, data) == ()
    assert panel.table.item(0, 5).text() == "3"
    assert panel.start_date.date() == old_date
    assert panel.date_filter.isChecked()
    assert "当前日期" in panel.status.text()


def test_member_statistics_snapshot_preserves_selected_parameters_and_unsaved_profile(
    qtbot, tmp_path
):
    window, workspace = fixture_window(qtbot, tmp_path)
    panel = workspace.profile_page
    workspace.stack.setCurrentWidget(panel)
    panel.category_combo.blockSignals(True)
    panel.category_combo.setCurrentIndex(panel.category_combo.findData("water"))
    panel.category_combo.blockSignals(False)
    panel.parameter_combo.blockSignals(True)
    panel.parameter_combo.clear()
    panel.parameter_combo.addItem("饮水量", "amount_ml")
    panel.parameter_combo.blockSignals(False)
    workspace.profile_editor.full_name_input.setText("尚未保存的表单姓名")
    data = make_snapshot(
        7,
        {
            "life_records": [
                {
                    "id": 1,
                    "user_id": 7,
                    "category": "water",
                    "content": "合成饮水",
                    "occurred_at": beijing_now(),
                    "details": {"amount_ml": 650},
                }
            ]
        },
    )
    assert publish(window, data) == ("档案摘要", "记录统计")
    assert panel.category_combo.currentData() == "water"
    assert panel.parameter_combo.currentData() == "amount_ml"
    assert "650" in panel.table.item(panel.table.rowCount() - 1, 1).text()
    assert workspace.profile_editor.full_name_input.text() == "尚未保存的表单姓名"


def test_today_large_font_small_screen_scrolls_without_overlapping_sections(qtbot, tmp_path):
    _, workspace = fixture_window(qtbot, tmp_path)
    workspace.setStyleSheet(build_style(24, "sage", 100))
    workspace.resize(1366, 680)
    workspace.show()
    qtbot.waitExposed(workspace)
    page = workspace.today_page
    assert page.content_scroll.verticalScrollBar().maximum() > 0
    assert page.records_table.geometry().top() > page.reminders_table.geometry().bottom()
    page.content_scroll.verticalScrollBar().setValue(
        page.content_scroll.verticalScrollBar().maximum()
    )
    assert page.content_scroll.verticalScrollBar().value() > 0
