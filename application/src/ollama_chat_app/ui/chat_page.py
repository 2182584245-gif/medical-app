from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    CLOUD_MODEL_PLACEHOLDER,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_VISION_MODEL,
    DEFAULT_LOCAL_MODEL,
    MAX_CONTEXT_MESSAGES,
    MAX_MESSAGE_LENGTH,
)
from ..providers.deepseek_cloud import DeepSeekCloudProvider
from ..providers.demo_life import DEMO_MODEL, DemoLifeProvider
from ..providers.local_ollama import LocalOllamaProvider
from ..providers.ollama_cloud import OllamaCloudProvider
from ..security.secret_store import SecretStore
from ..services.backup import PortableBackupService
from ..services.chat import ChatService, ChatTurn
from ..services.chat_attachments import (
    ChatAttachment,
    prepare_attachment,
    prepare_attachment_bytes,
    validate_attachments,
)
from ..services.chat_personalization import needs_life_context, personalized_system_message
from ..services.life_agent import ConversationAgentProvider
from ..services.offline_outbox import QueuedOperation
from ..services.voice import OfflineSpeechRecognizer
from ..time_utils import beijing_now
from ..workers.task import FunctionTask
from .ai_proposals_panel import AiProposalsPanel
from .chat_settings_dialog import ChatSettingsDialog
from .cloud_key_dialog import CloudKeyDialog
from .deepseek_key_dialog import DeepSeekKeyDialog
from .meal_capture_dialog import MealCaptureDialog
from .message_list import MessageList
from .sleep_check_dialog import SleepCheckDialog
from .voice_input import VoiceInputController


