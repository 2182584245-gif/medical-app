from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QThreadPool
from PySide6.QtGui import QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..workers.task import FunctionTask
from .time_fields import OptionalDateEdit


def _size_text(size_bytes: object) -> str:
    try:
        value = int(size_bytes)
    except (TypeError, ValueError):
        return "大小未知"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / 1024:.1f} KB"


class FilePreviewDialog(QDialog):
    def __init__(self, stored_file: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"查看原件｜{stored_file.get('original_name', '文件')}")
        self.resize(900, 680)
        layout = QVBoxLayout(self)
        content = bytes(stored_file.get("content", b""))
        media_type = str(stored_file.get("media_type", ""))

        if media_type.startswith("image/"):
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setScaledContents(False)
            pixmap = QPixmap()
            if not pixmap.loadFromData(content):
                label.setText("无法显示这张图片，但原件仍保存在本地数据库中。")
            else:
                label.setPixmap(
                    pixmap.scaled(
                        840,
                        590,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            layout.addWidget(label, 1)
        elif media_type == "application/pdf":
            self._pdf_buffer = QBuffer(self)
            self._pdf_buffer.setData(QByteArray(content))
            self._pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self._pdf_document = QPdfDocument(self)
            error = self._pdf_document.load(self._pdf_buffer)
            if error == QPdfDocument.Error.None_:
                view = QPdfView(self)
                view.setDocument(self._pdf_document)
                view.setPageMode(QPdfView.PageMode.MultiPage)
                layout.addWidget(view, 1)
            else:
                layout.addWidget(QLabel("无法预览该 PDF；可以返回后导出原件查看。"), 1)
        else:
            layout.addWidget(QLabel("当前文件类型不支持应用内预览。"), 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ReportDraftDialog(QDialog):
    """Review extracted source text before it becomes a report draft."""

    def __init__(self, extraction: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("核对 OCR / PDF 提取结果")
        self.resize(900, 720)
        root = QVBoxLayout(self)
        warning = QLabel(
            "以下内容由原件文字层或离线 OCR 提取，可能识别错误。保存后仍是草稿；"
            "请对照原件核对，应用不会据此诊断疾病。"
        )
        warning.setWordWrap(True)
        warning.setObjectName("CommerceNotice")
        root.addWidget(warning)

        form = QFormLayout()
        self.report_type = QLineEdit("健康报告")
        form.addRow("报告类型", self.report_type)
        self.report_date = OptionalDateEdit()
        self.report_date.setToolTip("选填；请按报告原件选择日期，无需输入时间或时区。")
        form.addRow("报告日期", self.report_date)
        self.institution = QLineEdit()
        self.institution.setPlaceholderText("选填，请以原件显示为准")
        form.addRow("机构名称", self.institution)
        root.addLayout(form)

        root.addWidget(QLabel("识别原文（只读）"))
        self.source_text = QTextEdit()
        self.source_text.setReadOnly(True)
        text = str(extraction.get("text", ""))
        self.source_text.setPlainText(text or "没有识别到可显示的文字，请直接对照原件手工核对。")
        self.source_text.setMaximumHeight(210)
        root.addWidget(self.source_text)

        root.addWidget(QLabel("待确认项目（勾选后仅保存为草稿项目）"))
        self.item_table = QTableWidget(0, 4)
        self.item_table.setHorizontalHeaderLabels(["采用", "项目", "结果 / 单位", "参考范围"])
        self.item_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        candidates = extraction.get("candidate_items", [])
        rows = list(candidates) if isinstance(candidates, list) else []
        self.item_table.setRowCount(len(rows))
        for row_index, candidate in enumerate(rows):
            if not isinstance(candidate, Mapping):
                continue
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            check.setData(Qt.ItemDataRole.UserRole, dict(candidate))
            self.item_table.setItem(row_index, 0, check)
            self.item_table.setItem(
                row_index, 1, QTableWidgetItem(str(candidate.get("item_name", "")))
            )
            value = str(candidate.get("result_value", "") or "")
            unit = str(candidate.get("unit", "") or "")
            self.item_table.setItem(row_index, 2, QTableWidgetItem(f"{value} {unit}".strip()))
            self.item_table.setItem(
                row_index,
                3,
                QTableWidgetItem(str(candidate.get("reference_range", "") or "")),
            )
        header = self.item_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.item_table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存为待确认草稿")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _validate(self) -> None:
        if not self.report_type.text().strip():
            QMessageBox.warning(self, "报告类型未填写", "请填写报告类型。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, Any]:
        selected_items: list[dict[str, Any]] = []
        for row in range(self.item_table.rowCount()):
            item = self.item_table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                value = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(value, Mapping):
                    selected_items.append(dict(value))
        source_text = self.source_text.toPlainText().strip()
        summary = source_text[:3_000] if source_text else None
        return {
            "report_type": self.report_type.text().strip(),
            "report_date": self.report_date.iso_value(),
            "institution": self.institution.text().strip() or None,
            "summary": summary,
            "items": selected_items,
        }


class FilesReportsPanel(QWidget):
    """Portable member file library, offline OCR, and report confirmation UI."""

    def __init__(self, file_service: object | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.file_service = file_service
        self.user_id: int | None = None
        self._tasks: list[FunctionTask] = []
        self._busy = False

        root = QVBoxLayout(self)
        notice = QLabel(
            "图片和 PDF 原件保存在本地 SQLite 数据库中，会随数据备份迁移；"
            "OCR 结果必须人工核对，异常标记不代表诊断。"
        )
        notice.setWordWrap(True)
        notice.setObjectName("CommerceNotice")
        root.addWidget(notice)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_files_tab(), "我的文件")
        self.tabs.addTab(self._build_reports_tab(), "健康报告")
        root.addWidget(self.tabs, 1)
        self._refresh_controls()

    def _build_files_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.usage_label = QLabel("已使用 0 MB / 100 MB")
        layout.addWidget(self.usage_label)
        self.file_table = QTableWidget(0, 4)
        self.file_table.setHorizontalHeaderLabels(["文件名", "类型", "大小", "用途"])
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.itemSelectionChanged.connect(self._refresh_controls)
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.file_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        layout.addWidget(self.file_table, 1)
        actions = QHBoxLayout()
        self.upload_button = QPushButton("上传图片 / PDF")
        self.upload_button.setObjectName("PrimaryButton")
        self.upload_button.clicked.connect(self.upload_file)
        actions.addWidget(self.upload_button)
        self.preview_button = QPushButton("查看原件")
        self.preview_button.clicked.connect(self.preview_selected_file)
        actions.addWidget(self.preview_button)
        self.export_button = QPushButton("导出副本")
        self.export_button.clicked.connect(self.export_selected_file)
        actions.addWidget(self.export_button)
        self.extract_button = QPushButton("提取文字并建报告草稿")
        self.extract_button.clicked.connect(self.extract_selected_file)
        actions.addWidget(self.extract_button)
        self.delete_file_button = QPushButton("删除文件")
        self.delete_file_button.setObjectName("DangerButton")
        self.delete_file_button.clicked.connect(self.delete_selected_file)
        actions.addWidget(self.delete_file_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def _build_reports_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.report_table = QTableWidget(0, 5)
        self.report_table.setHorizontalHeaderLabels(["报告类型", "日期", "机构", "原件", "状态"])
        self.report_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.report_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.report_table.itemSelectionChanged.connect(self.show_selected_report)
        self.report_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            self.report_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        layout.addWidget(self.report_table, 1)
        self.report_detail = QTextEdit()
        self.report_detail.setReadOnly(True)
        self.report_detail.setMaximumHeight(180)
        self.report_detail.setPlaceholderText("选择一份报告可查看摘要和项目。")
        layout.addWidget(self.report_detail)
        actions = QHBoxLayout()
        self.confirm_report_button = QPushButton("确认所选草稿")
        self.confirm_report_button.setObjectName("PrimaryButton")
        self.confirm_report_button.clicked.connect(self.confirm_selected_report)
        actions.addWidget(self.confirm_report_button)
        self.archive_report_button = QPushButton("归档所选报告")
        self.archive_report_button.clicked.connect(self.archive_selected_report)
        actions.addWidget(self.archive_report_button)
        self.delete_report_button = QPushButton("删除所选报告")
        self.delete_report_button.setObjectName("DangerButton")
        self.delete_report_button.clicked.connect(self.delete_selected_report)
        actions.addWidget(self.delete_report_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        self.file_table.setRowCount(0)
        self.report_table.setRowCount(0)
        self.report_detail.clear()
        if self.user_id is None or self.file_service is None:
            self._set_status("当前构建没有启用文件与报告数据服务。", True)
            self._refresh_controls()
            return
        try:
            files = list(self.file_service.list_files(self.user_id))
            self.file_table.setRowCount(len(files))
            for row, value in enumerate(files):
                name = QTableWidgetItem(str(value.get("original_name", "")))
                name.setData(Qt.ItemDataRole.UserRole, dict(value))
                self.file_table.setItem(row, 0, name)
                self.file_table.setItem(row, 1, QTableWidgetItem(str(value.get("media_type", ""))))
                self.file_table.setItem(
                    row, 2, QTableWidgetItem(_size_text(value.get("size_bytes")))
                )
                self.file_table.setItem(
                    row,
                    3,
                    QTableWidgetItem("报告原件" if value.get("attached_to_report") else "普通文件"),
                )
            usage = self.file_service.storage_usage(self.user_id)
            self.usage_label.setText(
                f"已使用 {_size_text(usage['used_bytes'])} / {_size_text(usage['quota_bytes'])}"
            )
            reports = list(self.file_service.list_reports(self.user_id))
            self.report_table.setRowCount(len(reports))
            for row, value in enumerate(reports):
                report_type = QTableWidgetItem(str(value.get("report_type", "")))
                report_type.setData(Qt.ItemDataRole.UserRole, dict(value))
                self.report_table.setItem(row, 0, report_type)
                self.report_table.setItem(
                    row, 1, QTableWidgetItem(str(value.get("report_date") or ""))
                )
                self.report_table.setItem(
                    row, 2, QTableWidgetItem(str(value.get("institution") or ""))
                )
                self.report_table.setItem(
                    row, 3, QTableWidgetItem(str(value.get("original_name") or ""))
                )
                status = {"draft": "待确认", "confirmed": "已确认"}.get(
                    str(value.get("status")), "已归档"
                )
                self.report_table.setItem(row, 4, QTableWidgetItem(status))
            self.status_label.hide()
        except Exception as error:
            self._set_status(f"读取文件与报告失败：{error}", True)
        self._refresh_controls()

    def upload_file(self) -> None:
        if self.user_id is None or self.file_service is None:
            return
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片或 PDF",
            str(Path.home()),
            "图片或 PDF (*.jpg *.jpeg *.png *.webp *.pdf)",
        )
        if not source:
            return
        try:
            self.file_service.upload_file(self.user_id, source)
            self.refresh()
            self._set_status("文件已加密哈希校验后保存到本地数据库。", False)
        except Exception as error:
            self._set_status(str(error), True)

    def preview_selected_file(self) -> None:
        value = self._selected(self.file_table)
        if value is None or self.user_id is None:
            return
        try:
            stored = self.file_service.get_file(self.user_id, int(value["id"]))
            FilePreviewDialog(stored, self).exec()
        except Exception as error:
            self._set_status(str(error), True)

    def export_selected_file(self) -> None:
        value = self._selected(self.file_table)
        if value is None or self.user_id is None:
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "导出文件副本", str(Path.home() / str(value.get("original_name", "文件")))
        )
        if not target:
            return
        try:
            path = self.file_service.export_file(self.user_id, int(value["id"]), target)
            self._set_status(f"文件副本已导出：{path}", False)
        except Exception as error:
            self._set_status(str(error), True)

    def delete_selected_file(self) -> None:
        value = self._selected(self.file_table)
        if value is None or self.user_id is None:
            return
        if (
            QMessageBox.question(self, "确认删除", "删除后无法在应用内恢复，是否继续？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.file_service.delete_file(self.user_id, int(value["id"]))
            self.refresh()
        except Exception as error:
            self._set_status(str(error), True)

    def extract_selected_file(self) -> None:
        value = self._selected(self.file_table)
        if value is None or self.user_id is None or self._busy:
            return
        self._busy = True
        self._refresh_controls()
        self._set_status("正在本机提取文字；图片首次 OCR 可能需要一些时间…", False)
        task = FunctionTask(self.file_service.extract_text, self.user_id, int(value["id"]))
        self._tasks.append(task)
        task.signals.result.connect(
            lambda result, file_id=int(value["id"]): self._extraction_ready(file_id, result)
        )
        task.signals.error.connect(lambda error: self._set_status(str(error), True))
        task.signals.finished.connect(lambda: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _extraction_ready(self, file_id: int, result: object) -> None:
        if self.user_id is None or not isinstance(result, Mapping):
            return
        dialog = ReportDraftDialog(result, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._set_status("已取消建档；提取结果没有写入数据库。", False)
            return
        values = dialog.values
        try:
            report_id = self.file_service.create_report_draft(
                self.user_id,
                file_id,
                report_type=values["report_type"],
                report_date=values["report_date"],
                institution=values["institution"],
                summary=values["summary"],
            )
            for item in values["items"]:
                self.file_service.add_report_item_draft(
                    self.user_id,
                    report_id,
                    str(item.get("item_name", "")),
                    result_value=item.get("result_value"),
                    unit=item.get("unit"),
                    reference_range=item.get("reference_range"),
                    flag="unknown",
                )
            self.refresh()
            self.tabs.setCurrentIndex(1)
            self._set_status("报告草稿已保存；请对照原件核对后再确认。", False)
        except Exception as error:
            self._set_status(str(error), True)

    def show_selected_report(self) -> None:
        value = self._selected(self.report_table)
        if value is None or self.user_id is None:
            self.report_detail.clear()
            self._refresh_controls()
            return
        try:
            report = self.file_service.get_report(self.user_id, int(value["id"]))
            lines = [str(report.get("summary") or "没有摘要。")]
            for item in report.get("items", []):
                if isinstance(item, Mapping):
                    reference_range = item.get("reference_range", "") or "未提取"
                    lines.append(
                        f"• {item.get('item_name', '')}：{item.get('result_value', '') or ''} "
                        f"{item.get('unit', '') or ''}｜参考范围 {reference_range}"
                        f"｜{('已确认' if item.get('status') == 'confirmed' else '待确认')}"
                    )
            self.report_detail.setPlainText("\n".join(lines))
        except Exception as error:
            self._set_status(str(error), True)
        self._refresh_controls()

    def confirm_selected_report(self) -> None:
        value = self._selected(self.report_table)
        if value is None or self.user_id is None:
            return
        answer = QMessageBox.warning(
            self,
            "确认报告内容",
            "请确认已对照原件核对摘要和项目。确认只表示内容核对完成，"
            "不代表疾病诊断，也不能替代医生意见。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.file_service.confirm_report(self.user_id, int(value["id"]))
            self.refresh()
            self._set_status("报告已标记为人工确认。", False)
        except Exception as error:
            self._set_status(str(error), True)

    def archive_selected_report(self) -> None:
        value = self._selected(self.report_table)
        if value is None or self.user_id is None:
            return
        try:
            self.file_service.archive_report(self.user_id, int(value["id"]))
            self.refresh()
        except Exception as error:
            self._set_status(str(error), True)

    def delete_selected_report(self) -> None:
        value = self._selected(self.report_table)
        if value is None or self.user_id is None:
            return
        if (
            QMessageBox.question(self, "确认删除", "删除报告后原文件会保留，是否继续？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.file_service.delete_report(self.user_id, int(value["id"]))
            self.refresh()
        except Exception as error:
            self._set_status(str(error), True)

    def _task_finished(self, task: FunctionTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        self._busy = False
        self._refresh_controls()

    @staticmethod
    def _selected(table: QTableWidget) -> dict[str, Any] | None:
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return dict(value) if isinstance(value, Mapping) else None

    def _refresh_controls(self) -> None:
        enabled = self.file_service is not None and self.user_id is not None and not self._busy
        has_file = self._selected(self.file_table) is not None
        report = self._selected(self.report_table)
        self.upload_button.setEnabled(enabled)
        for button in (
            self.preview_button,
            self.export_button,
            self.extract_button,
            self.delete_file_button,
        ):
            button.setEnabled(enabled and has_file)
        status = str((report or {}).get("status", ""))
        self.confirm_report_button.setEnabled(enabled and status == "draft")
        self.archive_report_button.setEnabled(enabled and status == "confirmed")
        self.delete_report_button.setEnabled(enabled and report is not None)

    def _set_status(self, text: str, error: bool) -> None:
        self.status_label.setObjectName("HealthError" if error else "HealthSuccess")
        self.status_label.setText(text)
        self.status_label.show()
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


__all__ = ["FilePreviewDialog", "FilesReportsPanel", "ReportDraftDialog"]
