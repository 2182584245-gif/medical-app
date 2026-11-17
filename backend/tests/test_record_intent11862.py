from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import httpx
import pytest

from test_record_assistant_try12 import DEEPSEEK, Tools, change, record, run_turn, _not_disconnected
from server.ai139_stream import AI139Runner
from server.ai139_tools import AI139Tools
from server.record_offer11862 import OFFER_TEXT, accepts_offer, declines_offer, reports_health_fact


class OfferTools(Tools):
    bind_message = AI139Tools.bind_message
    set_record_offer = AI139Tools.set_record_offer
    prior_record_offer = AI139Tools.prior_record_offer

    def __init__(self):
        super().__init__()
        self.db.transaction = lambda: nullcontext(self.db.conn)

    def turn(self, latest):
        ident = self.db.conn.execute('SELECT COALESCE(MAX(id),0)+2 FROM messages').fetchone()[0]
        self.db.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                             (ident - 1, 21, ident - 1, 'user', record.user_text(latest), '2026-01-03T14:00:00Z'))
        self.db.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                             (ident, 21, ident, 'assistant', '', '2026-01-03T14:00:01Z'))
        return ident


@pytest.mark.parametrize('message', [
    '我今天喝了半升水。', '刚走了三公里。', '我昨晚睡了七小时。', '我测到血压120/85。',
    '我今天体检了。', '今天房间温度28度，感觉热。',
])
def test_clear_facts_can_be_offered_without_authorizing_a_draft(message):
    assert reports_health_fact(message)


@pytest.mark.parametrize('message', [
    '你好，血压一般多少？', '如果今天喝了半升水会有什么影响？', '我打算去医院复查。',
    '我爸爸今天测了血压。', '她今天喝了半升水。', '我没有跑了三公里。',
])
def test_queries_plans_quotations_and_other_person_facts_do_not_get_offers(message):
    assert not reports_health_fact(message)


@pytest.mark.parametrize('reply', ['可以', '好，帮我记录一下', '就按你说的记吧', '那就记一下', '可以，时间是两点'])
def test_direct_contextual_approval(reply):
    assert accepts_offer(reply) and not declines_offer(reply)


@pytest.mark.parametrize('reply', ['不用了，谢谢', '先不记', '不要记录', '不用记，先聊聊。'])
def test_direct_contextual_decline(reply):
    assert declines_offer(reply) and not accepts_offer(reply)


@pytest.mark.parametrize('reply', [
    '可以吗', '可以不记吗', '以后再说', '好的，换个话题', '不可以', '可以，时间怎么安排', '可以，帮我看看饮食建议',
])
def test_other_replies_do_not_authorize_recording(reply):
    assert not accepts_offer(reply)


def test_report_offer_then_confirmed_intent_reuses_exact_past_user_statement():
    tools = OfferTools()
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    factual = '我今天晚上八点喝了半升水。'
    drafts, offer, upstream = run_turn(tools, factual, [], capability=cap, expect_record=False)
    assert not drafts and OFFER_TEXT in offer and tools.runs[2]['record_offer'] == 1
    assert all(row['function']['name'] != 'organize_record_drafts' for row in upstream[0].get('tools', ()))

    drafts, answer, upstream = run_turn(tools, '好，帮我记录一下',
                                        [change('water', {'amount_ml': 500}, stamp=None)], capability=cap)
    draft, = drafts
    assert draft['operation'] == 'create' and draft['details']['amount_ml'] == 500
    assert draft['occurred_at'] == '2026-01-03T20:00:00+08:00'
    assert '核对' in answer and '已保存' not in answer
    assert any(factual in json.dumps(request, ensure_ascii=False) for request in upstream[1:])


def test_declining_or_skipping_the_owned_offer_never_creates_a_draft():
    for response in ('先不记', '不用记，先聊聊。', '换个话题'):
        tools = OfferTools()
        cap = record.DIALOGUE_PHOTOS_CAPABILITY
        assert OFFER_TEXT in run_turn(tools, '我今天喝了半升水。', [], expect_record=False, capability=cap)[1]
        draft, _, _ = run_turn(tools, response, [], capability=cap, expect_record=False)
        assert not draft
        later, _, upstream = run_turn(tools, '可以', [], capability=cap, expect_record=False)
        assert not later and len(upstream) == 1
        assert all(row['function']['name'] not in {'organize_record_drafts', 'prepare_record'}
                   for row in upstream[0].get('tools', ()))