class ChatPage(QWidget):
    logout_requested = Signal()
    data_changed = Signal()
    preferences_changed = Signal(dict)
    stream_progress = Signal(object)

    def __init__(
        self,
        chat_service: ChatService,
        secret_store: SecretStore,
        backup_service: PortableBackupService | None = None,
        ai_assistant_service: object | None = None,
        file_management_service: object | None = None,
        preferences_service: object | None = None,
    ) -> None:
        super().__init__()
        self.chat_service = chat_service
        self.secret_store = secret_store
        self.backup_service = backup_service
        self.ai_assistant_service = ai_assistant_service
        self.file_management_service = file_management_service
        self.preferences_service = preferences_service
        self._preferences: dict[str, object] = {}
        self._cancel_event = threading.Event()
        self.stream_progress.connect(self._stream_update)
        self.current_user: Any | None = None
        self._session_keys: dict[str, str] = {}
        self._request_running = False
        self._backup_running = False
        self._voice_processing = False
        self._last_voice_text = ""
        self._attachment_processing = False
        self.current_conversation_id: int | None = None
        self._session_generation = 0
        self._conversation_drafts: dict[int, tuple[str, tuple[ChatAttachment, ...]]] = {}
        self._pending_attachments: list[ChatAttachment] = []
        self._tasks: list[FunctionTask] = []
        self._previous_mode_index = 0
        self._pending_image_ids: list[int] = []
        self._pending_image_names: list[str] = []
        self._draft_dialog: QDialog | None = None
        self._draft_panel: AiProposalsPanel | None = None
        self.voice_recognizer = OfflineSpeechRecognizer()
        self.voice_controller = VoiceInputController(self)
        self.voice_controller.recording_started.connect(self._voice_recording_started)
        self.voice_controller.recording_stopped.connect(self._voice_recording_stopped)
        self.voice_controller.recording_failed.connect(self._voice_failed)
        self.voice_controller.remaining_seconds_changed.connect(
            self._voice_remaining_seconds_changed
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("Card")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(10)

        self.user_label = QLabel("未登录")
        self.user_label.setStyleSheet("font-weight: 600;")
        self.user_label.setMaximumWidth(360)
        header_layout.addWidget(self.user_label)
        header_layout.addStretch(1)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("本地 Ollama", "local")
        self.mode_combo.addItem("Ollama Cloud", "ollama_cloud")
        self.mode_combo.addItem("DeepSeek Cloud", "deepseek_cloud")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.mode_combo.setParent(self)
        self.mode_combo.hide()

        self.model_stack = QStackedWidget()
        self.model_stack.setParent(self)

        self.local_model_combo = QComboBox()
        self.local_model_combo.setEditable(True)
        self.local_model_combo.addItem(DEFAULT_LOCAL_MODEL)
        self.local_model_combo.setToolTip(
            "本地模式暂不检测 Ollama 或读取模型列表，请直接填写已经安装的模型名称。"
        )
        self.model_stack.addWidget(self.local_model_combo)

        self.cloud_model_combo = QComboBox()
        self.cloud_model_combo.addItem(CLOUD_MODEL_PLACEHOLDER, None)
        self.cloud_model_combo.setToolTip(
            "登录后动态读取当前账户模型；免费 Starter 名单和额度以 Ollama 当前规则为准。"
        )
        self.model_stack.addWidget(self.cloud_model_combo)

        self.deepseek_model_combo = QComboBox()
        self.deepseek_model_combo.addItem(DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_MODEL)
        self.deepseek_model_combo.addItem(DEEPSEEK_VISION_MODEL, DEEPSEEK_VISION_MODEL)
        self.deepseek_model_combo.setToolTip(
            "文字使用 deepseek-v4-flash；当前上下文含图片时自动使用 DeepSeek 视觉模型。"
        )
        self.model_stack.addWidget(self.deepseek_model_combo)
        self.model_stack.hide()

        self.key_button = QPushButton("更换 APIKEY")
        self.key_button.clicked.connect(self._manage_cloud_key)
        self.key_button.hide()
        header_layout.addWidget(self.key_button)

        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self._open_settings)
        header_layout.addWidget(self.settings_button)

        self.drafts_button = QPushButton("AI 待确认")
        self.drafts_button.setToolTip("查看 AI 提出的记录、档案或提醒草稿，并逐项决定是否保存。")
        self.drafts_button.setEnabled(ai_assistant_service is not None)
        self.drafts_button.clicked.connect(self._open_ai_drafts)
        header_layout.addWidget(self.drafts_button)

        self.backup_button = QPushButton("导出数据")
        backup_tooltip = (
            "导出全部本地账户和聊天记录，以及档案和生活服务数据；不会包含 EXE、API Key 或模型。"
            if self.backup_service is not None
            else "当前运行方式没有启用数据迁移功能。"
        )
        self.backup_button.setToolTip(backup_tooltip)
        self.backup_button.clicked.connect(self._export_data_backup)
        self.backup_button.setEnabled(self.backup_service is not None)
        header_layout.addWidget(self.backup_button)
        self.backup_button.hide()

        self.import_button = QPushButton("导入数据")
        self.import_button.setToolTip(
            "从本应用导出的数据备份恢复全部本地业务数据；导入前会自动备份当前数据。"
        )
        self.import_button.clicked.connect(self._import_data_backup)
        self.import_button.setEnabled(self.backup_service is not None)
        header_layout.addWidget(self.import_button)
        self.import_button.hide()

        self.logout_button = QPushButton("退出登录")
        self.logout_button.clicked.connect(self.logout_requested)
        header_layout.addWidget(self.logout_button)
        self.logout_button.hide()
        root.addWidget(header)

        self.status_label = QLabel("本地模式不会自动检测 Ollama；发送消息时才会连接本机服务。")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        body = QHBoxLayout()
        sidebar = QFrame()
        sidebar.setObjectName("Card")
        sidebar.setFixedWidth(205)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.addWidget(QLabel("我的对话"))
        self.new_conversation_button = QPushButton("＋ 新建对话")
        self.new_conversation_button.clicked.connect(self._create_conversation)
        sidebar_layout.addWidget(self.new_conversation_button)
        self.conversation_list = QListWidget()
        self.conversation_list.currentItemChanged.connect(self._conversation_changed)
        sidebar_layout.addWidget(self.conversation_list, 1)
        self.rename_conversation_button = QPushButton("重命名对话")
        self.rename_conversation_button.clicked.connect(self._rename_conversation)
        sidebar_layout.addWidget(self.rename_conversation_button)
        body.addWidget(sidebar)
        self.message_list = MessageList()
        self.message_list.suggestion_selected.connect(self._send_suggestion)
        body.addWidget(self.message_list, 1)
        root.addLayout(body, 1)

        self.attachment_label = QLabel()
        self.attachment_label.setObjectName("Hint")
        self.attachment_label.setWordWrap(True)
        self.attachment_label.hide()
        root.addWidget(self.attachment_label)
        self.attachment_cards = QWidget()
        self.attachment_cards_layout = QHBoxLayout(self.attachment_cards)
        self.attachment_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_cards.hide()
        root.addWidget(self.attachment_cards)

        self.context_checkbox = QCheckBox("结合我的生活资料回答")
        self.context_checkbox.setChecked(False)
        self.context_checkbox.toggled.connect(self._save_context_consent)
        root.addWidget(self.context_checkbox)
        self.demo_checkbox = QCheckBox("无 Key 演示（本机规则模拟，非 DeepSeek）")
        self.demo_checkbox.setToolTip("勾选后不连接模型服务；可体验记录、偏好、提醒草稿及确认保存。")
        root.addWidget(self.demo_checkbox)
        self.speak_checkbox = QCheckBox("把回复读给我听（使用本机语音）")
        self.speak_checkbox.toggled.connect(
            lambda enabled: self._stop_speaking() if not enabled else None
        )
        root.addWidget(self.speak_checkbox)
        self._speech_output = None
        self.context_notice = QLabel(
            "发送生活相关问题时，会把本账号的有限档案、近期记录及提醒与本轮消息发送给所选 AI；"
            "称呼与回答偏好也会用于回复。可取消勾选。"
        )
        self.context_notice.setObjectName("Hint")
        self.context_notice.setWordWrap(True)
        self.context_checkbox.setToolTip(self.context_notice.text())
        # The first-use consent dialog explains the full scope. Keeping another
        # multi-line copy permanently visible crowds out chat on smaller screens.
        self.context_notice.hide()
        root.addWidget(self.context_notice)

        daily_actions = QHBoxLayout()
        daily_actions.setSpacing(12)
        self.meal_button = QPushButton("拍下这一餐")
        self.meal_button.clicked.connect(self._capture_meal)
        self.activity_button = QPushButton("记一下活动")
        self.activity_button.clicked.connect(self._activity_entry)
        self.sleep_button = QPushButton("早上报睡眠")
        self.sleep_button.clicked.connect(self._sleep_entry)
        for button in (self.meal_button, self.activity_button, self.sleep_button):
            button.setMinimumHeight(56)
            daily_actions.addWidget(button)
        root.addLayout(daily_actions)

        input_frame = QFrame()
        input_frame.setObjectName("Card")
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(12, 12, 12, 12)
        input_layout.setSpacing(10)

        self.message_input = QPlainTextEdit()
        self.message_input.setPlaceholderText("可以直接说话，也可以在这里打字；吃饭优先拍照")
        self.message_input.setMaximumBlockCount(500)
        self.message_input.setFixedHeight(76)
        input_layout.addWidget(self.message_input, 1)

        self.image_button = QPushButton("＋")
        self.image_button.setToolTip(
            "JPG、PNG、WEBP，每个不超过 10 MB；发送后随消息保存在本机。"
            "DeepSeek 会使用原生视觉模型，发送前会提示上传与计费。"
        )
        self.image_button.setFixedWidth(48)
        self.image_button.setFixedHeight(44)
        self.image_button.setEnabled(hasattr(chat_service, "database"))
        self.image_button.setAccessibleName("添加图片或文件")
        self.image_button.clicked.connect(self._choose_any_attachment)
        input_layout.addWidget(self.image_button)

        self.life_actions_button = QPushButton("生活动态生成")
        self.life_actions_button.setMinimumHeight(44)
        life_menu = QMenu(self.life_actions_button)
        for label, prompt in (("看看近期生活", "请总结最近的生活记录"),
                              ("设置生活提醒", "我想设置一个生活提醒")):
            action = life_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, text=prompt: self._send_suggestion(text)
            )
        self.life_actions_button.setMenu(life_menu)
        input_layout.addWidget(self.life_actions_button)

        self.file_button = QPushButton("附加文件")
        self.file_button.setToolTip(
            "DeepSeek 支持 PDF、TXT、Markdown、CSV、DOCX；每个最多 10 MB / 50000 字符，"
            "PDF 最多 100 页。只在本机提取文字，扫描 PDF 请附加原图。"
            "每条最多 4 个附件、合计 20 MB；每个账号聊天附件最多 100 MB。"
        )
        self.file_button.setEnabled(False)
        self.file_button.clicked.connect(self._choose_files)
        attachment_actions = QHBoxLayout()
        self.file_button.setParent(self)
        self.file_button.hide()
        self.clear_attachments_button = QPushButton("移除附件")
        self.clear_attachments_button.clicked.connect(self._clear_pending_images)
        self.clear_attachments_button.setEnabled(False)
        self.clear_attachments_button.setParent(self)
        self.clear_attachments_button.hide()
        attachment_hint = QLabel("每条最多 4 个附件 · 单个 10 MB · 文件文字在本机提取")
        attachment_hint.setObjectName("Hint")
        self.image_button.setToolTip(self.image_button.toolTip() + "\n" + attachment_hint.text())
        attachment_hint.hide()
        attachment_actions.addWidget(attachment_hint)
        attachment_actions.addStretch(1)
        root.addLayout(attachment_actions)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.setFixedWidth(92)
        self.send_button.setFixedHeight(44)
        self.send_button.clicked.connect(self.send_message)
        input_layout.addWidget(self.send_button)

        self.cancel_button = QPushButton("停止回答")
        self.cancel_button.setFixedHeight(44)
        self.cancel_button.clicked.connect(self._cancel_request)
        self.cancel_button.hide()
        input_layout.addWidget(self.cancel_button)

        self.structured_button = QPushButton("生成生活草稿")
        self.structured_button.setToolTip(
            "AI 会读取已确认的有限本地上下文；可能的记录、档案或提醒只保存为待确认草稿。"
        )
        self.structured_button.setFixedWidth(128)
        self.structured_button.setFixedHeight(44)
        self.structured_button.setEnabled(ai_assistant_service is not None)
        self.structured_button.clicked.connect(self.send_structured_message)
        self.structured_button.setParent(self)
        self.structured_button.hide()

        self.voice_button = QPushButton("开始语音")
        self.voice_button.setToolTip(
            "使用随应用附带的离线中文模型把麦克风语音转为文字；录音不会上传。"
        )
        self.voice_button.setFixedWidth(112)
        self.voice_button.setFixedHeight(44)
        self.voice_button.clicked.connect(self._toggle_voice_recording)
        self.voice_button.setMinimumHeight(56)
        daily_actions.insertWidget(0, self.voice_button)
        root.addWidget(input_frame)

        self.message_input.installEventFilter(self)

    def eventFilter(self, watched: object, event: object) -> bool:
        if (
            watched is self.message_input
            and hasattr(event, "key")
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.send_message()
            return True
        return super().eventFilter(watched, event)

    def start_session(self, user: Any) -> None:
        self._session_generation += 1
        self.current_user = user
        self.current_conversation_id = None
        self._conversation_drafts.clear()
        self._session_keys.clear()
        self._clear_pending_images()
        self._preferences = {}
        if self.preferences_service is not None:
            with suppress(Exception):
                self._preferences = dict(self.preferences_service.get(user.id))
        self.user_label.setText(f"生活助手 · {self._preferences.get('nickname') or user.username}")
        mode_index = self.mode_combo.findData(
            self._preferences.get("ai_provider", "deepseek_cloud")
        )
        mode_index = max(0, mode_index)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(mode_index)
        self.mode_combo.blockSignals(False)
        self._previous_mode_index = mode_index
        self.model_stack.setCurrentIndex(mode_index)
        self.key_button.setVisible(mode_index != 0)
        self.context_checkbox.blockSignals(True)
        self.context_checkbox.setChecked(bool(self._preferences.get("ai_context_consent", False)))
        self.context_checkbox.blockSignals(False)
        self._apply_model_preference()
        self.message_list.clear_messages()
        self.message_input.clear()
        self._set_status(
            "欢迎聊聊今天的生活。点击发送后开始回答，右上角可设置模型或更换 APIKEY。",
            "hint",
        )
        self._refresh_conversations()
        self._load_conversation_messages()
        self._refresh_busy_controls()
        self.message_input.setFocus()
        self._refresh_drafts()

    def _apply_model_preference(self) -> None:
        model = str(self._preferences.get("ai_model") or DEEPSEEK_DEFAULT_MODEL)
        provider = self.mode_combo.currentData()
        if provider == "deepseek_cloud":
            index = self.deepseek_model_combo.findData(model)
            self.deepseek_model_combo.setCurrentIndex(max(0, index))
        elif provider == "local":
            self.local_model_combo.setCurrentText(
                DEFAULT_LOCAL_MODEL if model.startswith("deepseek-") else model
            )
        elif model and not model.startswith("deepseek-"):
            index = self.cloud_model_combo.findData(model)
            if index < 0:
                self.cloud_model_combo.addItem(model, model)
                index = self.cloud_model_combo.count() - 1
            self.cloud_model_combo.setCurrentIndex(index)

    def _open_settings(self) -> None:
        if not self.current_user or self._request_running:
            return
        settings = {**self._preferences, "ai_provider": self.mode_combo.currentData()}
        dialog = ChatSettingsDialog(settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        patch = dialog.values()
        try:
            result = (self.preferences_service.update(self.current_user.id, patch)
                      if self.preferences_service is not None
                      else {**self._preferences, **patch})
            if isinstance(result, QueuedOperation):
                self._set_status("AI 设置已加密保存在本机待提交，联网确认后生效。", "info")
                return
            self._preferences = dict(result)
        except Exception:
            self._set_status("设置未能保存，请稍后重试。", "error")
            return
        index = max(0, self.mode_combo.findData(patch["ai_provider"]))
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        self._previous_mode_index = index
        self._show_mode_controls(str(patch["ai_provider"]))
        self._apply_model_preference()
        self.preferences_changed.emit(dict(self._preferences))
        self._set_status("AI 设置已保存。", "ok")

    def _send_suggestion(self, question: str) -> None:
        if self._request_running or not self.current_user:
            return
        self.message_input.setPlainText(question)
        self.send_message()

    def _save_context_consent(self, enabled: bool) -> None:
        if self.current_user is None:
            return
        patch = {"ai_context_consent": enabled, "ai_context_consent_decided": True}
        try:
            result = (self.preferences_service.update(self.current_user.id, patch)
                      if self.preferences_service is not None
                      else {**self._preferences, **patch})
            if isinstance(result, QueuedOperation):
                # A queued opt-out is an immediate local veto, never permission
                # to share. Other clients learn it only after server confirmation.
                self._preferences.update(ai_context_consent=False, ai_context_consent_decided=True)
                self.context_checkbox.setChecked(False)
                self._set_status("本机已停止分享；此选择已排队，联网确认后同步到云端。", "info")
                return
            self._preferences = dict(result)
        except Exception:
            self.context_checkbox.blockSignals(True)
            self.context_checkbox.setChecked(False)
            self.context_checkbox.blockSignals(False)
            self._preferences["ai_context_consent"] = False
            self._set_status("未能保存分享选择，本次不会发送生活资料。", "error")

    def _ensure_context_consent(self) -> bool:
        if self.context_checkbox.isChecked():
            return True
        if self._preferences.get("ai_context_consent_decided", False):
            return False
        answer = QMessageBox.question(
            self,
            "是否结合您的生活资料？",
            "为回答这个生活问题，是否允许把本账号的有限档案、近期生活记录和提醒"
            "发送给所选 AI？您设置的称呼及回答偏好也会用于回复。\n\n"
            "选择“否”仍可继续一般问答。之后可用输入框上方的勾选项改变选择。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        enabled = answer == QMessageBox.StandardButton.Yes
        self._save_context_consent(enabled)
        enabled = enabled and bool(self._preferences.get("ai_context_consent"))
        self.context_checkbox.blockSignals(True)
        self.context_checkbox.setChecked(enabled)
        self.context_checkbox.blockSignals(False)
        return enabled

    def _credential_identity(self) -> str:
        if self.current_user is None:
            raise ValueError("请先登录")
        identity = "|".join(
            str(getattr(self.current_user, name, "")) for name in ("id", "username", "created_at")
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _load_conversation_messages(self) -> None:
        if not self.current_user:
            return
        self.message_list.clear_messages()
        options = (
            {"conversation_id": self.current_conversation_id}
            if self.current_conversation_id is not None
            else {}
        )
        for message in self.chat_service.list_messages(self.current_user.id, **options):
            content = message.content
            if message.status == "error" and not content:
                content = "上一次回答失败：" + str(message.error_message or "请重试")
            if hasattr(self.chat_service, "list_message_attachments"):
                attachments = self.chat_service.list_message_attachments(
                    self.current_user.id, message.id
                )
                if attachments:
                    content += "\n\n附件：" + "、".join(
                        str(item["original_name"]) for item in attachments
                    )
            self.message_list.add_message(
                message.id,
                message.role,
                content,
                message.status,
            )

    def _refresh_conversations(self) -> None:
        if not self.current_user or not hasattr(self.chat_service, "list_conversations"):
            return
        conversations = self.chat_service.list_conversations(self.current_user.id)
        if self.current_conversation_id is None and conversations:
            self.current_conversation_id = conversations[0].id
        self.conversation_list.blockSignals(True)
        self.conversation_list.clear()
        for conversation in conversations:
            item = QListWidgetItem(conversation.title)
            item.setData(Qt.ItemDataRole.UserRole, conversation.id)
            item.setToolTip(conversation.title)
            self.conversation_list.addItem(item)
            if conversation.id == self.current_conversation_id:
                self.conversation_list.setCurrentItem(item)
        self.conversation_list.blockSignals(False)

    def _conversation_changed(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None or not self.current_user:
            return
        selected_id = int(item.data(Qt.ItemDataRole.UserRole))
        if selected_id == self.current_conversation_id:
            return
        if self._request_running or self._attachment_processing or self._voice_processing:
            self._refresh_conversations()
            return
        if self.current_conversation_id is not None:
            self._conversation_drafts[self.current_conversation_id] = (
                self.message_input.toPlainText(),
                tuple(self._pending_attachments),
            )
        self.current_conversation_id = selected_id
        self._clear_pending_images()
        draft, attachments = self._conversation_drafts.get(selected_id, ("", ()))
        self.message_input.setPlainText(draft)
        self._pending_attachments = list(attachments)
        self._refresh_attachment_label()
        self._load_conversation_messages()
        self._set_status("已切换对话；AI 只会读取当前对话最近的消息和对应附件。", "hint")

    def _create_conversation(self) -> None:
        if not self.current_user or self._request_running or self._attachment_processing:
            return
        try:
            conversation = self.chat_service.create_conversation(self.current_user.id)
            self._refresh_conversations()
            for index in range(self.conversation_list.count()):
                item = self.conversation_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == conversation.id:
                    self.conversation_list.setCurrentItem(item)
                    break
        except Exception as exc:
            self._set_status(f"新建对话失败：{exc}", "error")

    def _rename_conversation(self) -> None:
        if not self.current_user or self.current_conversation_id is None or self._request_running:
            return
        current = self.conversation_list.currentItem()
        title, accepted = QInputDialog.getText(
            self, "重命名对话", "对话名称（最多 80 字符）", text=current.text() if current else ""
        )
        if accepted:
            try:
                self.chat_service.rename_conversation(
                    self.current_user.id, self.current_conversation_id, title
                )
                self._refresh_conversations()
            except Exception as exc:
                self._set_status(str(exc), "error")

    def end_session(self) -> None:
        self._stop_speaking()
        self._session_generation += 1
        self.voice_controller.cancel()
        self.current_user = None
        self._session_keys.clear()
        self.user_label.setText("未登录")
        self.message_list.clear_messages()
        self.message_input.clear()
        self._clear_pending_images()
        self.current_conversation_id = None
        self._conversation_drafts.clear()
        self.conversation_list.clear()

    def _mode_changed(self, index: int) -> None:
        if self._request_running:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(self._previous_mode_index)
            self.mode_combo.blockSignals(False)
            return

        mode = str(self.mode_combo.itemData(index))
        self._show_mode_controls(mode)

        if mode == "local":
            self._previous_mode_index = index
            self._set_status(
                "本地模式不会自动检测 Ollama；发送消息时才会连接本机服务。",
                "hint",
            )
            return

        if not self._ensure_cloud_key(mode):
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(self._previous_mode_index)
            self.mode_combo.blockSignals(False)
            previous_mode = str(self.mode_combo.itemData(self._previous_mode_index))
            self._show_mode_controls(previous_mode)
            return

        self._previous_mode_index = index
        if mode == "ollama_cloud":
            self._load_cloud_models()
        else:
            self._set_status(
                f"DeepSeek API Key 已就绪，将使用 {DEEPSEEK_DEFAULT_MODEL}。",
                "ok",
            )

    def _show_mode_controls(self, mode: str) -> None:
        stack_indexes = {"local": 0, "ollama_cloud": 1, "deepseek_cloud": 2}
        self.model_stack.setCurrentIndex(stack_indexes.get(mode, 0))
        self.key_button.setVisible(mode != "local")
        self.key_button.setText("更换 APIKEY")
        self._refresh_busy_controls()

    def _safe_get_session_key(self, provider: str) -> str | None:
        if not self.current_user:
            return None
        try:
            current = self.secret_store.get_api_key(self.current_user.id, provider)
            if current:
                return current
            if provider == "deepseek_cloud" and hasattr(self.secret_store, "get_effective_api_key"):
                return self.secret_store.get_effective_api_key(
                    self._credential_identity(), provider
                )
            return None
        except Exception as exc:
            self._show_warning("无法读取 API Key", str(exc))
            return None

    def _cloud_key(self, provider: str = "ollama_cloud") -> str | None:
        return self._session_keys.get(provider) or self._safe_get_session_key(provider)

    def _ensure_cloud_key(self, provider: str) -> bool:
        if self._cloud_key(provider):
            return True
        return self._open_key_dialog(provider)

    def _manage_cloud_key(self) -> None:
        provider = str(self.mode_combo.currentData())
        if provider != "local":
            self._open_key_dialog(provider)

    def _export_data_backup(self) -> None:
        if not self.current_user or not self.backup_service or self._backup_running:
            return
        if self._request_running:
            self._set_status("请等待当前 AI 回答完成后再导出数据。", "error")
            return

        answer = QMessageBox.warning(
            self,
            "导出全部本地数据",
            "数据备份将包含本机应用中的全部账户、密码哈希和聊天记录。\n\n"
            "备份没有加密，任何拿到 ZIP 的人都可能读取聊天正文；它不会包含 "
            "EXE、云端 API Key、本地 Ollama 程序或模型。\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        download_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        target_dir = Path(download_dir) if download_dir else Path.home()
        timestamp = beijing_now().strftime("%Y%m%d-%H%M%S")
        suggested_path = target_dir / f"健康生活服务平台-数据备份-{timestamp}.zip"
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存数据备份",
            str(suggested_path),
            "ZIP 压缩包 (*.zip)",
        )
        if not selected:
            return

        destination = Path(selected).expanduser()
        if destination.suffix.lower() != ".zip":
            destination = Path(f"{destination}.zip")

        self._set_backup_running(True)
        self._set_status("正在安全导出全部本地数据，请不要关闭应用…", "busy")
        task = FunctionTask(self.backup_service.create_archive, destination)
        self._start_task(
            task,
            self._data_export_succeeded,
            self._data_export_failed,
            lambda: self._set_backup_running(False),
        )

    # Kept as a small compatibility shim for older UI tests or integrations.
    def _create_portable_archive(self) -> None:
        self._export_data_backup()

    def _data_export_succeeded(self, result: object) -> None:
        path = Path(getattr(result, "path", ""))
        size_bytes = int(getattr(result, "size_bytes", 0))
        size_mb = size_bytes / (1024 * 1024)
        self._set_status(f"数据备份已保存：{path.name}", "ok")
        QMessageBox.information(
            self,
            "导出完成",
            f"数据备份已经保存：\n{path}\n\n大小约 {size_mb:.1f} MB。\n\n"
            "在另一台电脑安装本应用后，请使用“导入数据”选择此 ZIP。"
            "云端 API Key 需要重新输入，本地 Ollama 和模型也需要另行安装。",
        )

    def _data_export_failed(self, error: object) -> None:
        message = str(error) or "导出数据备份失败。"
        self._set_status(message, "error")
        QMessageBox.warning(self, "导出失败", message)

    def _import_data_backup(self) -> None:
        if not self.current_user or not self.backup_service or self._backup_running:
            return
        if self._request_running:
            self._set_status("请等待当前 AI 回答完成后再导入数据。", "error")
            return

        download_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        start_dir = Path(download_dir) if download_dir else Path.home()
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择数据备份",
            str(start_dir),
            "ZIP 压缩包 (*.zip)",
        )
        if not selected:
            return

        answer = QMessageBox.warning(
            self,
            "确认导入数据",
            "导入后，当前应用中的全部账户、聊天与健康生活数据会被所选备份替换。\n\n"
            "应用会先自动保存一份当前数据备份；如果检查或导入失败，当前数据"
            "不会被替换。所选 ZIP 必须由本应用的“导出数据”功能生成。\n\n"
            "云端 API Key 不会随备份迁移，导入后需要重新登录。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._set_backup_running(True)
        self._set_status("正在检查备份并导入数据，请不要关闭应用…", "busy")
        task = FunctionTask(self.backup_service.import_archive, Path(selected).expanduser())
        self._start_task(
            task,
            self._data_import_succeeded,
            self._data_import_failed,
            lambda: self._set_backup_running(False),
        )

    def _data_import_succeeded(self, result: object) -> None:
        safety_backup = Path(getattr(result, "safety_backup_path", ""))
        user_count = int(getattr(result, "user_count", 0))
        message_count = int(getattr(result, "message_count", 0))
        # Imported users can reuse numeric IDs from the previous database. Clear
        # all process-memory keys so one person's key can never become associated
        # with a different imported account.
        self.secret_store.clear_api_keys()
        self._session_keys.clear()
        self._set_status("数据已导入，即将返回登录页。", "ok")
        QMessageBox.information(
            self,
            "导入完成",
            f"已导入 {user_count} 个账户、{message_count} 条聊天消息。\n\n"
            f"导入前的当前数据已自动保存到：\n{safety_backup}\n\n"
            "现在会返回登录页以安全刷新数据。请使用备份中的账户登录；"
            "云端 API Key 需要重新输入。",
        )
        self.logout_requested.emit()

    def _data_import_failed(self, error: object) -> None:
        message = str(error) or "导入数据备份失败，当前数据没有被替换。"
        self._set_status(message, "error")
        QMessageBox.warning(self, "导入失败", message)

    def _open_key_dialog(self, provider: str = "ollama_cloud") -> bool:
        if not self.current_user:
            return False
        current_key = self._cloud_key(provider)
        dialog = (
            DeepSeekKeyDialog(current_key, self)
            if provider == "deepseek_cloud"
            else CloudKeyDialog(current_key, self)
        )
        dialog.key_input.setProperty("sensitive_input", True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        if dialog.remove_requested:
            try:
                if provider == "deepseek_cloud" and hasattr(
                    self.secret_store, "delete_persistent_api_key"
                ):
                    self.secret_store.delete_persistent_api_key(
                        self._credential_identity(), provider
                    )
                self.secret_store.delete_api_key(self.current_user.id, provider)
            except Exception as exc:
                self._show_warning("删除失败", str(exc))
                return False
            self._session_keys.pop(provider, None)
            if provider == "ollama_cloud":
                self.cloud_model_combo.clear()
                self.cloud_model_combo.addItem(CLOUD_MODEL_PLACEHOLDER, None)
            self._set_status(
                "已移除本账号的个人 API Key；若已配置本机默认 Key，将恢复使用默认 Key。", "ok"
            )
            return False

        if not dialog.api_key:
            return bool(current_key)

        self._session_keys[provider] = dialog.api_key
        try:
            if (provider == "deepseek_cloud" and getattr(dialog, "persist_key", True)
                    and hasattr(self.secret_store, "save_persistent_api_key")):
                self.secret_store.save_persistent_api_key(
                    self._credential_identity(), dialog.api_key, provider
                )
            self.secret_store.set_api_key(self.current_user.id, dialog.api_key, provider)
        except Exception as exc:
            self._session_keys.pop(provider, None)
            self._show_warning("无法保存 API Key", str(exc))
            return False
        if provider == "ollama_cloud":
            self._populate_cloud_models(dialog.models)
            self._set_status("Ollama API Key 验证成功，已加载当前账户的云端模型。", "ok")
        else:
            self._set_status(
                f"DeepSeek API Key 验证成功，将使用 {DEEPSEEK_DEFAULT_MODEL}。",
                "ok",
            )
        return True

    def _load_cloud_models(self) -> None:
        key = self._cloud_key("ollama_cloud")
        if not key:
            return
        self.cloud_model_combo.clear()
        self.cloud_model_combo.addItem("正在加载云端模型…", None)
        self.cloud_model_combo.setDisabled(True)
        self._set_status("正在读取当前 Ollama 账户可用的云端模型…", "busy")

        task = FunctionTask(OllamaCloudProvider(key).list_models)
        self._start_task(
            task,
            self._populate_cloud_models,
            lambda error: self._cloud_models_failed(error),
        )

    def _populate_cloud_models(self, models: object) -> None:
        values = [str(model) for model in models] if isinstance(models, list) else []
        self.cloud_model_combo.clear()
        for model in values:
            self.cloud_model_combo.addItem(model, model)
        self.cloud_model_combo.setDisabled(False)
        if values:
            self.cloud_model_combo.setCurrentIndex(0)
            self._set_status(
                "已默认选择当前账户返回的首个可用模型；免费 Starter 范围和额度以 Ollama 账户为准。",
                "ok",
            )
        else:
            self.cloud_model_combo.addItem(CLOUD_MODEL_PLACEHOLDER, None)
            self._set_status("当前账户没有返回可用的云端模型。", "error")

    def _cloud_models_failed(self, error: object) -> None:
        self.cloud_model_combo.clear()
        self.cloud_model_combo.addItem(CLOUD_MODEL_PLACEHOLDER, None)
        self.cloud_model_combo.setDisabled(False)
        self._set_status(str(error) or "读取云端模型失败。", "error")

    def _stop_speaking(self) -> None:
        if self._speech_output is not None:
            self._speech_output.stop()

    def _speak_answer(self, text: str) -> None:
        if not self.speak_checkbox.isChecked():
            return
        try:
            from PySide6.QtCore import QLocale
            from PySide6.QtTextToSpeech import QTextToSpeech

            if self._speech_output is None:
                self._speech_output = QTextToSpeech(self)
                self._speech_output.setLocale(QLocale("zh_CN"))
            if self._speech_output.state() == QTextToSpeech.State.Error:
                self._set_status("本机语音朗读不可用，回答已保留为文字。", "hint")
                return
            self._speech_output.say(text[:4000])
        except Exception:
            self._set_status("本机没有可用的中文朗读组件，回答已保留为文字。", "hint")

    def _activity_entry(self) -> None:
        self.message_input.setPlaceholderText("例如：我现在出去散步了；没有时长也可以先记录")
        self.message_input.setFocus()
        self._set_status("点“开始语音”说一句就好，也可以打字；时长不清楚会留空。", "hint")

    def _sleep_entry(self) -> None:
        dialog = SleepCheckDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            existing = self.message_input.toPlainText().strip()
            self.message_input.setPlainText((existing + "\n" + dialog.prompt()).strip())
            self._set_status("已整理您自报的入睡和起床时间，点击发送后核对记录。", "ok")

    def _capture_meal(self) -> None:
        if len(self._pending_attachments) >= 4:
            self._set_status("一条消息最多附加 4 张照片，请先发送或移除附件。", "error")
            return
        dialog = MealCaptureDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            attachment = (prepare_attachment(dialog.file_path) if dialog.file_path else
                          prepare_attachment_bytes("这一餐.jpg", dialog.image_bytes))
            result = [*self._pending_attachments, attachment]
            validate_attachments(result)
            self._pending_attachments = result
            self._refresh_attachment_label()
            instruction = (
                "这是我这一餐的照片。请识别照片里看得见的食物，整理饮食草稿让我确认。"
                "看不清楚请问我，不要猜重量、热量、配料或是否已经吃完。"
            )
            existing = self.message_input.toPlainText().strip()
            self.message_input.setPlainText((existing + "\n" + instruction).strip())
            self._set_status("餐食照片已就绪，点击发送识别；结果确认后才保存。", "ok")
        except Exception as exc:
            self._set_status(f"照片未能添加：{exc}", "error")

    def _choose_images(self) -> None:
        self._choose_attachments(images_only=True)

    def _choose_any_attachment(self) -> None:
        self._choose_attachments(images_only=False)

    def _choose_files(self) -> None:
        if self.mode_combo.currentData() != "deepseek_cloud":
            self._set_status("文件附件仅在 DeepSeek 模式下使用。", "error")
            return
        self._choose_attachments(images_only=False)

    def _choose_attachments(self, *, images_only: bool) -> None:
        if (
            not self.current_user
            or self._request_running
            or self._attachment_processing
            or self._backup_running
        ):
            return
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片或文件（自动识别类型，单个不超过 10 MB）",
            str(Path.home()),
            "图片 (*.jpg *.jpeg *.png *.webp)"
            if images_only
            else "图片与文件 (*.jpg *.jpeg *.png *.webp *.pdf *.txt *.md *.markdown *.csv *.docx)",
        )
        if not selected:
            return
        if len(selected) + len(self._pending_attachments) > 4:
            self._set_status("一条消息最多附加 4 个图片或文件。", "error")
            return
        existing = tuple(self._pending_attachments)
        generation = self._session_generation

        def prepare() -> tuple[ChatAttachment, ...]:
            result = existing + tuple(prepare_attachment(source) for source in selected)
            validate_attachments(result)
            return result

        def finished() -> None:
            self._attachment_processing = False
            self._refresh_busy_controls()

        def accepted(result: object) -> None:
            if generation != self._session_generation:
                return
            self._pending_attachments = list(result)
            self._refresh_attachment_label()
            self._set_status("附件已在本机检查，尚未发送。请检查问题后点击发送。", "ok")

        self._attachment_processing = True
        self._refresh_busy_controls()
        self._set_status("正在本机读取和检查附件…", "busy")
        self._start_task(
            FunctionTask(prepare),
            accepted,
            lambda error: self._set_status(f"附件读取失败：{error}", "error"),
            finished,
        )

    def _refresh_attachment_label(self) -> None:
        while self.attachment_cards_layout.count():
            item = self.attachment_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.attachment_label.hide()
        self.attachment_cards.setVisible(bool(self._pending_attachments))
        if not self._pending_attachments:
            self.attachment_label.clear()
            self.attachment_label.hide()
            self.clear_attachments_button.setEnabled(False)
            return
        self.attachment_label.setText(
            "待发送附件："
            + "、".join(
                f"{item.original_name}（{len(item.content) / 1024:.0f} KB）"
                for item in self._pending_attachments
            )
        )
        for index, attachment in enumerate(self._pending_attachments):
            card = QFrame()
            card.setObjectName("AttachmentCard")
            card.setMaximumWidth(280)
            layout = QHBoxLayout(card)
            label = QLabel(
                f"{'图片' if attachment.is_image else '文件'} · {attachment.original_name}\n"
                f"{len(attachment.content) / 1024:.0f} KB"
            )
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label, 1)
            remove = QPushButton("×")
            remove.setFixedSize(40, 40)
            remove.setAccessibleName(f"删除附件 {attachment.original_name}")
            remove.setToolTip(f"删除 {attachment.original_name}")
            remove.setDisabled(self._request_running or self._attachment_processing)
            remove.clicked.connect(
                lambda _checked=False, position=index: self._remove_attachment(position)
            )
            layout.addWidget(remove)
            self.attachment_cards_layout.addWidget(card)
        self.attachment_cards_layout.addStretch(1)
        self.clear_attachments_button.setEnabled(not self._request_running)

    def _remove_attachment(self, index: int) -> None:
        if self._request_running or self._attachment_processing or self._backup_running:
            return
        if 0 <= index < len(self._pending_attachments):
            self._pending_attachments.pop(index)
            self._refresh_attachment_label()

    def _pending_image_bytes(self) -> list[bytes]:
        if self._pending_attachments:
            return [item.content for item in self._pending_attachments if item.is_image]
        if not self._pending_image_ids:
            return []
        if not self.current_user or self.file_management_service is None:
            raise RuntimeError("无法读取已附加图片")
        images: list[bytes] = []
        for file_id in self._pending_image_ids:
            stored = self.file_management_service.get_file(self.current_user.id, file_id)
            media_type = str(stored.get("media_type", ""))
            if not media_type.startswith("image/"):
                raise RuntimeError("附加内容不是受支持的图片")
            images.append(bytes(stored["content"]))
        return images

    def _clear_pending_images(self) -> None:
        self._pending_attachments.clear()
        self._pending_image_ids.clear()
        self._pending_image_names.clear()
        self.attachment_label.clear()
        self.attachment_label.hide()
        if hasattr(self, "clear_attachments_button"):
            self.clear_attachments_button.setEnabled(False)
        if hasattr(self, "attachment_cards"):
            self._refresh_attachment_label()

    def _confirm_deepseek_attachments(self, *, has_images: bool) -> bool:
        detail = (
            "图片原件（包括当前对话最近消息中的历史图片）将发送到 "
            f"{DEEPSEEK_VISION_MODEL} 图片实验版。"
            "这是原生图片理解；系统不会先把图片 OCR 成文字。"
            if has_images
            else "附件在本机提取的文字将发送到 DeepSeek。"
        )
        answer = QMessageBox.warning(
            self,
            "发送附件到 DeepSeek",
            detail + "\n\nDeepSeek 可能按账户规则收取 API 费用。"
            "附件也会随当前对话保存在本机和数据备份中。是否发送？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def send_message(self) -> None:
        if (
            self._request_running
            or self._voice_processing
            or self._attachment_processing
            or self._backup_running
            or not self.current_user
        ):
            return
        if self.voice_controller.is_recording:
            self._set_status("请先结束当前录音，再发送识别后的文字。", "error")
            return
        content = self.message_input.toPlainText().strip()
        attachments = tuple(self._pending_attachments)
        if not content and attachments:
            content = "请根据我附加的图片或文件内容回答，并说明无法确定的部分。"
        if not content:
            self._set_status("请输入消息或附加文件后再发送。", "error")
            return
        if len(content) > MAX_MESSAGE_LENGTH:
            self._set_status(f"单条消息不能超过 {MAX_MESSAGE_LENGTH} 个字符。", "error")
            return
        selection = self._selected_provider()
        if selection is None:
            return
        provider, provider_name, model = selection
        user_id = self.current_user.id
        conversation_id = self.current_conversation_id
        generation = self._session_generation
        preferences = dict(self._preferences)
        if self.preferences_service is not None:
            try:
                preferences = dict(self.preferences_service.get(user_id))
            except Exception:
                self._set_status("无法读取您的回答偏好，请稍后再试。", "error")
                return
            self.context_checkbox.blockSignals(True)
            self.context_checkbox.setChecked(bool(preferences.get("ai_context_consent", False)))
            self.context_checkbox.blockSignals(False)
        context_service = (
            self.ai_assistant_service
            if getattr(self.current_user, "role_code", "member") == "member"
            else None
        )
        self._preferences = preferences
        share_context = self.context_checkbox.isChecked()
        if (
            context_service is not None
            and callable(getattr(context_service, "build_member_context", None))
            and needs_life_context(content)
        ):
            share_context = self._ensure_context_consent()
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        try:
            kinds = (
                self.chat_service.context_attachment_kinds(
                    user_id, conversation_id=conversation_id, limit=MAX_CONTEXT_MESSAGES
                )
                if hasattr(self.chat_service, "context_attachment_kinds")
                else set()
            )
            has_images = "image" in kinds or any(item.is_image for item in attachments)
            has_documents = "document" in kinds or any(not item.is_image for item in attachments)
            if has_documents and provider_name != "deepseek_cloud":
                self._set_status(
                    "当前对话包含文件附件。请切回 DeepSeek 继续，或新建对话使用其他服务。", "error"
                )
                return
            if has_images and not provider.supports_images:
                self._set_status("所选服务不支持图片，请选择具备视觉能力的模型。", "error")
                return
            if (
                provider_name == "deepseek_cloud"
                and (has_images or has_documents)
                and not self._confirm_deepseek_attachments(has_images=has_images)
            ):
                self._set_status("已取消发送，输入和附件均已保留。", "hint")
                return
        except Exception as exc:
            self._set_status(f"无法检查对话附件：{exc}", "error")
            return

        agent_enabled = (
            share_context
            and context_service is not None
            and callable(getattr(context_service, "create_member_draft", None))
        )
        if agent_enabled and len(content) > 4000:
            self._set_status("生活智能体单条消息请控制在 4000 字以内。", "error")
            return
        agent_result: dict[str, Any] = {}
        input_source = "voice" if content == self._last_voice_text else "text"

        def run_request() -> str:
            exchange = None
            effective_model = model
            try:
                options = (
                    {"conversation_id": conversation_id} if conversation_id is not None else {}
                )
                history = self.chat_service.context_for_provider(
                    user_id, limit=MAX_CONTEXT_MESSAGES - 1, **options
                )
                # Configuration applies to all conversation intents, including small talk.
                if context_service is not None:
                    config_reader = getattr(context_service, "get_ai_config", None)
                    if callable(config_reader):
                        config = config_reader(user_id)
                        if not config.get("enabled", True) or not config.get(
                            "member_assistant_enabled", True
                        ):
                            raise ValueError("会员 AI 助手当前已关闭，未发送资料。")
                system_message = personalized_system_message(
                    user_id, content, preferences,
                    None if agent_enabled else context_service,
                    share_context=share_context,
                )
                model_messages = [
                    system_message,
                    *history,
                    ChatTurn("user", content, attachments).as_dict(),
                ]
                if hasattr(provider, "validate_request"):
                    effective_model = provider.validate_request(model, model_messages)
                elif hasattr(provider, "resolve_model"):
                    effective_model = provider.resolve_model(model, model_messages)
                if cancel_event.is_set():
                    raise RuntimeError("已停止回答")
                exchange = self.chat_service.begin_message(
                    user_id,
                    content,
                    provider=provider_name,
                    model=effective_model,
                    attachments=attachments,
                    **options,
                )
                progress_base = {
                    "generation": generation,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "exchange": exchange,
                }
                self.stream_progress.emit({**progress_base, "content": ""})
                if agent_enabled:
                    agent = ConversationAgentProvider(
                        provider, history, ChatTurn("user", content, attachments).as_dict(),
                        cancel_event, input_source=input_source,
                        answer_settings=preferences,
                    )
                    result = context_service.create_member_draft(
                        user_id, agent, provider_name, effective_model, content,
                        images=[a.content for a in attachments if a.is_image],
                    )
                    agent_result.update(result)
                    answer = str(result["answer"])
                elif callable(getattr(provider, "chat_stream", None)):
                    chunks: list[str] = []
                    for chunk in provider.chat_stream(
                        effective_model, model_messages, cancel_event=cancel_event
                    ):
                        chunks.append(chunk)
                        self.stream_progress.emit({**progress_base, "content": "".join(chunks)})
                    answer = "".join(chunks)
                else:
                    answer = str(provider.chat(effective_model, model_messages))
                if cancel_event.is_set():
                    raise RuntimeError("已停止回答")
                self.chat_service.complete_message(
                    user_id,
                    exchange.assistant_message.id,
                    answer,
                    provider=provider_name,
                    model=effective_model,
                )
                return answer
            except Exception as exc:
                if exchange is not None:
                    with suppress(Exception):
                        self.chat_service.fail_message(
                            user_id,
                            exchange.assistant_message.id,
                            str(exc),
                            provider=provider_name,
                            model=effective_model,
                        )
                raise

        def still_current() -> bool:
            return (
                generation == self._session_generation
                and self.current_user is not None
                and user_id == self.current_user.id
                and conversation_id == self.current_conversation_id
            )

        def succeeded(_answer: object) -> None:
            if not still_current():
                return
            self.message_input.clear()
            self._clear_pending_images()
            if conversation_id is not None:
                self._conversation_drafts.pop(conversation_id, None)
            self._load_conversation_messages()
            self._refresh_conversations()
            if agent_result:
                self._refresh_drafts()
                pending = len(agent_result.get("proposals", []))
                self._set_status(
                    f"回答完成，{pending} 项待确认；点击右上角“AI 待确认”核对保存。"
                    if pending else "回答完成，本轮没有需要保存的内容。", "ok"
                )
            else:
                self._set_status("回答完成。附件会随当前对话最近的消息用于后续理解。", "ok")
            self.data_changed.emit()
            self._speak_answer(str(_answer))

        def failed(error: object) -> None:
            if not still_current():
                return
            self._load_conversation_messages()
            self._refresh_conversations()
            self._set_status(f"回答失败：{error}。输入与附件已保留，可修改后重试。", "error")

        self._set_request_running(True)
        self._set_status("AI 正在回答…", "busy")
        self._start_task(
            FunctionTask(run_request), succeeded, failed, lambda: self._set_request_running(False)
        )

    def _stream_update(self, progress: object) -> None:
        if not isinstance(progress, dict) or self.current_user is None:
            return
        if (
            progress.get("generation") != self._session_generation
            or progress.get("user_id") != self.current_user.id
            or progress.get("conversation_id") != self.current_conversation_id
        ):
            return
        exchange = progress["exchange"]
        if exchange.user_message.id not in self.message_list._bubbles:
            self.message_list.add_message(
                exchange.user_message.id, "user", exchange.user_message.content
            )
        assistant_id = exchange.assistant_message.id
        if assistant_id not in self.message_list._bubbles:
            self.message_list.add_message(assistant_id, "assistant", "", "pending")
        self.message_list.update_message(assistant_id, str(progress.get("content", "")), "pending")

    def _cancel_request(self) -> None:
        if self._request_running:
            self._cancel_event.set()
            self.cancel_button.setDisabled(True)
            self._set_status("正在停止回答，已保留输入；当前连接返回后会结束。", "busy")

    def send_structured_message(self) -> None:
        if (
            self._request_running
            or self._voice_processing
            or self._attachment_processing
            or self._backup_running
            or not self.current_user
            or self.ai_assistant_service is None
        ):
            return
        if self.voice_controller.is_recording:
            self._set_status("请先结束当前录音。", "error")
            return
        content = self.message_input.toPlainText().strip()
        if any(not item.is_image for item in self._pending_attachments):
            self._set_status("文件附件请使用“发送”进行对话；生活草稿支持文字和图片。", "error")
            return
        if not content and (self._pending_image_ids or self._pending_attachments):
            content = "请只提取图片中客观可见的饮食或生活信息，并把可能的生活记录作为待确认草稿。"
        if not content:
            self._set_status("请先输入问题或附加图片。", "error")
            return
        if len(content) > 4_000:
            self._set_status("生成生活草稿时，问题不能超过 4000 个字符。", "error")
            return

        if not self._ensure_context_consent():
            self._set_status("生活草稿需要分享本人资料；您也可以直接发送进行一般问答。", "hint")
            return

        selection = self._selected_provider()
        if selection is None:
            return
        provider, provider_name, model = selection
        try:
            image_bytes = self._pending_image_bytes()
        except Exception as exc:
            self._set_status(f"读取附加图片失败：{exc}", "error")
            return
        if image_bytes and not provider.supports_images:
            self._set_status(
                "当前 AI 服务只支持文字；图片请改用具备视觉能力的 Ollama 模型。",
                "error",
            )
            return
        if image_bytes and provider_name == "deepseek_cloud":
            if not self._confirm_deepseek_attachments(has_images=True):
                return
            try:
                messages = [{"role": "user", "content": content, "images": image_bytes}]
                model = (
                    provider.validate_request(model, messages)
                    if hasattr(provider, "validate_request")
                    else provider.resolve_model(model, messages)
                )
            except Exception as exc:
                self._set_status(f"图片请求检查失败：{exc}；输入和附件已保留。", "error")
                return

        try:
            exchange = self.chat_service.begin_message(
                self.current_user.id,
                content,
                provider=provider_name,
                model=model,
                conversation_id=self.current_conversation_id,
                attachments=tuple(self._pending_attachments),
            )
        except Exception as exc:
            self._set_status(f"保存消息失败：{exc}", "error")
            return
        self.message_list.add_message(
            exchange.user_message.id,
            "user",
            exchange.user_message.content,
            exchange.user_message.status,
        )
        self.message_list.add_message(exchange.assistant_message.id, "assistant", "", "pending")
        self._set_request_running(True)
        self._set_status("AI 正在读取已确认的有限数据并生成待确认草稿…", "busy")
        task = FunctionTask(
            self.ai_assistant_service.create_member_draft,
            self.current_user.id,
            provider,
            provider_name,
            model,
            content,
            images=image_bytes,
        )
        assistant_id = exchange.assistant_message.id
        user_id = self.current_user.id
        generation = self._session_generation

        def success(result: object) -> None:
            if generation == self._session_generation:
                self._structured_answer_succeeded(assistant_id, result)
            else:
                answer = (
                    str(result.get("answer", "")).strip() if isinstance(result, Mapping) else ""
                )
                if answer:
                    self.chat_service.complete_message(user_id, assistant_id, answer)
                else:
                    self.chat_service.fail_message(user_id, assistant_id, "AI 草稿返回格式无效")

        def failure(error: object) -> None:
            if generation == self._session_generation:
                self._answer_failed(assistant_id, error)
            else:
                with suppress(Exception):
                    self.chat_service.fail_message(user_id, assistant_id, str(error))

        self._start_task(
            task,
            success,
            failure,
            lambda: self._set_request_running(False),
        )

    def _selected_provider(self) -> tuple[object, str, str] | None:
        if self.demo_checkbox.isChecked():
            return DemoLifeProvider(), "workflow_demo", DEMO_MODEL
        mode = self.mode_combo.currentData()
        if mode == "local":
            model = self.local_model_combo.currentText().strip()
            if not model:
                self._set_status("请填写本地 Ollama 模型名称。", "error")
                return None
            return LocalOllamaProvider(), "ollama_local", model
        if mode == "ollama_cloud":
            key = self._cloud_key("ollama_cloud")
            if not key and not self._open_key_dialog("ollama_cloud"):
                self._set_status("使用云端模式前需要提供自己的 API Key。", "error")
                return None
            key = self._cloud_key("ollama_cloud")
            model = self.cloud_model_combo.currentData()
            if not key or not model:
                self._set_status("请先验证 API Key 并选择可用的云端模型。", "error")
                return None
            return OllamaCloudProvider(key), "ollama_cloud", str(model)
        key = self._cloud_key("deepseek_cloud")
        if not key and not self._open_key_dialog("deepseek_cloud"):
            self._set_status("使用 DeepSeek 前需要提供自己的 API Key。", "error")
            return None
        key = self._cloud_key("deepseek_cloud")
        if not key:
            self._set_status("请先验证 DeepSeek API Key。", "error")
            return None
        return (
            DeepSeekCloudProvider(key),
            "deepseek_cloud",
            str(self.deepseek_model_combo.currentData()),
        )

    def _structured_answer_succeeded(self, assistant_id: int, result: object) -> None:
        if not isinstance(result, Mapping):
            self._answer_failed(assistant_id, "AI 草稿返回格式无效。")
            return
        answer = str(result.get("answer", "")).strip()
        if not answer:
            self._answer_failed(assistant_id, "AI 没有返回可显示的回答。")
            return
        self._answer_succeeded(assistant_id, answer)
        self._refresh_drafts()
        self.data_changed.emit()
        proposals = result.get("proposals")
        pending = len(proposals) if isinstance(proposals, list) else 0
        self._set_status(
            f"回答完成，并生成 {pending} 项待确认内容；AI 未直接修改档案。",
            "ok",
        )

    def _open_ai_drafts(self) -> None:
        if self.ai_assistant_service is None or not self.current_user:
            return
        if self._draft_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("AI 待确认内容")
            dialog.resize(820, 680)
            layout = QVBoxLayout(dialog)
            panel = AiProposalsPanel(dialog)
            panel.refresh_requested.connect(self._refresh_drafts)
            panel.confirm_requested.connect(self._confirm_ai_proposal)
            panel.dismiss_requested.connect(self._dismiss_ai_proposal)
            layout.addWidget(panel, 1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            self._draft_dialog = dialog
            self._draft_panel = panel
        self._refresh_drafts()
        self._draft_dialog.exec()

    def _refresh_drafts(self) -> None:
        if self.ai_assistant_service is None or not self.current_user:
            self.drafts_button.setText("AI 待确认")
            return
        try:
            drafts = list(
                self.ai_assistant_service.list_drafts(self.current_user.id, include_resolved=True)
            )
            pending_count = sum(
                1
                for draft in drafts
                for proposal in draft.get("proposals", [])
                if isinstance(proposal, Mapping) and proposal.get("status") == "pending"
            )
            self.drafts_button.setText(f"AI 待确认（{pending_count}）")
            if self._draft_panel is not None:
                self._draft_panel.set_drafts(drafts)
        except Exception as exc:
            self.drafts_button.setText("AI 待确认")
            if self._draft_panel is not None:
                self._draft_panel.set_error(str(exc))

    def _confirm_ai_proposal(self, insight_id: int, proposal_id: int) -> None:
        if self.ai_assistant_service is None or not self.current_user:
            return
        answer = QMessageBox.warning(
            self,
            "确认保存 AI 草稿",
            "请确认内容与实际情况一致。AI 可能出错；确认后这一项才会写入本地"
            "档案、生活记录或提醒。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ai_assistant_service.confirm_proposal(
                self.current_user.id, insight_id, proposal_id
            )
            self._refresh_drafts()
            self._set_status("已确认保存，可在生活记录、档案或提醒中查看。", "ok")
            self.data_changed.emit()
        except Exception as exc:
            if self._draft_panel is not None:
                self._draft_panel.set_error(str(exc))

    def _dismiss_ai_proposal(self, insight_id: int, proposal_id: int) -> None:
        if self.ai_assistant_service is None or not self.current_user:
            return
        try:
            self.ai_assistant_service.dismiss_proposal(
                self.current_user.id, insight_id, proposal_id
            )
            self._refresh_drafts()
        except Exception as exc:
            if self._draft_panel is not None:
                self._draft_panel.set_error(str(exc))

    def _answer_succeeded(self, assistant_id: int, answer: str) -> None:
        if not self.current_user:
            return
        try:
            self.chat_service.complete_message(self.current_user.id, assistant_id, answer)
        except Exception as exc:
            self._set_status(f"AI 已回答，但保存回复失败：{exc}", "error")
            self.message_list.update_message(assistant_id, answer, "complete")
            return
        self.message_list.update_message(assistant_id, answer, "complete")
        self.message_input.clear()
        self._clear_pending_images()
        self._refresh_conversations()
        self._set_status("回答完成。", "ok")

    def _answer_failed(self, assistant_id: int, error: object) -> None:
        message = str(error) or "模型调用失败。"
        if self.current_user:
            with suppress(Exception):
                self.chat_service.fail_message(self.current_user.id, assistant_id, message)
        self.message_list.update_message(assistant_id, f"回答失败：{message}", "error")
        self._set_status(message, "error")

    def _set_request_running(self, running: bool) -> None:
        self._request_running = running
        self._refresh_busy_controls()
        if not running and not self._backup_running:
            self.message_input.setFocus()

    def _set_backup_running(self, running: bool) -> None:
        self._backup_running = running
        self._refresh_busy_controls()
        if not running and not self._request_running:
            self.message_input.setFocus()

    def _toggle_voice_recording(self) -> None:
        if self._request_running or self._backup_running or self._voice_processing:
            return
        if not self.current_user:
            self._set_status("请先登录后再使用语音输入。", "error")
            return
        if self.voice_controller.is_recording:
            self.voice_controller.stop()
        else:
            self._stop_speaking()
            self.voice_controller.start()

    def _voice_recording_started(self) -> None:
        self.voice_button.setText("结束并识别")
        self._set_status("正在本机录音；最长 60 秒，录音不会上传。", "busy")
        self._refresh_busy_controls()

    def _voice_remaining_seconds_changed(self, remaining: int) -> None:
        if self.voice_controller.is_recording:
            self.voice_button.setText(f"结束并识别 {remaining}s")

    def _voice_recording_stopped(self, audio: bytes) -> None:
        self.voice_button.setText("开始语音")
        self._voice_processing = True
        self._refresh_busy_controls()
        self._set_status("正在本机识别语音，首次使用需要加载离线模型…", "busy")
        task = FunctionTask(self.voice_recognizer.transcribe_pcm, audio)
        self._start_task(
            task,
            self._voice_recognition_succeeded,
            self._voice_failed,
            self._voice_recognition_finished,
        )

    def _voice_recognition_succeeded(self, result: object) -> None:
        recognized = str(result).strip()
        existing = self.message_input.toPlainText().strip()
        self.message_input.setPlainText(f"{existing}\n{recognized}".strip())
        self._last_voice_text = self.message_input.toPlainText().strip()
        self._set_status("已听到您说的话，点击发送即可；也可以先改一下。", "ok")

    def _voice_recognition_finished(self) -> None:
        self._voice_processing = False
        self._refresh_busy_controls()
        self.message_input.setFocus()

    def _voice_failed(self, error: object) -> None:
        self.voice_button.setText("开始语音")
        self._set_status(str(error) or "语音输入失败，请改用文字输入。", "error")
        self._refresh_busy_controls()

    def _refresh_busy_controls(self) -> None:
        recording = self.voice_controller.is_recording
        busy = (
            self._request_running
            or self._backup_running
            or self._voice_processing
            or self._attachment_processing
            or recording
        )
        self.conversation_list.setDisabled(busy)
        self.new_conversation_button.setDisabled(busy)
        self.rename_conversation_button.setDisabled(busy)
        self.send_button.setDisabled(busy)
        self.structured_button.setDisabled(busy or self.ai_assistant_service is None)
        self.message_input.setDisabled(busy)
        self.mode_combo.setDisabled(busy)
        self.local_model_combo.setDisabled(busy)
        self.cloud_model_combo.setDisabled(busy)
        self.deepseek_model_combo.setDisabled(busy)
        self.key_button.setDisabled(busy)
        self.settings_button.setDisabled(busy)
        self.context_checkbox.setDisabled(busy)
        self.demo_checkbox.setDisabled(busy)
        for button in (self.meal_button, self.activity_button, self.sleep_button):
            button.setDisabled(busy)
        self.life_actions_button.setDisabled(busy)
        self.attachment_cards.setDisabled(busy)
        self.cancel_button.setVisible(self._request_running)
        self.cancel_button.setDisabled(not self._request_running)
        for suggestion in self.message_list.suggestion_buttons:
            suggestion.setDisabled(busy)
        self.logout_button.setDisabled(busy)
        self.backup_button.setDisabled(busy or self.backup_service is None)
        self.import_button.setDisabled(busy or self.backup_service is None)
        self.image_button.setDisabled(
            busy or not callable(getattr(self.chat_service, "list_message_attachments", None))
        )
        self.file_button.setDisabled(busy or self.mode_combo.currentData() != "deepseek_cloud")
        self.clear_attachments_button.setDisabled(busy or not self._pending_attachments)
        self.drafts_button.setDisabled(busy or self.ai_assistant_service is None)
        self.voice_button.setDisabled(
            self._request_running
            or self._backup_running
            or self._voice_processing
            or self._attachment_processing
        )
        self.send_button.setText("等待中…" if self._request_running else "发送")
        self.backup_button.setText("处理中…" if self._backup_running else "导出数据")
        self.import_button.setText("处理中…" if self._backup_running else "导入数据")

    def _start_task(
        self,
        task: FunctionTask,
        on_result,
        on_error,
        on_finished=None,
    ) -> None:
        self._tasks.append(task)
        task.signals.result.connect(on_result)
        task.signals.error.connect(on_error)
        if on_finished:
            task.signals.finished.connect(on_finished)
        task.signals.finished.connect(lambda: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _task_finished(self, task: FunctionTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)

    def _set_status(self, message: str, kind: str) -> None:
        object_names = {
            "hint": "Hint",
            "ok": "StatusOk",
            "busy": "StatusBusy",
            "error": "StatusError",
        }
        self.status_label.setObjectName(object_names.get(kind, "Hint"))
        self.status_label.setText(message)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _show_warning(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)
