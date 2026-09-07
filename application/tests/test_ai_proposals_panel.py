from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from ollama_chat_app.ui.ai_proposals_panel import AiProposalsPanel


def test_proposals_panel_emits_exact_draft_and_proposal_ids(qtbot) -> None:
    panel = AiProposalsPanel()
    qtbot.addWidget(panel)
    panel.set_drafts(
        [
            {
                "id": 12,
                "answer": "我整理出一项待确认记录。",
                "provider": "Ollama",
                "model": "qwen-vl",
                "proposals": [
                    {
                        "id": 3,
                        "status": "pending",
                        "type": "life_record",
                        "category": "diet",
                        "occurred_at": "2030-01-01T12:00:00+08:00",
                        "content": "午餐吃了米饭和青菜",
                    }
                ],
            }
        ]
    )

    confirm = panel.findChild(QPushButton, "AiConfirmButton-12-3")
    dismiss = panel.findChild(QPushButton, "AiDismissButton-12-3")
    assert confirm is not None
    assert dismiss is not None
    with qtbot.waitSignal(panel.confirm_requested) as signal:
        confirm.click()
    assert signal.args == [12, 3]
    with qtbot.waitSignal(panel.dismiss_requested) as signal:
        dismiss.click()
    assert signal.args == [12, 3]


def test_proposals_panel_shows_empty_and_resolved_states(qtbot) -> None:
    panel = AiProposalsPanel()
    qtbot.addWidget(panel)
    assert panel.findChild(object, "AiDraftEmptyLabel") is not None

    panel.set_drafts(
        [
            {
                "id": 5,
                "answer": "已处理。",
                "proposals": [
                    {
                        "id": 1,
                        "status": "accepted",
                        "type": "insight",
                        "content": "保持规律作息。",
                    }
                ],
            }
        ]
    )
    assert panel.findChild(QPushButton, "AiConfirmButton-5-1") is None
    assert "0 项等待确认" in panel.status_label.text()