def test_approval_with_specific_time_is_contextual_and_keeps_original_day():
    tools = OfferTools()
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    response = '可以，时间是两点'
    no_offer, _, _ = run_turn(tools, response, [], capability=cap, expect_record=False)
    assert not no_offer
    assert OFFER_TEXT in run_turn(tools, '我今天下午喝了半升水。', [], expect_record=False, capability=cap)[1]
    approved, _, _ = run_turn(tools, response,
                              [change('water', {'amount_ml': 500}, stamp=None)], capability=cap)
    assert approved[0]['occurred_at'] == '2026-01-03T14:00:00+08:00'


def test_direct_refusal_without_an_offer_cannot_trigger_recording():
    tools = OfferTools()
    result, _, upstream = run_turn(tools, '不用记录，先聊聊。', [],
                                    capability=record.DIALOGUE_PHOTOS_CAPABILITY, expect_record=False)
    assert not result and all(row['function']['name'] != 'organize_record_drafts'
                              for row in upstream[0].get('tools', ()))


@pytest.mark.parametrize('utterance', ['今天天气真好', '我今天跑了半小时', '请讲一个笑话', '热量怎么算'])
def test_unrelated_chat_and_new_report_dont_use_active_record_authorization(utterance):
    tools = OfferTools()
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    ready, _, _ = run_turn(tools, '请帮我记录昨天喝了半升水，晚上八点',
                           [change('water', {'amount_ml': 500})], capability=cap)
    assert ready
    result, _, upstream = run_turn(tools, utterance, [], capability=cap, expect_record=False)
    assert not result and all(row['function']['name'] != 'organize_record_drafts'
                              for row in upstream[0].get('tools', ()))


def test_unadvertised_rogue_record_call_is_suppressed_before_any_execution_or_signal():
    tools = OfferTools()
    utterance = '我今天喝了半升水。'
    message_id = tools.turn(utterance)
    requests = []

    async def handle(request):
        upstream = json.loads(request.content)
        requests.append(upstream)
        assert upstream['stream']
        assert all(row['function']['name'] not in {'organize_record_drafts', 'prepare_record'}
                   for row in upstream.get('tools', ()))
        rogue = {'model': DEEPSEEK, 'choices': [{'finish_reason': 'tool_calls', 'delta': {'tool_calls': [{
            'index': 0, 'id': 'unrequested', 'type': 'function',
            'function': {'name': 'organize_record_drafts', 'arguments': '{}'},
        }]}}]}
        return httpx.Response(200, text='data: ' + json.dumps(rogue) + '\n\ndata: [DONE]\n',
                              headers={'content-type': 'text/event-stream'})

    async def run():
        proxy = SimpleNamespace(
            _capacity=nullcontext(), tool_service=tools,
            auth=SimpleNamespace(resolve_token=lambda _: tools.principal, identity=tools.auth.identity),
            upstream=lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        req = SimpleNamespace(is_disconnected=_not_disconnected)
        payload = {'model': DEEPSEEK, 'max_tokens': 512,
                   'messages': [{'role': 'user', 'content': utterance}],
                   '_ai139': {'assistant_team': record.DIALOGUE_PHOTOS_CAPABILITY,
                              'tool_mode': 'confirm', 'personal_context': False,
                              'assistant_message_id': message_id, 'conversation_id': 21}}
        return [event async for event in AI139Runner(proxy, req, ('synthetic', None, payload)).events()]

    events = asyncio.run(run())
    assert len(requests) == 1 and events[-1]['done']
    assert not any(event.get('type') in {'record_drafts', 'tool_start'} for event in events)
    assert OFFER_TEXT in ''.join(event.get('delta', '') for event in events)
    assert tools.runs[message_id]['record_offer'] == message_id - 1
