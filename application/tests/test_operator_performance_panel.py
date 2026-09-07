from __future__ import annotations

from ollama_chat_app.ui.operator_performance_panel import OperatorPerformancePanel


class FakeStatisticsService:
    def get_advisor_work_statistics(self, actor_user_id: int) -> dict[str, object]:
        assert actor_user_id == 9
        return {
            "notice": "只展示事实，不生成自动绩效分数。",
            "advisors": [
                {
                    "display_name": "王顾问",
                    "account_status": "active",
                    "active_member_count": 3,
                    "pending_task_count": 2,
                    "completed_task_count": 5,
                    "completed_visit_record_count": 4,
                    "recorded_visit_minutes": 180,
                    "records_without_duration_count": 1,
                }
            ],
        }


def test_operator_performance_panel_displays_auditable_facts(qtbot) -> None:
    panel = OperatorPerformancePanel(FakeStatisticsService())
    qtbot.addWidget(panel)

    panel.set_actor(9)

    assert panel.table.rowCount() == 1
    assert panel.table.item(0, 0).text() == "王顾问"
    assert panel.table.item(0, 6).text() == "180"
    assert panel.table.item(0, 7).text() == "1"
    assert "不生成自动绩效分数" in panel.notice.text()


def test_operator_performance_panel_handles_legacy_service_without_method(qtbot) -> None:
    panel = OperatorPerformancePanel(object())
    qtbot.addWidget(panel)

    panel.set_actor(9)

    assert panel.table.rowCount() == 0
    assert "未启用" in panel.status.text()
