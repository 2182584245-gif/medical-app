"""Run the no-key acceptance journey against disposable synthetic data.

PYTHONPATH=src python tools/demo_life_agent.py [--ui]
No production database, credentials, network, or model service is used.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from pathlib import Path

from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.demo_life import DEMO_MODEL, DemoLifeProvider
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.life_agent import ConversationAgentProvider
from ollama_chat_app.services.service_management import ServiceManagementService


def run(directory: Path, *, show_ui: bool = False) -> None:
    database = Database(directory / "synthetic-demo.db")
    auth = AuthService(database)
    member = auth.register("演示会员", "synthetic-demo-password-only")
    operator = auth.bootstrap_operator("演示运营", "synthetic-demo-password-only")
    management = ServiceManagementService(database)
    advisor = management.create_advisor(
        operator.id, "演示顾问", "synthetic-demo-password-only", display_name="演示顾问"
    )
    management.bind_advisor(operator.id, member.id, advisor.id)
    ai, chat = AiAssistantService(database), ChatService(database)
    traces = []
    for prompt in ["刚才喝了300毫升水", "我不吃香菜", "明天18点提醒我散步", "总结最近记录"]:
        history = chat.context_for_provider(member.id)
        agent = ConversationAgentProvider(
            DemoLifeProvider(), history, {"role": "user", "content": prompt}, threading.Event()
        )
        exchange = chat.begin_message(member.id, prompt, provider="workflow_demo", model=DEMO_MODEL)
        result = ai.create_member_draft(member.id, agent, "workflow_demo", DEMO_MODEL, prompt)
        chat.complete_message(member.id, exchange.assistant_message.id, result["answer"])
        for proposal in result["proposals"]:
            ai.confirm_proposal(member.id, result["id"], proposal["id"])
        traces.append(
            {"input": prompt, "tools": agent.trace, "confirmed": len(result["proposals"])}
        )
    summary = ai.create_advisor_summary_draft(
        advisor.id, member.id, DemoLifeProvider(), "workflow_demo", DEMO_MODEL
    )
    report = {
        "mode": "offline_rule_demo_not_deepseek",
        "synthetic_data_only": True,
        "traces": traces,
        "saved_life_records": len(HealthService(database).list_life_records(member.id)),
        "confirmed_preferences": ai.build_member_context(member.id)["confirmed_facts"],
        "saved_reminders": len(HealthService(database).list_reminders(member.id)),
        "advisor_summary_pending": summary["proposals"][0]["status"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if show_ui:
        from PySide6.QtWidgets import QApplication, QTabWidget

        from ollama_chat_app.ui.advisor_ai_panel import AdvisorAiPanel
        from ollama_chat_app.ui.chat_page import ChatPage
        from ollama_chat_app.ui.theme import APP_STYLE

        app = QApplication([])
        app.setStyleSheet(APP_STYLE)
        window = QTabWidget()
        window.setWindowTitle("家庭生活智能体｜无 Key 验收演示（虚构数据）")
        page = ChatPage(chat, SecretStore(), ai_assistant_service=ai)
        page.start_session(member)
        page.demo_checkbox.setChecked(True)
        page.context_checkbox.setChecked(True)
        page.message_input.setPlainText("我现在出去散步了")
        panel = AdvisorAiPanel(ai, SecretStore())
        panel.start_session(advisor.id, [{"id": member.id, "display_name": "演示会员"}])
        panel.demo_checkbox.setChecked(True)
        window.addTab(page, "会员智能体")
        window.addTab(panel, "顾问复访摘要")
        window.resize(1120, 820)
        window.show()
        app.exec()


if __name__ == "__main__":
    # Windows redirected output can default to a non-Chinese code page.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui", action="store_true", help="打开真实桌面组件，使用临时虚构数据")
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="medical-agent-demo-") as temp:
        run(Path(temp), show_ui=arguments.ui)
