from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import DEEPSEEK_DEFAULT_MODEL, DEFAULT_LOCAL_MODEL
from ..providers.demo_life import DEMO_MODEL, DemoLifeProvider
from ..providers.local_ollama import LocalOllamaProvider
from ..providers.ollama_cloud import OllamaCloudProvider
from ..services.ai_provider_resolver import resolve_deepseek
from ..services.ai_runtime import ConfiguredAIProvider, ai_settings
from ..workers.task import FunctionTask
from .ai_proposals_panel import AiProposalsPanel
from .cloud_key_dialog import CloudKeyDialog
from .deepseek_key_dialog import DeepSeekKeyDialog


class AdvisorAiPanel(QWidget):
    """Generate and confirm bounded AI work summaries for an active advisor."""

    def __init__(
        self,
        ai_service: object | None,
        secret_store: object | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ai_service = ai_service
        self.secret_store = secret_store
        self.advisor_user_id: int | None = None
        self._tasks: list[FunctionTask] = []
        self._session_keys: dict[str, str] = {}
        self._persistent_identity: str | None = None
        self._developer_snapshot = {}

        root = QVBoxLayout(self)
        hint = QLabel(
            "AI 只读取所选会员已确认的档案事实、近期生活记录和当前服务任务，"
            "先生成顾问摘要草稿；只有当前绑定顾问确认后才成为正式摘要。"
            "摘要日期和时间按右上角所选时区显示，默认北京时间。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("AdvisorHint")
        root.addWidget(hint)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("会员"))
        self.member_combo = QComboBox()
        self.member_combo.currentIndexChanged.connect(self.refresh_drafts)
        controls.addWidget(self.member_combo, 1)
        controls.addWidget(QLabel("AI 模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("本地 Ollama", "local")
        self.mode_combo.addItem("Ollama Cloud", "ollama_cloud")
        self.mode_combo.addItem("DeepSeek Cloud", "deepseek_cloud")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        controls.addWidget(self.mode_combo)
        controls.addWidget(QLabel("模型"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItem(DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_MODEL)
        self.model_combo.setMinimumWidth(220)
        controls.addWidget(self.model_combo)
        self.key_button = QPushButton("API Key")
        self.key_button.hide()
        self.key_button.clicked.connect(self._manage_key)
        controls.addWidget(self.key_button)
        self.generate_button = QPushButton("生成摘要草稿")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self.generate_summary)
        controls.addWidget(self.generate_button)
        root.addLayout(controls)
        self.demo_checkbox = QCheckBox("无 Key 演示（本机规则模拟，非 DeepSeek）")
        root.addWidget(self.demo_checkbox)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)
        self.proposals = AiProposalsPanel()
        self.proposals.refresh_requested.connect(self.refresh_drafts)
        self.proposals.confirm_requested.connect(self.confirm_proposal)
        self.proposals.dismiss_requested.connect(self.dismiss_proposal)
        root.addWidget(self.proposals, 1)
        self._refresh_enabled()

    def start_session(
        self,
        advisor_user_id: int,
        members: Sequence[Mapping[str, Any]],
        *,
        account: Any | None = None,
    ) -> None:
        self.advisor_user_id = int(advisor_user_id)
        # A bare row number can be reused after an import. Persistent keys must
        # also bind to the stable account attributes, in the store's namespace.
        self._persistent_identity = None
        if (
            account is not None
            and getattr(account, "id", None) == self.advisor_user_id
            and getattr(account, "username", None)
            and getattr(account, "created_at", None)
        ):
            identity = "|".join(
                str(getattr(account, name)) for name in ("id", "username", "created_at")
            )
            self._persistent_identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self._session_keys.clear()
        self.demo_checkbox.setChecked(False)
        self.set_members(members)
        self.refresh_drafts()

    def end_session(self) -> None:
        self.advisor_user_id = None
        self._persistent_identity = None
        self._session_keys.clear()
        self.demo_checkbox.setChecked(False)
        self.member_combo.clear()
        self.proposals.set_drafts([])
        self._refresh_enabled()

    def set_members(self, members: Sequence[Mapping[str, Any]]) -> None:
        selected = self.member_combo.currentData()
        self.member_combo.blockSignals(True)
        self.member_combo.clear()
        selected_index = -1
        for index, member in enumerate(members):
            member_id = int(member["id"])
            name = str(member.get("display_name") or member.get("username") or "会员")
            self.member_combo.addItem(name, member_id)
            if selected is not None and member_id == int(selected):
                selected_index = index
        if self.member_combo.count():
            self.member_combo.setCurrentIndex(max(0, selected_index))
        self.member_combo.blockSignals(False)
        self._refresh_enabled()

    def _mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData())
        self.model_combo.clear()
        self.model_combo.setEditable(mode != "deepseek_cloud")
        self.key_button.setVisible(mode != "local")
        if mode == "local":
            self.model_combo.addItem(DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_MODEL)
            self.key_button.setText("API Key")
        elif mode == "ollama_cloud":
            self.model_combo.addItem("验证 Key 后加载模型", None)
            self.key_button.setText("Ollama Key")
        else:
            self.model_combo.addItem(DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_MODEL)
            self.key_button.setText("DeepSeek Key")

    def _manage_key(self) -> bool:
        if self.advisor_user_id is None or self.secret_store is None:
            return False
        mode = str(self.mode_combo.currentData())
        if mode == "local":
            return True
        try:
            current = self._read_key(mode)
        except Exception:
            self._set_status("无法安全读取当前账号的 Key，请检查本机加密配置。", True)
            return False
        dialog = (
            DeepSeekKeyDialog(current, self)
            if mode == "deepseek_cloud"
            else CloudKeyDialog(current, self)
        )
        if mode == "deepseek_cloud" and self._persistent_identity is None:
            dialog.persist_checkbox.setEnabled(False)
            dialog.persist_checkbox.setToolTip("当前未取得稳定账号标识，仅在本次运行内使用。")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        if dialog.remove_requested:
            try:
                if mode == "deepseek_cloud" and self._persistent_identity is not None:
                    self.secret_store.delete_persistent_api_key(self._persistent_identity, mode)
                self.secret_store.delete_api_key(self.advisor_user_id, mode)
            except Exception:
                self._set_status("无法清除加密 Key，请检查本机配置后重试。", True)
                return False
            self._session_keys.pop(mode, None)
            self._set_status("已清除本账号的个人 API Key；不会撤销官网上的 Key。", False)
            return False
        if not dialog.api_key:
            return bool(current)
        remember = (
            mode == "deepseek_cloud"
            and sys.platform == "win32"
            and self._persistent_identity is not None
            and getattr(dialog, "persist_key", False)
        )
        try:
            if remember:
                self.secret_store.save_persistent_api_key(
                    self._persistent_identity, dialog.api_key, mode
                )
            self.secret_store.set_api_key(self.advisor_user_id, dialog.api_key, mode)
        except Exception:
            self._set_status("未能安全保存 Key；不会保存明文，请重试。", True)
            return False
        self._session_keys[mode] = dialog.api_key
        if mode == "ollama_cloud":
            self.model_combo.clear()
            for model in dialog.models:
                self.model_combo.addItem(model, model)
        self._set_status(
            "API Key 已用 Windows 当前账户加密保存。"
            if remember else "API Key 已在本次运行内存中就绪；未改变原有加密配置。",
            False,
        )
        return True

    def _read_key(self, mode: str) -> str | None:
        key = self._session_keys.get(mode)
        if self.secret_store is not None and self.advisor_user_id is not None:
            key = key or self.secret_store.get_api_key(self.advisor_user_id, mode)
            if (
                not key
                and mode == "deepseek_cloud"
                and sys.platform == "win32"
                and self._persistent_identity is not None
            ):
                key = self.secret_store.get_persistent_api_key(self._persistent_identity, mode)
        return key

    def apply_developer_snapshot(self, snapshot):
        self._developer_snapshot = deepcopy(snapshot)

    def _provider(self) -> tuple[object, str, str] | None:
        settings = ai_settings(self._developer_snapshot)
        if (settings.get("enabled") is False
                or self._developer_snapshot.get("settings", {}).get("features", {}).get("ai")
                is False):
            self._set_status("本次启动的配置已关闭 AI 服务。", True)
            return None
        if self.demo_checkbox.isChecked():
            return DemoLifeProvider(), "workflow_demo", DEMO_MODEL
        mode = str(self.mode_combo.currentData())
        if mode == "local":
            model = self.model_combo.currentText().strip()
            if not model:
                self._set_status("请填写本地 Ollama 模型名称。", True)
                return None
            return LocalOllamaProvider(), "ollama_local", model
        if mode == "deepseek_cloud" and self.secret_store is not None:
            try:
                provider = resolve_deepseek(
                    self.secret_store, self._persistent_identity or str(self.advisor_user_id),
                    session_key=self._read_key(mode),
                    cloud_client=getattr(self.ai_service, "client", None),
                )
                if provider is not None:
                    if settings:
                        provider = ConfiguredAIProvider(provider, self._developer_snapshot)
                    return provider, mode, settings.get("model", DEEPSEEK_DEFAULT_MODEL)
            except Exception:
                self._set_status("无法安全读取 AI 配置，请检查连接或密钥。", True)
                return None
        try:
            key = self._read_key(mode)
        except Exception:
            self._set_status("无法安全读取当前账号的 Key，请检查本机加密配置。", True)
            return None
        if not key and not self._manage_key():
            self._set_status("使用云端 AI 前需要输入自己的 API Key。", True)
            return None
        key = self._read_key(mode)
        if not key:
            return None
        if mode == "ollama_cloud":
            model = self.model_combo.currentData() or self.model_combo.currentText().strip()
            if not model:
                self._set_status("请先验证 Key 并选择云端模型。", True)
                return None
            return OllamaCloudProvider(key), mode, str(model)
        from ..providers.deepseek_cloud import DeepSeekCloudProvider

        provider = DeepSeekCloudProvider(key)
        if settings:
            provider = ConfiguredAIProvider(provider, self._developer_snapshot)
        return provider, mode, settings.get("model", DEEPSEEK_DEFAULT_MODEL)

    def generate_summary(self) -> None:
        member_id = self.member_combo.currentData()
        if (
            self.advisor_user_id is None
            or member_id is None
            or self.ai_service is None
            or self._tasks
        ):
            return
        selection = self._provider()
        if selection is None:
            return
        provider, provider_name, model = selection
        self._set_busy(True)
        self._set_status("AI 正在根据已确认事实生成一份待确认摘要…", False)
        task = FunctionTask(
            self.ai_service.create_advisor_summary_draft,
            self.advisor_user_id,
            int(member_id),
            provider,
            provider_name,
            model,
        )
        self._tasks.append(task)
        task.signals.result.connect(lambda _result: self._generation_succeeded())
        task.signals.error.connect(lambda error: self._set_status(str(error), True))
        task.signals.finished.connect(lambda: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _generation_succeeded(self) -> None:
        self.refresh_drafts()
        self._set_status("顾问摘要草稿已生成；请核对后逐项确认或忽略。", False)

    def refresh_drafts(self) -> None:
        member_id = self.member_combo.currentData()
        if self.advisor_user_id is None or member_id is None or self.ai_service is None:
            self.proposals.set_drafts([])
            return
        try:
            drafts = self.ai_service.list_drafts(
                self.advisor_user_id,
                member_user_id=int(member_id),
                include_resolved=True,
            )
            self.proposals.set_drafts(drafts)
        except Exception as error:
            self.proposals.set_error(str(error))

    def confirm_proposal(self, insight_id: int, proposal_id: int) -> None:
        if self.advisor_user_id is None or self.ai_service is None:
            return
        answer = QMessageBox.warning(
            self,
            "确认顾问摘要",
            "请确认摘要与会员事实和服务记录一致；确认后才成为正式顾问摘要。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ai_service.confirm_proposal(self.advisor_user_id, insight_id, proposal_id)
            self.refresh_drafts()
        except Exception as error:
            self.proposals.set_error(str(error))

    def dismiss_proposal(self, insight_id: int, proposal_id: int) -> None:
        if self.advisor_user_id is None or self.ai_service is None:
            return
        try:
            self.ai_service.dismiss_proposal(self.advisor_user_id, insight_id, proposal_id)
            self.refresh_drafts()
        except Exception as error:
            self.proposals.set_error(str(error))

    def _task_finished(self, task: FunctionTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.generate_button.setDisabled(busy or self.ai_service is None)
        self.member_combo.setDisabled(busy)
        self.mode_combo.setDisabled(busy)
        self.demo_checkbox.setDisabled(busy)
        self.model_combo.setDisabled(busy)
        self.key_button.setDisabled(busy)

    def _refresh_enabled(self) -> None:
        enabled = (
            self.ai_service is not None
            and self.advisor_user_id is not None
            and self.member_combo.count() > 0
        )
        self.generate_button.setEnabled(enabled)

    def _set_status(self, text: str, error: bool) -> None:
        self.status_label.setObjectName("AdvisorStatusError" if error else "AdvisorStatusSuccess")
        self.status_label.setText(text)
        self.status_label.show()
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


__all__ = ["AdvisorAiPanel"]
