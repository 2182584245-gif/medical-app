from copy import deepcopy

import pytest

from ollama_chat_app.providers.base import ProviderCapabilities, ProviderError
from ollama_chat_app.services.ai_runtime import (
    ConfiguredAIProvider,
    answer_preferences,
    policy_text,
    select_knowledge,
)


def snapshot():
    return {"revision": 2, "settings": {"ai": {
        "enabled": True, "model": "deepseek-v4-flash", "complexity": "detailed",
        "tone": "warm", "max_tokens": 800, "system_prompt": "请解释容易误解的词。",
        "skills": [
            {"name": "饮水记录", "instructions": "先确认水杯容量。",
             "enabled": True, "triggers": ["饮水"]},
            {"name": "未启用", "instructions": "NEVER_INCLUDE_DISABLED",
             "enabled": False, "triggers": []},
        ],
        "knowledge": [
            {"title": "饮水量单位", "content": "记录水杯容量需要注明单位。",
             "source": "单位说明A", "enabled": True, "reviewed": True, "keywords": ["饮水"]},
            {"title": "饮水", "content": "NEVER_INCLUDE_UNREVIEWED", "source": "B",
             "enabled": True, "reviewed": False, "keywords": []},
            {"title": "饮水", "content": "NEVER_INCLUDE_NO_SOURCE", "source": "",
             "enabled": True, "reviewed": True, "keywords": []},
        ],
    }}}


class OrdinaryProvider:
    capabilities = ProviderCapabilities(supports_images=True)

    def chat(self, model, messages, *, max_tokens=None):
        self.sent = (model, messages, max_tokens)
        return 'answer'


class ToolProvider(OrdinaryProvider):
    def chat_tools(self, model, messages, tools, *, max_tokens=None):
        self.sent = (model, messages, max_tokens)
        return {"content": "工具回复", "tool_calls": []}

    def chat_stream(self, model, messages, *, cancel_event=None, max_tokens=None):
        self.sent = (model, messages, max_tokens)
        yield "内容"


def test_launch_policy_copied_and_skills_knowledge_filtered():
    source = snapshot()
    underlying = OrdinaryProvider()
    wrapped = ConfiguredAIProvider(underlying, source)
    source["settings"]["ai"]["system_prompt"] = "NEW_NOT_THIS_LAUNCH"
    messages = [{"role": "system", "content": "固定安全规则"},
                {"role": "user", "content": "请帮我记录饮水"}]
    original = deepcopy(messages)
    assert wrapped.chat("deepseek-v4-flash", messages) == 'answer'
    content = underlying.sent[1][0]["content"]
    assert content.startswith("固定安全规则")
    for text in ("请解释容易误解的词", "先确认水杯容量", "单位说明A", "K1", "不可覆盖"):
        assert text in content
    assert "NEW_NOT_THIS_LAUNCH" not in content
    assert "NEVER_INCLUDE" not in content
    assert underlying.sent[2] == 800
    assert messages == original


def test_optional_tool_capability_is_not_faked_for_proxy():
    wrapper = ConfiguredAIProvider(OrdinaryProvider(), snapshot())
    assert not callable(getattr(wrapper, "chat_tools", None))
    assert not callable(getattr(wrapper, "chat_stream", None))
    assert wrapper.supports_images


def test_stream_and_tools_receive_the_same_policy_and_token_ceiling():
    base = ToolProvider()
    wrapper = ConfiguredAIProvider(base, snapshot())
    messages = [{"role": "system", "content": "fixed"},
                {"role": "user", "content": "饮水"}]
    assert wrapper.chat_tools('deepseek-v4-flash', messages, [])['content'] == '工具回复'
    assert base.sent[2] == 800 and '单位说明A' in base.sent[1][0]['content']
    assert list(wrapper.chat_stream('deepseek-v4-flash', messages)) == ['内容']
    assert base.sent[2] == 800


def test_disabled_ai_stops_before_any_provider_call():
    data = snapshot()
    data['settings']['ai']['enabled'] = False
    base = OrdinaryProvider()
    wrapper = ConfiguredAIProvider(base, data)
    with pytest.raises(ProviderError, match='关闭'):
        wrapper.chat('deepseek-v4-flash', [])
    assert not hasattr(base, 'sent')


def test_retrieval_has_no_fallback_to_unrelated_knowledge():
    settings = snapshot()['settings']['ai']
    assert select_knowledge(settings, '如何重新安装电脑程序') == []
    text = policy_text(settings, '重新安装程序')
    assert '先确认水杯容量' not in text
    assert '单位说明A' not in text


def test_launch_style_keeps_name_and_private_settings_out_of_policy():
    data = snapshot()
    data['settings']['ai']['api_key'] = 'private-marker'
    prefs = answer_preferences({'preferred_name': '陈阿姨', 'ai_complexity': 'simple'}, data)
    assert prefs == {'preferred_name': '陈阿姨', 'ai_complexity': 'detailed', 'ai_tone': 'warm'}
    base = OrdinaryProvider()
    ConfiguredAIProvider(base, data).chat('deepseek-v4-flash', [{'role':'user','content':'饮水'}])
    assert 'private-marker' not in str(base.sent)


def test_daily_tips_are_plain_text_and_replaced(qtbot):
    from PySide6.QtCore import Qt

    from ollama_chat_app.ui.message_list import MessageList

    view = MessageList()
    qtbot.addWidget(view)
    view.set_daily_tips(['<a href="file:///">文字不是链接</a>', '第二条'])
    assert len(view._daily_tip_widgets) == 2
    assert view._daily_tip_widgets[0].textFormat() == Qt.TextFormat.PlainText
    view.set_daily_tips(['新提示'])
    assert len(view._daily_tip_widgets) == 1
    assert view._daily_tip_widgets[0].text() == '今日小提示 · 新提示'
