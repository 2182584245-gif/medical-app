from __future__ import annotations

from types import SimpleNamespace

from ollama_chat_app.ui.files_reports_panel import FilesReportsPanel


class FakeFileService:
    def list_files(self, user_id: int):
        assert user_id == 7
        return [
            {
                "id": 11,
                "original_name": "饮食照片.png",
                "media_type": "image/png",
                "size_bytes": 2048,
                "attached_to_report": False,
            }
        ]

    def storage_usage(self, user_id: int):
        assert user_id == 7
        return {"used_bytes": 2048, "quota_bytes": 100 * 1024 * 1024}

    def list_reports(self, user_id: int):
        assert user_id == 7
        return [
            {
                "id": 21,
                "report_type": "体检报告",
                "report_date": "2026-09-01",
                "institution": "示例机构",
                "original_name": "报告.pdf",
                "status": "draft",
            }
        ]

    def get_report(self, user_id: int, report_id: int):
        assert (user_id, report_id) == (7, 21)
        return {
            "summary": "待核对摘要",
            "items": [
                {
                    "item_name": "示例指标",
                    "result_value": "5.2",
                    "unit": "mmol/L",
                    "reference_range": "3.0-6.0",
                    "status": "draft",
                }
            ],
        }


def test_files_reports_panel_loads_portable_files_and_drafts(qtbot) -> None:
    panel = FilesReportsPanel(FakeFileService())
    qtbot.addWidget(panel)

    panel.set_user(SimpleNamespace(id=7).id)

    assert panel.file_table.rowCount() == 1
    assert panel.file_table.item(0, 0).text() == "饮食照片.png"
    assert "100.0 MB" in panel.usage_label.text()
    assert panel.report_table.rowCount() == 1
    assert panel.report_table.item(0, 4).text() == "待确认"
    panel.report_table.selectRow(0)
    panel.show_selected_report()
    assert "示例指标" in panel.report_detail.toPlainText()
    assert panel.confirm_report_button.isEnabled()
