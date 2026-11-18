"""Synthetic feature coverage through DeepSeek dispatch and authenticated record lookup."""
from __future__ import annotations

import asyncio
import base64
from contextlib import nullcontext
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from expansion.common import entity
from expansion.contracts import NUTRIENTS
from expansion.foods import Foods
from expansion.records import Records
from ollama_chat_app.services.health import HealthService
from server.ai139_stream import AI139Runner
from server.ai139_tools import AI139Tools, ToolError
from server import record_assistant_try12 as record


DATE = '2026-01-03'
STAMP = DATE + 'T12:00:00.000000Z'
VISION = 'deepseek-v4-flash-vision-exp'
DEEPSEEK = 'deepseek-v4-flash'
PHOTO = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGNgAAAAAgAB48peWAAAAABJRU5ErkJggg=='


def change(category, details, *, operation='create', stamp=STAMP, selector=None, content=''):
    return {'operation': operation, 'category': category, 'content': content, 'details': details,
            'occurred_at': stamp, 'selector': selector or {}}


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE conversations(id INTEGER PRIMARY KEY,user_id INTEGER);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id INTEGER,sequence_no INTEGER,role TEXT,content TEXT,created_at TEXT);
            CREATE TABLE chat_attachments(id INTEGER PRIMARY KEY,message_id INTEGER,media_type TEXT,content BLOB,sha256 TEXT);
            CREATE TABLE user_files(id INTEGER PRIMARY KEY,user_id INTEGER,media_type TEXT,size_bytes INTEGER,sha256 TEXT);
            CREATE TABLE user_file_contents(file_id INTEGER PRIMARY KEY,content BLOB,mime_type TEXT,sha256 TEXT);
            CREATE TABLE life_records(id INTEGER PRIMARY KEY,user_id INTEGER,category TEXT,occurred_at TEXT,content TEXT,details_json TEXT);
            CREATE TABLE expansion_meals(id INTEGER PRIMARY KEY,user_id INTEGER,life_record_id INTEGER,occurred_at TEXT,meal_type TEXT,payload_json TEXT);
            CREATE TABLE expansion_observations(id INTEGER PRIMARY KEY,user_id INTEGER,kind TEXT,measured_at TEXT,payload_json TEXT);
            CREATE TABLE rpc_requests(user_id INTEGER, request_id TEXT, request_digest TEXT, response_json TEXT,
                                      created_at TEXT, PRIMARY KEY(user_id, request_id));
            INSERT INTO conversations VALUES(21,7),(25,8);
        ''')

    def connect(self):
        return nullcontext(self.conn)


class Expansion(Records, Foods):
    def __init__(self, db):
        self.database = db
        self.health = HealthService

    def _actor(self, actor):
        return actor

    def get_observation(self, actor, payload):
        row = self.database.conn.execute('SELECT * FROM expansion_observations WHERE id=? AND user_id=?',
                                        (payload['observation_id'], actor)).fetchone()
        if row is None:
            raise ValueError('not_found')
        return entity(row)

    def get_meal(self, actor, payload):
        row = self.database.conn.execute('SELECT * FROM expansion_meals WHERE id=? AND user_id=?',
                                        (payload['meal_id'], actor)).fetchone()
        if row is None:
            raise ValueError('not_found')
        return entity(row)


class Tools:
    def __init__(self):
        self.db = Database()
        self.auth = SimpleNamespace(identity=lambda _: nullcontext())
        self.services = {'expansion': Expansion(self.db)}
        self.memory = SimpleNamespace(settings=lambda _: self.settings, core=SimpleNamespace(
            resolve_references=self.resolve_references))
        self.settings = {'enabled': True, 'revision': 'sandbox', 'controls': [
            {'domain': 'life_records', 'enabled_for_ai': True}]}
        self.runs = {}
        self.principal = SimpleNamespace(user=SimpleNamespace(id=7, role_code='member', account_status='active'))
        self.forgot = set()
        self.reads = []

    _member = AI139Tools._member
    _replace_record_drafts = AI139Tools._replace_record_drafts
    list_run = AI139Tools.list_run
    _public_run = AI139Tools._public_run
    clock = staticmethod(lambda: datetime.now(timezone.utc).timestamp())

    def resolve_references(self, actor, refs):
        result = {}
        for value in refs:
            table, ident = value['ref'].split(':')
            assert table in {'life_records', 'expansion_observations'}
            row = self.db.conn.execute('SELECT id FROM ' + table + ' WHERE id=? AND user_id=?', (ident, actor)).fetchone()
            if row and value['ref'] not in self.forgot:
                result[value['ref']] = row
        return result

    def add_old(self, ident, category, details, *, actor=7, stamp=STAMP, content='本人旧记录'):
        self.db.conn.execute('INSERT INTO life_records VALUES(?,?,?,?,?,?)',
                             (ident, actor, category, stamp, content, json.dumps(details, ensure_ascii=False)))

    def turn(self, latest):
        ident = self.db.conn.execute('SELECT COALESCE(MAX(id),0)+2 FROM messages').fetchone()[0]
        self.db.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,NULL)', (ident - 1, 21, ident - 1, 'user', record.user_text(latest)))
        self.db.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,NULL)', (ident, 21, ident, 'assistant', ''))
        for part in latest if isinstance(latest, list) else []:
            if part['type'] != 'image_url':
                continue
            media, payload = part['image_url']['url'].removeprefix('data:').split(';base64,')
            data = base64.b64decode(payload)
            self.db.conn.execute('INSERT INTO chat_attachments(message_id,media_type,content,sha256) VALUES(?,?,?,?)',
                                 (ident - 1, media, data, hashlib.sha256(data).hexdigest()))
        return ident

    def add_photo_file(self, file_id, actor=7, *, payload=PHOTO):
        media, value = payload.removeprefix('data:').split(';base64,')
        raw = base64.b64decode(value)
        digest = hashlib.sha256(raw).hexdigest()
        self.db.conn.execute('INSERT INTO user_files VALUES(?,?,?,?,?)', (file_id, actor, media, len(raw), digest))
        self.db.conn.execute('INSERT INTO user_file_contents VALUES(?,?,?,?)', (file_id, raw, media, digest))

    def _read(self, conn, actor, assistant):
        self.reads.append((actor, assistant))
        return self.runs.get(assistant)

    def begin(self, principal, meta):
        self.runs[meta['assistant_message_id']] = {'status': 'running', 'record_drafts': []}

    def set_record_drafts(self, principal, meta, drafts):
        if meta['assistant_team'] in record.DIALOGUE_CAPABILITIES:
            self._replace_record_drafts(self.db.conn, {'actor': self.principal.user.id, **meta}, drafts)
        self.runs[meta['assistant_message_id']]['record_drafts'] = copy.deepcopy(drafts)

    def _save(self, conn, actor, run):
        assert actor == self.principal.user.id
        assert any(value is run for value in self.runs.values())

    def finish(self, principal, meta):
        self.runs[meta['assistant_message_id']]['status'] = 'complete'

    def validate_context_proof(self, principal, proof):
        if proof['schema'] == record.PROOF:
            return record.validate_target_proof(self, principal, proof)
        assert proof['settings_revision'] == self.settings['revision']
        return True

    def read_tool(self, principal, name, args, personal_context):
        assert name == 'read_profile'
        return {'profile': {}}


async def _not_disconnected():
    return False


def run_turn(tools, latest, extracted, *, personal=False, model=DEEPSEEK, expect_record=True, auto_delegate=False,
             capability=record.CAPABILITY, decided=None, reviewed=None, raw_extracted=None,
             tasks_enabled=False, task_id=None):
    ident = tools.turn(latest)
    upstream_calls = []

    async def handler(request):
        payload = json.loads(request.content)
        upstream_calls.append(payload)
        if payload['stream']:
            if any(message['role'] == 'tool' for message in payload['messages']):
                result = {'model': model, 'choices': [{'delta': {'content': '请核对记录草稿。'}, 'finish_reason': 'stop'}]}
            elif not expect_record:
                result = {'model': model, 'choices': [{'delta': {'content': '知道了。'}, 'finish_reason': 'stop'}]}
            else:
                if auto_delegate:
                    assert 'tool_choice' not in payload
                    assert any(s['function']['name'] == 'organize_record_drafts' for s in payload['tools'])
                else:
                    assert payload['tool_choice']['function']['name'] == 'organize_record_drafts'
                result = {'model': model, 'choices': [{'delta': {'tool_calls': [{
                    'index': 0, 'id': 'record_draft_call', 'type': 'function',
                    'function': {'name': 'organize_record_drafts', 'arguments': '{}'}}]},
                    'finish_reason': 'tool_calls'}]}
            return httpx.Response(200, text='data: ' + json.dumps(result) + '\n\ndata: [DONE]\n',
                                  headers={'content-type': 'text/event-stream'})
        data = {'changes': extracted}
        if '"required_review":true' in payload['messages'][1]['content'][0]['text']:
            data = {'reviewed': reviewed} if isinstance(reviewed, list) else reviewed
        content = raw_extracted if raw_extracted is not None else json.dumps(data, ensure_ascii=False)
        return httpx.Response(200, json={'model': model, 'choices': [{'finish_reason': 'stop',
            'message': {'content': content}}]})

    async def perform():
        transport = httpx.MockTransport(handler)
        proxy = SimpleNamespace(_capacity=nullcontext(), tool_service=tools,
                                auth=SimpleNamespace(resolve_token=lambda _: tools.principal, identity=tools.auth.identity),
                                upstream=lambda _: httpx.AsyncClient(transport=transport))
        options = {'model': model, 'max_tokens': 2048, 'messages': [{'role': 'system', 'content': '测试'},
            {'role': 'user', 'content': latest}], '_ai139': {'assistant_team': capability,
            'tool_mode': 'confirm', 'personal_context': personal, 'assistant_message_id': ident,
            'conversation_id': 21, 'memory_revision': tools.settings['revision']}}
        if decided:
            options['_ai139']['decided_record_draft_ids'] = decided
        if tasks_enabled:
            options['_ai139']['record_tasks'] = 'try1.2-tasks-v1'
        if task_id:
            options['_ai139']['record_task_id'] = task_id
        request = SimpleNamespace(is_disconnected=_not_disconnected)
        runner = AI139Runner(proxy, request, ('synthetic-token', None, options))
        events = [event async for event in runner.events()]
        assert runner.operation_finished and events[-1]['done'] is True
        return runner, events

    instance, events = asyncio.run(perform())
    assert tools.runs[ident]['status'] == 'complete'
    assert tools.runs[ident]['record_drafts'] == instance.record_drafts
    assert [ev['tool'] for ev in events if ev.get('type') == 'tool_done'] == (['organize_record_drafts'] if expect_record else [])
    if capability in record.DIALOGUE_CAPABILITIES and instance.record_drafts:
        card_index = next(i for i, ev in enumerate(events) if ev.get('type') == 'record_drafts')
        assert card_index < next(i for i, ev in enumerate(events) if 'delta' in ev)
        assert events[card_index]['drafts'] == record.project_record_photo_drafts(
            instance.record_drafts, photo_enabled=True, dialogue_enabled=True,
            time_hints_enabled=capability in record.DIALOGUE_TIME_CAPABILITIES,
            extended_photos=capability == record.DIALOGUE_PHOTOS_CAPABILITY,
        )
    return instance.record_drafts, ''.join(ev['delta'] for ev in events if 'delta' in ev), upstream_calls


@pytest.mark.parametrize(('category', 'fields'), [
    ('diet', {'food': '面条'}),
    ('water', {'amount_ml': 250}),
    ('activity', {'activity': '散步'}),
    ('sleep', {'duration_hours': 7}),
    ('environment', {'temperature_c': 22}),
    ('medical', {'event_type': 'consultation', 'name': '常规复查'}),
    ('metric', {'kind': 'blood_pressure', 'value': 117, 'diastolic': 79}),
    ('metric', {'kind': 'custom', 'metric_name': '自测记录', 'value': 1}),
])
def test_all_record_categories_have_reviewable_cards(category, fields):
    data, answer, calls = run_turn(Tools(), f"请帮我记录{record.CATEGORIES[category]}，当天晚上8点",
                                  [change(category, fields)])
    assert len(calls) == 2 and len(data) == 1
    assert data[0]['category'] == category and data[0]['operation'] == 'create'
    assert data[0]['missing_required'] == [] and not data[0]['blocked']
    assert '请核对' in answer


def test_image_and_optional_omission_then_record_update_and_backfill():
    tools = Tools()
    message = [{'type': 'text', 'text': '请帮我记录这张图中2026年1月3日20点的晚餐'},
               {'type': 'image_url', 'image_url': {'url': PHOTO}}]
    data, answer, calls = run_turn(tools, message, [change('diet', {'food': '苹果', 'meal': '晚餐'})], model=VISION)
    assert calls[1]['messages'][-1]['content'][1] == message[1]
    assert '热量' in [item['label'] for item in data[0]['missing_optional']]
    assert '热量' not in answer and '备注' not in answer
    assert data[0]['missing_required'] == [] and data[0]['details'].get('calories_kcal') is None

    tools.add_old(100, 'diet', {'food': '面条', 'meal': '晚餐', 'notes': '已有备注'})
    tools.add_old(101, 'diet', {'food': '隐私内容', 'meal': '晚餐'}, actor=8)
    update = [{'type': 'text', 'text': '请按图修改2026年1月3日晚餐记录，改图中的食物'}, message[1]]
    data, answer, calls = run_turn(tools, update, [change('diet', {'food': '苹果'}, operation='update',
        stamp=None, selector={'date': DATE, 'contains': '晚餐'})], personal=True, model=VISION)
    assert calls[1]['messages'][-1]['content'][1] == message[1]
    assert data[0]['target']['id'] == 100
    assert data[0]['target']['before']['details']['food'] == '面条'
    assert data[0]['details']['food'] == '苹果' and data[0]['details']['notes'] == '已有备注'
    assert tools.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0].find('面条') > 0
    assert '隐私内容' not in json.dumps(calls, ensure_ascii=False)

    latest = [{'type': 'text', 'text': '补记2026年1月3日20点图上的身体指标记录'}, message[1]]
    data, _, _ = run_turn(tools, latest, [change('metric', {'kind': 'weight', 'value': 66.2, 'unit': 'kg'})],
                          model=VISION)
    assert data[0]['operation'] == 'create' and data[0]['details']['value'] == 66.2


def meal_photo(text='请帮我记录这张餐照里的晚餐'):
    return [{'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': PHOTO}}]


def different_photo():
    header, payload = PHOTO.split(',')
    return header + ',' + base64.b64encode(base64.b64decode(payload) + b'new-fixture').decode()


@pytest.mark.parametrize(('category', 'label', 'details'), [
    ('diet', '饮食', {'food': '米饭'}),
    ('environment', '居住环境', {'description': '窗户关着'}),
    ('medical', '医疗记录', {'name': '常规复查', 'event_type': 'consultation'}),
])
def test_v3_attaches_original_for_each_photo_category(category, label, details):
    tools = Tools()
    tools.add_photo_file(32)
    drafts, answer, _ = run_turn(tools, meal_photo(f'请帮我记录{label}，原图也保存'),
                                 [change(category, details)], model=VISION,
                                 capability=record.DIALOGUE_PHOTOS_CAPABILITY)
    draft, = drafts
    assert draft['photo_file_ids'] == [32] and 'photo_file_ids' not in draft['details']
    assert not draft['blocked'] and '原图会随这条记录一起保存' in answer
    legacy, = record.project_record_photo_drafts(drafts, photo_enabled=True, dialogue_enabled=True,
                                                 time_hints_enabled=True)
    assert ('photo_file_ids' in legacy) is (category == 'diet')
    assert legacy['blocked'] is (category != 'diet')


@pytest.mark.parametrize('operation', ['update', 'supplement'])
def test_v3_appends_original_to_existing_record_and_keeps_order(operation):
    tools = Tools()
    tools.add_photo_file(32)
    tools.add_photo_file(33, payload=different_photo())
    tools.add_old(100, 'environment', {'description': '窗户关着', 'photo_file_ids': [32]})
    latest = [{'type': 'text', 'text': '给居住环境那条记录加上这张照片'},
              {'type': 'image_url', 'image_url': {'url': different_photo()}}]
    drafts, answer, _ = run_turn(tools, latest, [
        change('environment', {}, operation=operation, stamp=None, selector={'date': DATE})
    ], model=VISION, personal=True, capability=record.DIALOGUE_PHOTOS_CAPABILITY)
    draft, = drafts
    assert draft['target']['id'] == 100 and draft['photo_file_ids'] == [32, 33]
    assert draft['details']['description'] == '窗户关着' and not draft['blocked']
    assert '原图会随这条记录一起保存' in answer
    legacy, = record.project_record_photo_drafts(drafts, photo_enabled=True, dialogue_enabled=True)
    assert legacy['blocked'] and 'photo_file_ids' not in legacy
    assert json.loads(tools.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0]
                      )['photo_file_ids'] == [32]


def test_v3_text_only_vision_edit_keeps_existing_refs_without_new_image():
    tools = Tools()
    tools.add_photo_file(32)
    tools.add_photo_file(33, payload=different_photo())
    tools.add_old(100, 'medical', {'name': '旧检查', 'photo_file_ids': [32]})
    latest = [{'type': 'text', 'text': '请按图修改医疗记录的名称'},
              {'type': 'image_url', 'image_url': {'url': different_photo()}}]
    drafts, _, _ = run_turn(tools, latest, [
        change('medical', {'name': '例行检查'}, operation='update', stamp=None, selector={'date': DATE})
    ], model=VISION, personal=True, capability=record.DIALOGUE_PHOTOS_CAPABILITY)
    assert drafts[0]['photo_file_ids'] == [32] and not drafts[0]['blocked']


def test_v3_respects_selected_category_and_refuses_cross_category_originals():
    tools = Tools()
    tools.add_photo_file(32)
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    wrong, guidance, _ = run_turn(tools, meal_photo('请把这张照片保存到饮水记录'),
                                   [change('diet', {'food': '米饭'})], model=VISION, capability=cap)
    assert not wrong and '指定的类别' in guidance
    unsupported, guidance, _ = run_turn(tools, meal_photo('请把这张照片保存到饮水记录'),
                                         [change('water', {'amount_ml': 250})], model=VISION, capability=cap)
    assert not unsupported[0]['blocked'] and 'photo_file_ids' not in unsupported[0]
    assert '原图仍可在本次对话查看' in guidance and '确认后仅保存草稿中的记录内容' in guidance
    text, _, _ = run_turn(tools, meal_photo('请记录居住环境的情况，不附图'),
                          [change('environment', {'description': '窗户关着'})], model=VISION, capability=cap)
    assert not text[0]['blocked'] and 'photo_file_ids' not in text[0]


def test_v3_accumulates_old_locator_and_accepts_followup_without_an_extraction():
    tools = Tools()
    tools.add_old(100, 'water', {'amount_ml': 500})
    tools.add_old(101, 'water', {'amount_ml': 600}, stamp=DATE + 'T11:00:00.000000Z')
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    first, _, _ = run_turn(tools, '请帮我修改2026年1月3日饮水记录，改成700毫升', [
        change('water', {'amount_ml': 700}, operation='update', stamp=None, selector={'date': DATE})
    ], personal=True, capability=cap)
    assert first[0]['target'] is None and first[0]['_record_locator']['date'] == DATE
    visible, = record.project_record_photo_drafts(first, photo_enabled=True, dialogue_enabled=True,
                                                   time_hints_enabled=True, extended_photos=True)
    assert '_record_locator' not in visible
    later, _, _ = run_turn(tools, '晚上七点那条', [], personal=True, capability=cap)
    target, = later
    assert target['target']['id'] == 101 and target['details']['amount_ml'] == 700
    assert target['supersedes_draft_id'] == first[0]['id']


def test_v3_contextual_attachment_only_reply_keeps_bound_target():
    tools = Tools()
    tools.add_photo_file(32)
    tools.add_old(100, 'environment', {'description': '窗户关着'})
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    first, _, _ = run_turn(tools, '请把环境记录的说明改成窗户打开了', [
        change('environment', {'description': '窗户打开了'}, operation='update', stamp=None, selector={'date': DATE})
    ], personal=True, capability=cap)
    revised, _, _ = run_turn(tools, meal_photo('原图也附上'), [], personal=True, capability=cap, model=VISION)
    draft, = revised
    assert draft['target']['id'] == 100 and draft['photo_file_ids'] == [32]
    assert draft['details']['description'] == '窗户打开了'
    assert draft['supersedes_draft_id'] == first[0]['id']


def test_v3_pending_old_time_change_waits_for_clock_and_retains_target():
    tools = Tools()
    tools.add_old(100, 'water', {'amount_ml': 500})
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    first, answer, _ = run_turn(tools, '请把旧饮水记录的发生时间改到晚上', [
        change('water', {}, operation='update', stamp=DATE + 'T11:00:00Z', selector={'date': DATE})
    ], personal=True, capability=cap)
    assert first[0]['target']['id'] == 100 and first[0]['occurred_at'] is None
    assert first[0]['record_date'] == DATE and first[0]['record_time_hint'] == 'evening'
    assert first[0]['missing_required'][0]['field'] == 'record_time' and '大概几点' in answer
    later, _, _ = run_turn(tools, '大概八点半', [], personal=True, capability=cap)
    assert later[0]['target']['id'] == 100 and later[0]['occurred_at'] == DATE + 'T20:30:00+08:00'
    assert later[0]['target']['before']['occurred_at'] == STAMP


def test_owned_photo_is_bound_to_the_diet_draft_and_legacy_stays_compatible():
    tools = Tools()
    tools.add_photo_file(32)
    tools.add_photo_file(48, actor=8)
    meal = change('diet', {'food': '米饭和番茄'})
    drafts, answer, calls = run_turn(
        tools, meal_photo('请把这张餐照记到饮食里，并分析搭配'), [meal], model=VISION,
        capability=record.PHOTO_CAPABILITY,
    )
    assert drafts[0]['photo_file_ids'] == [32]
    assert not drafts[0]['blocked'] and 'photo_file_ids' not in drafts[0]['details']
    # A compound answer can depend on record details, but the upstream model
    # never receives private record file identifiers.
    tool_inputs = [json.loads(item['content']) for item in calls[-1]['messages'] if item['role'] == 'tool']
    assert len(tool_inputs) == 1 and 'photo_file_ids' not in tool_inputs[0]['record_drafts'][0]
    run = dict(assistant_message_id=2, proposals=[], sources=[], record_assistant_try12=True,
               status='complete', record_drafts=drafts)
    assert AI139Tools._public_run(None, run, photo_enabled=True)['record_drafts'] == drafts
    old_draft, = AI139Tools._public_run(None, run)['record_drafts']
    assert old_draft['id'] == drafts[0]['id'] and old_draft['blocked'] and 'photo_file_ids' not in old_draft
    text_only, _, _ = run_turn(tools, meal_photo(), [meal], model=VISION)
    assert not text_only[0]['blocked'] and 'photo_file_ids' not in text_only[0]


def test_diet_photo_continuation_keeps_exact_photo_and_checks_it_still_exists():
    tools = Tools()
    tools.add_photo_file(32)
    meal = change('diet', {'food': '米饭和番茄'}, stamp=None)
    partial, ask, _ = run_turn(tools, meal_photo(), [meal], model=VISION, capability=record.PHOTO_CAPABILITY)
    assert partial[0]['photo_file_ids'] == [32]
    assert partial[0]['missing_required'] and '请补充' in ask
    ready, answer, _ = run_turn(tools, '那条是今天晚上八点', [dict(meal, occurred_at=STAMP)],
                                capability=record.PHOTO_CAPABILITY)
    assert ready[0]['photo_file_ids'] == [32]
    assert not ready[0]['missing_required'] and '一起保存' in answer
    run_turn(tools, meal_photo(), [meal], model=VISION, capability=record.PHOTO_CAPABILITY)
    tools.db.conn.execute('DELETE FROM user_files WHERE id=32')
    removed, answer, _ = run_turn(tools, '那条是今天晚上八点', [dict(meal, occurred_at=STAMP)],
                                  capability=record.PHOTO_CAPABILITY)
    assert removed[0]['blocked'] and 'photo_file_ids' not in removed[0]
    assert '重新发送餐照' in answer


def test_diet_photo_accepts_plain_food_list_and_omits_blank_text():
    tools = Tools()
    tools.add_photo_file(32)
    meal = change('diet', {'food': [' 米饭 ', '番茄'], 'notes': ' \t '}, content='  ')
    drafts, _, _ = run_turn(tools, meal_photo(), [meal], model=VISION, capability=record.PHOTO_CAPABILITY)
    draft, = drafts
    assert draft['photo_file_ids'] == [32] and draft['details']['food'] == '米饭、番茄'
    assert draft['content'] == '米饭、番茄' and 'notes' not in draft['details']
    for invalid in ([{'name': '米饭'}], ['米饭', 1], {'name': '米饭'}):
        with pytest.raises(ToolError):
            record.changes({'changes': [change('diet', {'food': invalid})]})


def test_never_binds_a_different_account_or_earlier_unrelated_turn_photo():
    tools = Tools()
    tools.add_photo_file(48, actor=8)
    meal = change('diet', {'food': '米饭和番茄'})
    denied, answer, _ = run_turn(tools, meal_photo(), [meal], model=VISION, capability=record.PHOTO_CAPABILITY)
    assert denied[0]['blocked'] and 'photo_file_ids' not in denied[0] and '重新发送' in answer
    tools.add_photo_file(32)
    tools.turn(meal_photo('图片给你看一下'))
    clean, _, _ = run_turn(tools, '请记录我昨天晚上八点的晚餐', [meal], capability=record.PHOTO_CAPABILITY)
    assert not clean[0]['blocked'] and 'photo_file_ids' not in clean[0]
    skipped, _, _ = run_turn(tools, meal_photo('请帮我记录晚餐，不要附上照片'), [meal],
                              model=VISION, capability=record.PHOTO_CAPABILITY)
    assert not skipped[0]['blocked'] and 'photo_file_ids' not in skipped[0]


def test_required_followup_success_and_refusal_blocks_even_if_model_guesses():
    tools = Tools()
    part, ask, _ = run_turn(tools, '请记录一下喝水，晚上八点', [change('water', {})])
    assert [field['field'] for field in part[0]['missing_required']] == ['amount_ml']
    assert '请补充' in ask and '毫升' in ask
    ready, answer, calls = run_turn(tools, '喝了250毫升', [change('water', {'amount_ml': 250}, stamp=None)])
    assert ready[0]['occurred_at'] == part[0]['occurred_at'] and ready[0]['missing_required'] == []
    context = calls[1]['messages'][1]['content'][0]['text']
    assert 'previous_incomplete_drafts' in context and '请记录一下喝水' in context
    assert '请核对' in answer

    part, _, _ = run_turn(tools, '请记录饮水，晚上八点', [change('water', {})])
    refused, answer, _ = run_turn(tools, '我不想说', [change('water', {'amount_ml': 200}, stamp=None)])
    assert refused[0]['blocked'] and refused[0]['details'] == {}
    assert '不能保存' in answer and '请补充' not in answer and '？' not in answer
    assert not record.previous_turn(tools, tools.principal, {
        'conversation_id': 21, 'assistant_message_id': tools.turn('谢谢'),
    })


def test_supplement_ambiguity_authorization_and_consent_revalidation():
    tools = Tools()
    tools.add_old(1, 'activity', {'activity': '散步', 'duration_minutes': 20})
    tools.add_old(2, 'activity', {'activity': '跑步', 'duration_minutes': 60})
    line = change('activity', {'duration_minutes': 99, 'steps': 1300}, operation='supplement',
                  stamp=None, selector={'date': DATE})
    drafts, answer, _ = run_turn(tools, '请补充2026年1月3日的运动记录', [line], personal=True)
    assert '多条' in answer and drafts[0]['missing_required'][0]['field'] == 'target'
    narrowed, answer, _ = run_turn(tools, '是那条散步记录', [dict(line, selector={'date': DATE, 'contains': '散步'},
        details={})], personal=True)
    assert narrowed[0]['target']['id'] == 1
    assert narrowed[0]['details']['duration_minutes'] == 20
    assert narrowed[0]['details']['steps'] == 1300 and '请核对' in answer
    denied, answer, _ = run_turn(tools, '请修改2026年1月3日运动记录', [line], personal=False)
    assert denied[0]['blocked'] and denied[0]['target'] is None and '权限' in answer
    _, proof, _ = record.lookup(tools, tools.principal, 'activity', {'date': DATE, 'contains': '散步'})
    tools.db.conn.execute("UPDATE life_records SET content='已变更' WHERE id=1")
    with pytest.raises(ToolError, match='record_assistant_target_changed'):
        record.validate_target_proof(tools, tools.principal, proof)
    tools.forgot.add('life_records:1')
    target, _, _ = record.lookup(tools, tools.principal, 'activity', {'date': DATE, 'contains': '散步'})
    assert target is None


def test_no_target_id_or_photo_ids_can_be_supplied_by_model():
    with pytest.raises(ToolError):
        record.changes({'changes': [change('diet', {'food': '水果', 'photo_file_ids': [101]})]})
    with pytest.raises(ToolError):
        record.changes({'changes': [change('diet', {'food': '水果'}, operation='update', selector={'id': 101})]})


def test_legacy_linked_meal_keeps_item_list_and_uses_existing_cas_projection():
    tools = Tools()
    tools.add_old(100, 'diet', {'amount_g': 100}, content='苹果')
    original = {'items': [{'food_id': None, 'name': '苹果', 'amount_g': 100,
                           'nutrients': {key: None for key in NUTRIENTS}}],
                'notes': '', 'photo_file_id': None}
    tools.db.conn.execute('INSERT INTO expansion_meals VALUES(?,?,?,?,?,?)',
                          (200, 7, 100, STAMP, 'dinner', json.dumps(original, ensure_ascii=False)))
    row = change('diet', {'notes': '饭后自述'}, operation='supplement', stamp=None,
                 selector={'date': DATE, 'time': '20:00:00', 'contains': '晚餐'})
    drafts, answer, _ = run_turn(tools, '请给2026年1月3日20点的晚餐记录补充备注', [row], personal=True)
    draft = drafts[0]
    assert not draft['blocked'] and draft['target']['linked_meal_id'] == 200
    assert draft['target']['linked_meal']['changes'] == {'notes': '饭后自述'}
    assert draft['details']['food'] == '苹果' and draft['details']['amount_g'] == 100
    assert draft['target']['before']['details'].get('notes') is None
    assert draft['details']['notes'] == '饭后自述' and '请核对' in answer
    target_row, proof, _ = record.lookup(tools, tools.principal, 'diet', {'date': DATE, 'contains': '晚餐'})
    assert target_row['linked_meal_id'] == 200
    tools.db.conn.execute('UPDATE expansion_meals SET meal_type=? WHERE id=?', ('lunch', 200))
    with pytest.raises(ToolError, match='record_assistant_target_changed'):
        record.validate_target_proof(tools, tools.principal, proof)


def test_optional_omission_never_activates_a_followup_on_ready_card():
    tools = Tools()
    draft, _, _ = run_turn(tools, '请记录饮水，晚上八点喝了250毫升', [change('water', {'amount_ml': 250})])
    assert draft[0]['missing_optional'] == [{'field': 'notes', 'label': '备注'}]
    refused, answer, calls = run_turn(tools, '备注不想说', [], expect_record=False)
    assert not refused and not calls and '可' in answer and '确认保存' in answer and '？' not in answer
    assert tools.runs[2]['record_drafts'][0]['missing_required'] == []


def test_main_does_not_delegate_on_a_tracking_goal_and_report_alone():
    draft, _, calls = run_turn(Tools(), '想跟踪喝水习惯，刚喝了250毫升。', [], expect_record=False)
    assert not draft and len(calls) == 1
    assert all(row['function']['name'] != 'organize_record_drafts' for row in calls[0].get('tools', ()))


def test_authenticated_capability_endpoint_and_draft_status_marker():
    from fastapi import FastAPI
    from server.platform_ai_proxy import AIProxyError, create_ai_router
    tools = Tools()

    async def authorize(request):
        if request.headers.get('Authorization') != 'Bearer synthetic':
            raise AIProxyError(401, 'authentication_required')
        return 'synthetic', tools.principal

    app = FastAPI()
    app.include_router(create_ai_router(SimpleNamespace(tool_service=tools, authorize=authorize)))
    endpoint = '/v1/ai/tools/capabilities'

    async def check_capability():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            assert (await client.get(endpoint)).status_code == 401
            allowed = await client.get(endpoint, headers={'Authorization': 'Bearer synthetic'})
            assert allowed.json() == {'record_assistant': record.CAPABILITY,
                                      'record_photo_attachment': record.PHOTO_CAPABILITY,
                                      'record_dialogue': record.DIALOGUE_CAPABILITY,
                                      'record_dialogue_time_hints': record.DIALOGUE_TIME_HINTS_CAPABILITY,
                                      'record_dialogue_photos': record.DIALOGUE_PHOTOS_CAPABILITY,
                                      'record_tasks': 'try1.2-tasks-v1'}

    asyncio.run(check_capability())
    run = {'assistant_message_id': 11, 'conversation_id': 21,
           'status': 'running', 'record_assistant_try12': True, 'record_drafts': [], 'sources': [], 'proposals': []}
    assert AI139Tools._public_run(tools, run)['record_drafts_ready'] is False
    assert AI139Tools._public_run(tools, {**run, 'status': 'complete'})['record_drafts_ready'] is True


@pytest.mark.parametrize('team', sorted(record.DIALOGUE_CAPABILITIES))
def test_dialogue_teams_support_decided_drafts_and_a_broad_time_reply(team):
    from uuid import uuid4
    from server.platform_ai_proxy import AIRequest, request_payload

    tools = Tools()
    draft, _, _ = run_turn(tools, '请记录昨天晚上喝了250毫升水',
                           [change('water', {'amount_ml': 250}, stamp=None)], capability=team)
    request = AIRequest(model=DEEPSEEK, messages=[{'role': 'user', 'content': '大概八点半'}],
                        tool_mode='confirm', assistant_team=team, assistant_message_id=4, conversation_id=21,
                        operation_id=str(uuid4()), operation_kind='chat.send', decided_record_draft_ids=[draft[0]['id']])
    assert request_payload(request)['_ai139']['decided_record_draft_ids'] == [draft[0]['id']]
    reply, _, _ = run_turn(tools, '大概八点半', [], capability=team)
    assert reply[0]['occurred_at'] == draft[0]['record_date'] + 'T20:30:00+08:00'
    assert reply[0]['supersedes_draft_id'] == draft[0]['id']


def test_dialogue_v2_and_v3_get_time_hints_from_proposals_and_recovery():
    from fastapi import FastAPI
    from server.platform_ai_proxy import create_ai_router
    from uuid import uuid4

    tools = Tools()
    original, _, _ = run_turn(tools, '请记录昨天晚上喝了250毫升水',
                              [change('water', {'amount_ml': 250}, stamp=None)],
                              capability=record.DIALOGUE_TIME_HINTS_CAPABILITY)
    tools.runs[2].update(assistant_message_id=2, sources=[], proposals=[], record_assistant_try12=True)

    async def authorize(_request):
        return 'synthetic', tools.principal

    def recover(principal, identifier, kind):
        assert principal == tools.principal and kind == 'chat.send'
        return {'state': 'complete', 'response': {'record_drafts': original}}

    proxy = SimpleNamespace(tool_service=tools, authorize=authorize, operation_ledger=SimpleNamespace(view=recover))
    app = FastAPI()
    app.include_router(create_ai_router(proxy))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            for team in record.DIALOGUE_CAPABILITIES:
                keep_hint = team in record.DIALOGUE_TIME_CAPABILITIES
                proposals = await client.get('/v1/ai/tools/proposals', params={'assistant_message_id': 2,
                                                                              'record_dialogue': team})
                recovery = await client.get('/v1/ai/operations/' + str(uuid4()),
                                            params={'kind': 'chat.send', 'record_dialogue': team})
                assert proposals.status_code == recovery.status_code == 200
                for card in (proposals.json()['record_drafts'][0], recovery.json()['response']['record_drafts'][0]):
                    assert ('record_time_hint' in card) is keep_hint
                    assert card['record_date'] == original[0]['record_date']

    asyncio.run(check())


def test_record_dialogue_fills_only_missing_form_values_and_retains_photo():
    tools = Tools()
    tools.add_photo_file(32)
    message = meal_photo(f'请帮我记下{DATE}吃的午餐，米饭和番茄。把这张照片也存到饮食记录里，具体时间我还没告诉你。')
    initial, ask, _ = run_turn(
        tools, message,
        [change('diet', {'food': '米饭、番茄', 'meal_type': 'lunch'}, stamp=None)],
        model=VISION, capability=record.DIALOGUE_CAPABILITY,
    )
    assert initial[0]['record_date'] == DATE and initial[0]['photo_file_ids'] == [32]
    assert initial[0]['missing_required'] == [{'field': 'record_time', 'label': '发生时间'}]
    assert '方便直接在这里' in ask and '发生时间' in ask and '记录日期' not in ask
    with_time, answer, _ = run_turn(
        tools, '发生时间是中午12:15。',
        [change('diet', {}, stamp=None)],
        capability=record.DIALOGUE_CAPABILITY,
    )
    assert with_time[0]['occurred_at'] == DATE + 'T12:15:00+08:00'
    assert with_time[0]['supersedes_draft_id'] == initial[0]['id']
    assert '请核对更新后的草稿' in answer and '热量、其他说明' in answer and '都可以留空' in answer
    assert tools.runs[2]['record_drafts'][0]['superseded'] is True
    assert record.draft_was_replaced(tools.db.conn, 7, initial[0]['id'])
    assert not record.draft_was_replaced(tools.db.conn, 8, initial[0]['id'])
    ready, final, calls = run_turn(
        tools, '热量补充420千卡，其他说明写少盐。',
        [change('diet', {'calories_kcal': 420, 'notes': '少盐', 'food': '不得覆盖'},
                operation='supplement', stamp=None, selector={'date': '2026-11-14'})],
        capability=record.DIALOGUE_CAPABILITY,
    )
    assert ready[0]['photo_file_ids'] == [32] and ready[0]['supersedes_draft_id'] == with_time[0]['id']
    assert ready[0]['operation'] == 'create' and ready[0]['target'] is None
    assert ready[0]['details']['food'] == '米饭、番茄'
    assert ready[0]['details']['calories_kcal'] == 420 and ready[0]['details']['notes'] == '少盐'
    assert '请核对更新后的草稿' in final and '留空' not in final
    assert 'dialogue_form' in calls[-1]['messages'][1]['content'][0]['text']
    superseded = tools.runs[4]['record_drafts'][0]
    assert superseded['blocked'] is True
    assert 'superseded' not in record.project_record_photo_drafts([superseded], photo_enabled=True)[0]
    assert record.project_record_photo_drafts([superseded], photo_enabled=True, dialogue_enabled=True)[0]['superseded']


@pytest.mark.parametrize(('message', 'expected'), [
    ('时间是下午2:15。', {'record_time': '14:15'}),
    ('2026年1月3日T12:15饮水', {'record_date': DATE, 'record_time': '12:15'}),
    ('请记录2026-02-31的午饭', {}),
    ('2026-01-03还是2026-01-04记不得', {}),
    ('喝了点水，配方比例是12:15。', {}),
    ('时间是上午10:15还是下午2:15我记不清。', {}),
])
def test_local_time_fallback_recovers_only_clear_user_values(message, expected):
    assert record._explicit_time_parts(message) == expected


@pytest.mark.parametrize(('message', 'period', 'expected'), [
    ('请记今天晚上喝了水', None, {'record_date': '2026-01-05', 'record_time_hint': 'evening'}),
    ('昨晚十点半', None, {'record_date': '2026-01-04', 'record_time': '22:30'}),
    ('大概八点左右', 'evening', {'record_time': '20:00'}),
    ('差不多八点十五分吧', 'evening', {'record_time': '20:15'}),
    ('上午九点一刻', None, {'record_time': '09:15'}),
    ('凌晨12点零五分', None, {'record_time': '00:05'}),
    ('今晚七到八点', None, {'record_date': '2026-01-05', 'record_time_hint': 'evening'}),
    ('不是今天，是昨天', None, {'record_date': '2026-01-04'}),
    ('我刚才喝了半升水', None, {'record_date': '2026-01-05', 'record_time': '02:43'}),
    ('刚才的饮水量了三百毫升', None, {'record_date': '2026-01-05', 'record_time': '02:43'}),
    ('以晚餐为准。热量先不填，其他都按刚才的草稿。', None, {}),
    ('照刚才的说法，餐次是晚餐', None, {}),
    ('发生时间确定是今晚八点，晚餐，今天20:00。以这个时间为准。热量不填。', None,
     {'record_date': '2026-01-05', 'record_time': '20:00'}),
    ('今晚八点，也就是20:00', None, {'record_date': '2026-01-05', 'record_time': '20:00'}),
    ('今晚八点，今天08:00', None, {'record_date': '2026-01-05', 'record_time_hint': 'evening'}),
    ('现在帮我记录喝了一点水', None, {}),
    ('下午锻炼了两个小时', None, {'record_time_hint': 'afternoon'}),
])
def test_time_parts_accept_civil_relative_dates_and_spoken_clocks(message, period, expected):
    reference = datetime(2026, 1, 5, 2, 43, 22, tzinfo=record.BEIJING)
    assert record._explicit_time_parts(message, reference=reference, prior_period=period) == expected


@pytest.mark.parametrize(('category', 'written', 'details'), [
    ('diet', '吃了米饭', {'food': '米饭', 'meal_type': 'dinner'}),
    ('water', '喝了半升水', {'amount_ml': 500}),
    ('activity', '跑步半小时', {'activity': '跑步', 'duration_minutes': 30}),
    ('sleep', '睡了7小时', {'duration_hours': 7}),
    ('environment', '室温22度', {'temperature_c': 22}),
    ('medical', '做了常规复查', {'name': '常规复查', 'event_type': 'consultation'}),
    ('metric', '血压117/79', {'kind': 'blood_pressure', 'value': 117, 'diastolic': 79}),
])
def test_relative_daypart_then_approximate_hour_for_every_record_form(category, written, details):
    tools = Tools()
    options = {'capability': record.DIALOGUE_CAPABILITY}
    draft, ask, _ = run_turn(tools, f'请记录昨天晚上{written}', [change(category, details)], **options)
    yesterday = (datetime.now(record.BEIJING).date() - record.timedelta(days=1)).isoformat()
    assert draft[0]['record_date'] == yesterday and draft[0]['record_time_hint'] == 'evening'
    assert draft[0]['occurred_at'] is None and 'record_time' not in draft[0]
    assert [field['field'] for field in draft[0]['missing_required']] == ['record_time']
    assert '我已记下是晚上，大概几点就可以' in ask
    older_projection = record.project_record_photo_drafts(draft, photo_enabled=True, dialogue_enabled=False)
    assert 'record_time_hint' not in older_projection[0]
    answered, reply, _ = run_turn(tools, '大概八点左右', [], **options)
    assert answered[0]['occurred_at'] == yesterday + 'T20:00:00+08:00'
    assert answered[0]['supersedes_draft_id'] == draft[0]['id']
    assert 'record_time_hint' not in answered[0] and '确认' in reply


def test_time_period_persists_across_an_intervening_required_answer():
    tools = Tools()
    options = {'capability': record.DIALOGUE_CAPABILITY}
    initial, _, _ = run_turn(tools, '请记录昨晚的晚餐', [change('diet', {'meal': '晚餐'}, stamp=None)], **options)
    with_food, ask, _ = run_turn(tools, '豆腐和青菜', [change('diet', {'food': '豆腐和青菜'}, stamp=None)], **options)
    assert with_food[0]['record_time_hint'] == 'evening' and with_food[0]['record_date'] == initial[0]['record_date']
    assert '发生时间' in ask and '吃了什么' not in ask
    ready, _, _ = run_turn(tools, '九点半', [], **options)
    assert ready[0]['occurred_at'] == initial[0]['record_date'] + 'T21:30:00+08:00'
    assert ready[0]['details']['food'] == '豆腐和青菜'


@pytest.fixture
def beijing_afternoon(monkeypatch):
    moment = datetime(2026, 1, 5, 15, 30, tzinfo=record.BEIJING)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr(record, 'datetime', FixedDateTime)
    return moment.date().isoformat()


@pytest.mark.parametrize('extracted', [
    [],
    [change('diet', {}, stamp='2026-01-05T20:00:00+08:00')],
    [change('water', {'amount_ml': 200}, stamp='2026-01-05T20:00:00+08:00')],
])
def test_tonight_at_eight_is_retained_when_answering_the_open_record_question(beijing_afternoon, extracted):
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    tools.add_photo_file(32)
    original = '请记录我的晚饭，吃了米饭和青菜。把这张照片原图也存到饮食记录。'
    initial, question, _ = run_turn(tools, meal_photo(original),
                                    [change('diet', {'meal': '晚餐', 'food': '米饭和青菜'}, stamp=None)],
                                    model=VISION, **options)
    assert {item['field'] for item in initial[0]['missing_required']} == {'record_date', 'record_time'}
    tools.db.conn.execute('UPDATE messages SET content=? WHERE id=2', (question,))
    completed, answer, calls = run_turn(tools, '今晚八点', extracted, **options)
    assert completed[0]['occurred_at'] == beijing_afternoon + 'T20:00:00+08:00'
    assert completed[0]['photo_file_ids'] == [32] and completed[0]['supersedes_draft_id'] == initial[0]['id']
    assert not completed[0]['missing_required'] and '确认' in answer and '还缺' not in answer
    specialty = calls[-1]['messages']
    context = json.loads(specialty[1]['content'][0]['text'].split('\n', 1)[1])
    assert context['recent_dialogue'] == [{'role': 'user', 'text': original}, {'role': 'assistant', 'text': question}]
    assert context['previous_incomplete_drafts'][0]['details']['food'] == '米饭和青菜'
    assert specialty[2]['content'][0]['text'] == '今晚八点'
    optional, _, _ = run_turn(tools, '用清水煮的', [change('diet', {'notes': '用清水煮的'}, stamp=None)], **options)
    assert optional[0]['details']['notes'] == '用清水煮的'
    assert optional[0]['occurred_at'] == completed[0]['occurred_at'] and optional[0]['photo_file_ids'] == [32]


@pytest.mark.parametrize('statement', ['今晚八点提醒我吃饭', '我打算今晚八点吃面条'])
def test_plans_and_reminders_do_not_complete_an_open_record(beijing_afternoon, statement):
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    first, _, _ = run_turn(tools, '请记录晚餐吃了青菜', [change('diet', {'food': '青菜'}, stamp=None)], **options)
    assert first[0]['missing_required']
    next_drafts, _, _ = run_turn(tools, statement, [], expect_record=False, **options)
    assert not next_drafts and not tools.runs[2]['record_drafts'][0]['blocked']
    forced, _, _ = record.make_drafts([change('diet', {'food': '面条'}, stamp=None)], tools=tools,
                                      principal=tools.principal, personal=False, prior={}, latest=statement,
                                      dialogue_enabled=True)
    assert forced == []


def test_future_time_is_not_accepted_as_unanchored_or_tomorrow(beijing_afternoon):
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    first, _, _ = run_turn(tools, '请记录今晚八点吃了青菜',
                            [change('diet', {'food': '青菜'}, stamp='2026-01-05T20:00:00+08:00')], **options)
    assert first[0]['occurred_at'] is None and fields_for_draft(first[0]) == {'record_time'}
    tomorrow, clarify, _ = run_turn(tools, '明晚八点', [], **options)
    assert tomorrow[0]['occurred_at'] is None and 'record_date' in fields_for_draft(tomorrow[0])
    assert '以哪个为准' in clarify
    agreed, _, _ = run_turn(tools, '就按后一个', [], **options)
    assert agreed[0]['occurred_at'] is None and fields_for_draft(agreed[0]) == {'record_time'}


def fields_for_draft(draft):
    return {item['field'] for item in draft['missing_required']}


def test_resolving_meal_conflict_keeps_time_and_original_photo(beijing_afternoon):
    tools = Tools()
    tools.add_photo_file(32)
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    initial, _, _ = run_turn(tools, meal_photo('请记录今天午饭吃面，把这张照片原图也存到饮食记录。'),
                             [change('diet', {'meal': '午餐', 'food': '面'}, stamp=None)], model=VISION, **options)
    conflict, _, _ = run_turn(tools, '今晚八点吃的，是晚餐。',
                              [change('diet', {'meal': '晚餐'}, stamp=None)], **options)
    assert [item['field'] for item in conflict[0]['_draft_conflicts']] == ['meal']
    resolved, _, _ = run_turn(tools, '以晚餐为准。热量先不填，其他都按刚才的草稿。',
                              [change('diet', {'meal': '晚餐'}, stamp=beijing_afternoon+'T14:00:00+08:00')], **options)
    assert resolved[0]['occurred_at'] == beijing_afternoon+'T20:00:00+08:00'
    assert resolved[0]['details']['meal'] == '晚餐'
    assert resolved[0]['photo_file_ids'] == initial[0]['photo_file_ids'] == [32]
    assert not resolved[0]['blocked'] and not resolved[0]['missing_required']


def test_refusal_applies_to_the_named_field_only():
    assert not record._refused('发生时间确定是今晚八点，晚餐，今天20:00。以这个时间为准。热量不填。', ['record_time'])
    assert record._refused('热量不填。发生时间也不记得。', ['record_time'])
    assert record._refused('我不想说。', ['record_time'])


@pytest.mark.parametrize('model_fields', [
    {'meal': '晚餐'}, {'meal_type': 'dinner', 'meal': '午餐'},
    {'meal': '午餐', 'meal_type': 'lunch'}, {},
])
def test_real_meal_aliases_follow_owner_choice_through_photo_draft(beijing_afternoon, model_fields):
    tools = Tools()
    tools.add_photo_file(32)
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY}
    initial, _, _ = run_turn(tools, meal_photo('请记录今天午饭吃番茄意面，保存原图。'),
        [change('diet', {'food': '番茄意面', 'meal': '午餐', 'meal_type': 'lunch', 'notes': '云验收'}, stamp=None)],
        model=VISION, **options)
    conflict, ask, _ = run_turn(tools, '今晚八点吃的，是晚餐。',
                                [change('diet', model_fields, stamp=None)], **options)
    assert fields_for_draft(conflict[0]) == {'meal'} and '以哪个为准' in ask
    ready, _, _ = run_turn(tools, '以晚餐为准。热量先不填，请保留原照片，其他都按刚才的草稿。',
                           [change('diet', model_fields, stamp=None)], **options)
    draft, = ready
    assert draft['details'] == {'meal': '晚餐', 'meal_type': 'dinner', 'food': '番茄意面', 'notes': '云验收'}
    assert draft['content'] == '晚餐 · 番茄意面'
    assert draft['photo_file_ids'] == initial[0]['photo_file_ids'] == [32]
    assert draft['occurred_at'] == beijing_afternoon+'T20:00:00+08:00'
    assert not draft['missing_required'] and not draft['blocked']


@pytest.mark.parametrize('category, original, patch, words, expected', [
    ('diet', {'meal_type': 'lunch', 'food': '面'}, {'meal': '晚餐'}, '晚餐',
     {'meal_type': 'dinner', 'meal': '晚餐'}),
    ('activity', {'activity': '散步', 'activity_type': '散步'}, {'activity_type': '跑步'}, '跑步',
     {'activity': '跑步', 'activity_type': '跑步'}),
])
def test_semantic_aliases_in_old_record_update_and_supplement(category, original, patch, words, expected):
    for operation in ('update', 'supplement'):
        tools = Tools()
        tools.add_old(52, category, original)
        changes = {**patch, 'notes': '体感良好'}
        name = '修改' if operation == 'update' else '补充'
        output, _, _ = run_turn(tools, f'请{name}2026年1月3日的{record.CATEGORIES[category]}记录，{words}，备注体感良好。',
            [change(category, changes, operation=operation, stamp=None, selector={'date': DATE})],
            personal=True, capability=record.DIALOGUE_PHOTOS_CAPABILITY)
        result = output[0]['details']
        assert result['notes'] == '体感良好'
        if operation == 'update':
            assert {key: result.get(key) for key in expected} == expected
        else:
            assert result == {**original, 'notes': '体感良好'}
        assert output[0]['target']['before']['details'] == original


def test_disagreeing_model_aliases_require_user_choice():
    tools = Tools()
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY}
    drafts, ask, _ = run_turn(tools, '请记录昨天晚上八点吃了面，我不确定是午餐还是晚餐。',
        [change('diet', {'meal_type': 'lunch', 'meal': '晚餐', 'food': '面'}, stamp=None)], **options)
    assert fields_for_draft(drafts[0]) == {'meal'} and '以哪个为准' in ask
    final, _, _ = run_turn(tools, '以晚餐为准。', [], **options)
    assert record._normalized_fact('meal', final[0]['details'].get('meal_type') or final[0]['details']['meal']) == '晚餐'
    assert not final[0]['missing_required']


def test_linked_meal_save_payload_and_card_keep_confirmed_meal_through_later_edit():
    tools = Tools()
    tools.add_old(100, 'diet', {'amount_g': 100}, content='苹果')
    old = {'items': [{'food_id': None, 'name': '苹果', 'amount_g': 100,
                      'nutrients': {key: None for key in NUTRIENTS}}], 'notes': '', 'photo_file_id': None}
    tools.db.conn.execute('INSERT INTO expansion_meals VALUES(?,?,?,?,?,?)',
                          (200, 7, 100, STAMP, 'lunch', json.dumps(old, ensure_ascii=False)))
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY, 'personal': True}
    first, _, _ = run_turn(tools, '请把2026年1月3日的午餐记录的餐次改为晚餐。',
        [change('diet', {'meal_type': 'lunch', 'meal': '晚餐'}, operation='update', stamp=None,
                selector={'date': DATE})], **options)
    assert first[0]['target']['linked_meal']['changes'] == {'meal_type': 'dinner'}
    final, _, _ = run_turn(tools, '给这条原记录补充备注，饭后散步。', [
        change('diet', {'notes': '饭后散步'}, operation='update', stamp=None)], **options)
    draft, = final
    assert draft['target']['linked_meal']['changes'] == {'meal_type': 'dinner', 'notes': '饭后散步'}
    assert draft['details']['meal_type'] == 'dinner' and draft['details']['meal'] == '晚餐'
    assert draft['target']['before']['details']['meal_type'] == 'lunch'


def test_repeated_clock_resolves_existing_conflict_with_optional_refusal_and_photo(beijing_afternoon):
    tools = Tools()
    tools.add_photo_file(32)
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY}
    original, _, _ = run_turn(tools, meal_photo('请记录今天中午十二点吃的意面，原图也存入饮食记录。'),
                              [change('diet', {'food': '意面'}, stamp=None)], model=VISION, **options)
    conflict, _, _ = run_turn(tools, '发生时间是今晚八点。', [change('diet', {}, stamp=None)], **options)
    assert conflict[0]['_draft_conflicts'][0]['field'] == 'record_time'
    confirmed, guidance, _ = run_turn(tools, '发生时间确定是今晚八点，晚餐，今天20:00。以这个时间为准。热量不填。',
                                      [change('diet', {'meal': '晚餐'}, stamp=None)], **options)
    draft, = confirmed
    assert draft['supersedes_draft_id'] == conflict[0]['id']
    assert draft['occurred_at'] == beijing_afternoon + 'T20:00:00+08:00'
    assert draft['photo_file_ids'] == original[0]['photo_file_ids'] == [32]
    assert not draft['missing_required'] and not draft['blocked'] and '确认' in guidance


def test_explicit_time_estimation_permission_with_known_past_daypart(beijing_afternoon):
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    initial, _, _ = run_turn(tools, '请记录昨晚吃了面条', [change('diet', {'food': '面条'}, stamp=None)], **options)
    assert initial[0]['record_time_hint'] == 'evening'
    guessed, answer, _ = run_turn(tools, '就按晚上估一下', [], **options)
    assert guessed[0]['occurred_at'] == '2026-01-04T20:00:00+08:00'
    assert '按晚上估为 20:00' in answer and '核对' in answer


def test_second_broad_reply_offers_an_estimate_without_selecting_it(beijing_afternoon):
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    initial, first, _ = run_turn(tools, '请记录昨晚吃了面条', [change('diet', {'food': '面条'}, stamp=None)], **options)
    rough, second, _ = run_turn(tools, '就是晚上', [], **options)
    assert rough[0]['record_time_hint'] == 'evening' and rough[0]['record_date'] == initial[0]['record_date']
    assert rough[0]['occurred_at'] is None and fields_for_draft(rough[0]) == {'record_time'}
    assert '可以说个大概几点，或让我估一个时间供你核对' in second and second != first


def test_explicit_tonight_survives_even_when_other_required_fields_are_blank(beijing_afternoon):
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    initial, _, _ = run_turn(tools, '请记录一条饮食', [change('diet', {}, stamp=None)], **options)
    assert initial[0]['details'] == {}
    timed, reply, _ = run_turn(tools, '今晚八点', [], **options)
    assert timed[0]['occurred_at'] == beijing_afternoon + 'T20:00:00+08:00'
    assert fields_for_draft(timed[0]) == {'food'} and '吃了什么' in reply


def test_long_natural_optional_reply_still_reaches_the_specialist():
    tools = Tools()
    options = {'capability': record.DIALOGUE_TIME_HINTS_CAPABILITY}
    earlier, _, _ = run_turn(tools, f'请记录{DATE}晚上八点吃了面条',
                              [change('diet', {'meal': '晚餐', 'food': '面条'})], **options)
    assert not earlier[0]['missing_required']
    original = '面汤用小火煮，后来加了几滴生抽。这顿饭我只吃了半碗，吃完感觉很清淡。' * 10
    assert 220 < len(original) < 1200
    updated, _, _ = run_turn(tools, original, [change('diet', {'notes': '只吃了半碗，少量生抽'}, stamp=None)], **options)
    assert updated[0]['supersedes_draft_id'] == earlier[0]['id']
    assert updated[0]['details']['notes'] == '只吃了半碗，少量生抽'


def test_unconfirmed_ready_create_requires_choice_before_relative_date_correction():
    tools = Tools()
    options = {'capability': record.DIALOGUE_CAPABILITY}
    today = datetime.now(record.BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    ready, _, _ = run_turn(tools, '请记录今天00:00喝了半升水，备注温水',
                            [change('water', {'amount_ml': 500, 'notes': '温水'}, stamp=today.isoformat())], **options)
    assert not ready[0]['missing_required'] and not ready[0]['missing_optional']
    disputed, reply, _ = run_turn(tools, '不是今天，是昨天', [], **options)
    assert disputed[0]['occurred_at'] == today.isoformat() and fields_for_draft(disputed[0]) == {'record_date'}
    assert disputed[0]['supersedes_draft_id'] == ready[0]['id']
    assert '以哪个为准' in reply and '确认保存' not in reply
    assert tools.runs[2]['record_drafts'][0]['superseded'] is True
    ambiguous, _, _ = run_turn(tools, '好', [], **options)
    assert fields_for_draft(ambiguous[0]) == {'record_date'}
    corrected, reply, _ = run_turn(tools, '就按后一个', [], **options)
    assert corrected[0]['occurred_at'] == (today - record.timedelta(days=1)).isoformat()
    assert corrected[0]['supersedes_draft_id'] == ambiguous[0]['id']
    assert corrected[0]['details'] == ready[0]['details']
    assert '确认' in reply


def test_same_draft_keeps_first_record_context_and_photo_while_numeric_and_day_conflicts_are_resolved(beijing_afternoon):
    tools = Tools()
    tools.turn('无关的早先闲聊。')
    tools.add_photo_file(32)
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY}
    original = '请记录前天晚饭吃的米饭和青菜，热量300千卡，把这张图片原图也保存到饮食记录。'
    draft, _, _ = run_turn(tools, meal_photo(original),
                          [change('diet', {'food': '米饭、青菜', 'meal': '晚餐', 'calories_kcal': 300}, stamp=None)],
                          model=VISION, **options)
    assert draft[0]['record_date'] == '2026-01-03' and draft[0]['photo_file_ids'] == [32]
    timed, _, _ = run_turn(tools, '晚上八点', [], **options)
    assert timed[0]['occurred_at'] == '2026-01-03T20:00:00+08:00'
    disputed, question, calls = run_turn(tools, '其实是昨天，热量四百二十千卡。',
                                        [change('diet', {'calories_kcal': 420}, stamp=None)], **options)
    held = disputed[0]
    assert held['supersedes_draft_id'] == timed[0]['id']
    assert fields_for_draft(held) == {'record_date', 'calories_kcal'}
    assert held['details']['calories_kcal'] == 300 and held['photo_file_ids'] == [32]
    assert all(option in question for option in ('2026-01-03', '2026-01-04', '300', '420', '哪个为准'))
    assert '确认保存' not in question
    assert record.draft_was_replaced(tools.db.conn, tools.principal.user.id, timed[0]['id'])
    reference = json.loads(calls[-1]['messages'][1]['content'][0]['text'].split('\n', 1)[1])
    assert reference['record_start'] == original
    assert reference['recent_dialogue'][0] == {'role': 'user', 'text': original}
    assert all('无关' not in item['text'] for item in reference['recent_dialogue'])
    wire, = record.project_record_photo_drafts(disputed, photo_enabled=True, dialogue_enabled=True,
                                             time_hints_enabled=True, extended_photos=True)
    assert '_draft_conflicts' not in wire and '_record_origin_message_id' not in wire
    resolved, reply, _ = run_turn(tools, '日期还是前天，热量按四百二十千卡。', [], **options)
    assert not fields_for_draft(resolved[0]) and resolved[0]['occurred_at'] == timed[0]['occurred_at']
    assert resolved[0]['details']['calories_kcal'] == 420 and resolved[0]['details']['food'] == draft[0]['details']['food']
    assert resolved[0]['photo_file_ids'] == [32] and '确认' in reply


def test_compatible_additions_and_equivalent_values_do_not_create_conflicts():
    tools = Tools()
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY}
    prior, _, _ = run_turn(tools, f'请记录{DATE}晚上八点吃了米饭和青菜，300千卡，备注少盐。',
                          [change('diet', {'food': '米饭、青菜', 'meal': '晚餐', 'calories_kcal': 300, 'notes': '少盐'})], **options)
    ready, confirmation, _ = run_turn(tools, '这条还是三百千卡，还吃了个鸡蛋，备注再加一句饭后散步。',
                                      [change('diet', {'food': '青菜和米饭、鸡蛋', 'calories_kcal': 300.0,
                                                       'notes': '少盐；饭后散步'}, stamp=None)], **options)
    updated = ready[0]
    assert not fields_for_draft(updated) and '_draft_conflicts' not in updated and '确认' in confirmation
    assert updated['occurred_at'] == prior[0]['occurred_at'] and updated['details']['calories_kcal'] == 300
    assert set(updated['details']['food'].split('、')) == {'米饭', '青菜', '鸡蛋'}
    assert updated['details']['notes'] == '少盐；饭后散步'


@pytest.mark.parametrize('model_edits', [[], [change('diet', {'calories_kcal': 420}, stamp=None)]])
def test_named_calories_still_revise_a_fully_filled_lunch_draft(beijing_afternoon, model_edits):
    tools = Tools()
    tools.add_photo_file(32)
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY}
    original = '帮我记一下今天的午饭，吃了米饭和番茄，三百千卡。这张餐照原图也一起保存。'
    initial, _, _ = run_turn(tools, meal_photo(original),
                             [change('diet', {'food': '米饭、番茄', 'meal_type': 'lunch', 'calories_kcal': 300}, stamp=None)],
                             model=VISION, **options)
    assert initial[0]['photo_file_ids'] == [32]
    ready, _, _ = run_turn(tools, '中午十二点半，吃得清淡，少盐。',
                           [change('diet', {'notes': '吃得清淡，少盐'}, stamp=None)], **options)
    assert not ready[0]['missing_required'] and not ready[0]['missing_optional']
    obsolete_id = ready[0]['id']
    disputed, ask, _ = run_turn(tools, '热量是四百二十千卡。', model_edits, **options)
    assert fields_for_draft(disputed[0]) == {'calories_kcal'}
    assert record.draft_was_replaced(tools.db.conn, tools.principal.user.id, obsolete_id)
    assert all(part in ask for part in ('300', '420', '以哪个为准')) and '确认保存' not in ask
    resolved, answer, _ = run_turn(tools, '按后来那个四百二十千卡。', [], **options)
    assert not resolved[0]['missing_required'] and '确认' in answer
    assert resolved[0]['details'] == {**ready[0]['details'], 'calories_kcal': 420}
    assert resolved[0]['occurred_at'] == beijing_afternoon + 'T12:30:00+08:00'
    assert resolved[0]['photo_file_ids'] == [32] and resolved[0]['supersedes_draft_id'] == disputed[0]['id']


def test_new_old_record_edit_is_not_a_conflict_until_the_draft_itself_changes():
    tools = Tools()
    tools.add_old(66, 'water', {'amount_ml': 250})
    options = {'capability': record.DIALOGUE_PHOTOS_CAPABILITY, 'personal': True}
    first, _, _ = run_turn(tools, '请把2026-01-03那条饮水记录的量改成300毫升。',
                           [change('water', {'amount_ml': 300}, operation='update', stamp=None, selector={'date': DATE})], **options)
    assert not fields_for_draft(first[0]) and first[0]['target']['before']['details']['amount_ml'] == 250
    conflicting, reply, _ = run_turn(tools, '实际是四百二十毫升。',
                                     [change('water', {'amount_ml': 420}, stamp=None)], **options)
    assert fields_for_draft(conflicting[0]) == {'amount_ml'} and conflicting[0]['target']['id'] == 66
    assert '300' in reply and '420' in reply and '250' not in reply


@pytest.mark.parametrize('change_word', ['改为', '改到', '调到'])
def test_old_record_time_is_never_changed_from_a_broad_period(change_word):
    tools = Tools()
    tools.add_old(51, 'activity', {'activity': '散步'})
    edited, reply, _ = run_turn(
        tools, f'把昨天的散步记录时间{change_word}晚上。',
        [change('activity', {}, operation='update', selector={'date': DATE}, stamp='2026-01-04T22:00:00+08:00')],
        personal=True, capability=record.DIALOGUE_CAPABILITY,
    )
    assert not edited and '大概几点' in reply
    assert tools.db.conn.execute('SELECT occurred_at FROM life_records WHERE id=51').fetchone()[0] == STAMP


@pytest.mark.parametrize(('message', 'category', 'fields', 'normalized', 'missing'), [
    ('请记录昨天傍晚六点，我喝了半升水。', 'water', {}, {'amount_ml': 500}, []),
    ('请记录昨天傍晚六点，我喝了一杯水。', 'water', {'amount_ml': 250}, {'notes': '喝了一杯水'}, ['amount_ml']),
    ('请记录昨天傍晚六点，我喝了一杯250毫升的水。', 'water', {'amount_ml': 250}, {'amount_ml': 250}, []),
    ('请记录昨天傍晚六点，我跑步半小时。', 'activity', {'activity': '跑步'},
     {'activity': '跑步', 'duration_minutes': 30}, []),
])
def test_explicit_quantities_can_convert_but_undeclared_cup_sizes_cannot(message, category, fields, normalized, missing):
    drafts, _, _ = run_turn(Tools(), message, [change(category, fields, stamp=None)], capability=record.DIALOGUE_CAPABILITY)
    assert drafts[0]['details'] == normalized
    assert [field['field'] for field in drafts[0]['missing_required']] == missing


@pytest.mark.parametrize(('text', 'missing'), [
    ('请记录昨天八点到九点间喝了250毫升水。', {'record_time'}),
    ('请记录前天还是昨天喝了250毫升水。', {'record_date', 'record_time'}),
    ('请记录昨天上午8:00还是晚上8:00喝了250毫升水。', {'record_time'}),
])
def test_model_cannot_supply_an_arbitrary_timestamp_for_temporal_choices(text, missing):
    drafts, _, _ = run_turn(Tools(), text, [change('water', {'amount_ml': 250})], capability=record.DIALOGUE_CAPABILITY)
    assert drafts[0]['occurred_at'] is None
    assert {field['field'] for field in drafts[0]['missing_required']} == missing


def test_record_dialogue_does_not_turn_an_old_record_locator_into_an_update():
    tools = Tools()
    tools.add_old(51, 'diet', {'food': '豆腐', 'meal_type': 'dinner'})
    edited, _, _ = run_turn(
        tools, f'修改{DATE}晚上20:00那条饮食记录的食物，改成青菜。',
        [change('diet', {'food': '青菜'}, operation='update', stamp=None, selector={'date': DATE, 'time': '20:00'})],
        personal=True, capability=record.DIALOGUE_CAPABILITY,
    )
    assert edited[0]['occurred_at'] == STAMP and edited[0]['target']['id'] == 51


def test_record_dialogue_accepts_required_food_answer_without_button_and_respects_optional_decline():
    tools = Tools()
    _, question, _ = run_turn(tools, '请记录晚餐', [change('diet', {'meal': '晚餐'})],
                              capability=record.DIALOGUE_CAPABILITY)
    assert '吃了什么' in question
    ready, answer, _ = run_turn(tools, '米饭和煎蛋', [change('diet', {'food': '米饭和煎蛋'}, stamp=None)],
                                capability=record.DIALOGUE_CAPABILITY)
    assert ready[0]['missing_required'] == [] and '热量、其他说明' in answer
    refused, acknowledgement, calls = run_turn(tools, '不用了', [], expect_record=False,
                                             capability=record.DIALOGUE_CAPABILITY)
    assert refused == calls == [] and '可以留空' in acknowledgement
    assert not tools.runs[4]['record_drafts'][0]['blocked']


def test_record_dialogue_excludes_saved_or_locally_decided_draft():
    from uuid import uuid5
    from server.mobile_sync149 import NAMESPACE
    tools = Tools()
    ready, _, _ = run_turn(tools, '请记录晚餐，吃了米饭', [change('diet', {'food': '米饭', 'meal': '晚餐'})],
                          capability=record.DIALOGUE_CAPABILITY)
    mid = tools.turn('420千卡')
    meta = {'conversation_id': 21, 'assistant_message_id': mid, 'decided_record_draft_ids': [ready[0]['id']]}
    assert record.previous_turn(tools, tools.principal, meta, dialogue_enabled=True) == {}
    tools.db.conn.execute(
        'INSERT INTO rpc_requests VALUES(?,?,?,?,?)',
        (7, str(uuid5(NAMESPACE, "7:" + ready[0]['id'])), 'fixture', '{"mobile149":{"status":"applied"}}', STAMP),
    )
    del meta['decided_record_draft_ids']
    assert record.previous_turn(tools, tools.principal, meta, dialogue_enabled=True) == {}


def test_record_dialogue_stores_only_visible_form_extraction_and_keeps_existing_metadata():
    tools = Tools()
    drafted, _, _ = run_turn(
        tools, '请记录午饭吃了米饭',
        [change('diet', {'food': '米饭', 'meal_type': 'lunch', 'calories_kcal': 320,
                         'amount_g': 150, 'sugar_g': 8, 'estimated': True})],
        capability=record.DIALOGUE_CAPABILITY,
    )
    assert drafted[0]['details'] == {'food': '米饭', 'meal_type': 'lunch', 'calories_kcal': 320}
    tools.add_old(51, 'diet', {'food': '豆腐', 'meal_type': 'dinner', 'amount_g': 130, 'sugar_g': 3})
    edited, _, _ = run_turn(
        tools, '修改2026年1月3日的晚餐记录，改成豆腐和青菜',
        [change('diet', {'food': '豆腐和青菜', 'amount_g': 170, 'sugar_g': 99},
                operation='update', stamp=None, selector={'date': DATE})],
        personal=True, capability=record.DIALOGUE_CAPABILITY,
    )
    assert edited[0]['details'] == {'food': '豆腐和青菜', 'meal_type': 'dinner', 'amount_g': 130, 'sugar_g': 3}


def test_record_dialogue_separates_fresh_records_from_optional_answers():
    tools = Tools()
    options = {'capability': record.DIALOGUE_CAPABILITY}
    meal, _, _ = run_turn(tools, '请记录今天12点吃了米饭', [change('diet', {'food': '米饭'})], **options)
    exercise, _, _ = run_turn(
        tools, '请帮我记录今天20:00跑步30分钟，备注很累',
        [change('activity', {'activity': '跑步', 'duration_minutes': 30, 'notes': '很累'})], **options,
    )
    assert exercise[0]['category'] == 'activity' and 'supersedes_draft_id' not in exercise[0]
    assert not tools.runs[2]['record_drafts'][0]['blocked']
    second, _, _ = run_turn(tools, '请记录今天12点吃了面条', [change('diet', {'food': '面条'})], **options)
    third, _, _ = run_turn(tools, '请记录今天13点午餐吃了饺子，其他说明是清淡',
                            [change('diet', {'food': '饺子', 'meal': '午餐', 'notes': '清淡'})], **options)
    assert third[0]['details']['food'] == '饺子' and 'supersedes_draft_id' not in third[0]
    assert not record.draft_was_replaced(tools.db.conn, 7, meal[0]['id'])
    assert not record.draft_was_replaced(tools.db.conn, 7, second[0]['id'])
    updated, _, _ = run_turn(tools, '请把热量补上395千卡',
                              [change('diet', {'calories_kcal': 395}, stamp=None)], **options)
    assert updated[0]['supersedes_draft_id'] == third[0]['id']


@pytest.mark.parametrize(('category', 'missing', 'reply', 'values', 'optional', 'extra', 'patch'), [
    ('water', 'amount_ml', '250毫升', {'amount_ml': 250}, '其他说明', '其他说明写温水', {'notes': '温水'}),
    ('activity', 'activity', '散步', {'activity': '散步'}, '运动时长', '15分钟', {'duration_minutes': 15}),
    ('sleep', 'duration_hours', '7小时', {'duration_hours': 7}, '其他说明', '其他说明写中途醒了一次', {'notes': '中途醒了一次'}),
    ('environment', 'environment', '室温22度', {'temperature_c': 22}, '湿度', '湿度45%', {'humidity_percent': 45}),
    ('medical', 'name', '常规复查', {'name': '常规复查'}, '地点或机构', '地点是社区医院', {'location': '社区医院'}),
    ('metric', 'diastolic', '舒张压79', {'diastolic': 79}, '其他说明', '其他说明写坐着测的', {'notes': '坐着测的'}),
])
def test_other_record_forms_offer_conversational_required_and_optional_followups(category, missing, reply,
                                                                         values, optional, extra, patch):
    tools = Tools()
    options = {'capability': record.DIALOGUE_CAPABILITY}
    start = {'kind': 'blood_pressure', 'value': 117} if category == 'metric' else {}
    draft, ask, _ = run_turn(tools, f'请记录{record.CATEGORIES[category]}，晚上八点',
                            [change(category, start)], **options)
    assert [field['field'] for field in draft[0]['missing_required']] == [missing]
    assert '方便直接在这里告诉我吗' in ask and draft[0]['missing_required'][0]['label'] in ask
    ready, confirm, _ = run_turn(tools, reply, [change(category, values, stamp=None)], **options)
    assert ready[0]['missing_required'] == [] and ready[0]['supersedes_draft_id'] == draft[0]['id']
    assert optional in confirm and '都可以留空' in confirm
    updated, confirm, _ = run_turn(tools, extra, [change(category, patch, stamp=None)], **options)
    assert updated[0]['supersedes_draft_id'] == ready[0]['id']
    assert all(updated[0]['details'][key] == value for key, value in (values | patch).items())
    assert '请核对更新后的草稿' in confirm and '留空' not in confirm


def test_required_review_uses_the_referenced_same_chat_fact():
    tools = Tools()
    tools.turn('我那次喝的是三百六十毫升白水。')
    drafts, answer, calls = run_turn(tools, '请帮我把刚才说的那次饮水记到饮水记录里，发生时间是2026-01-03早上七点。',
        [change('water', {}, stamp=None)], capability=record.DIALOGUE_PHOTOS_CAPABILITY,
        reviewed=[{'field': 'amount_ml', 'value': 360, 'source': 1, 'quote': '三百六十毫升'}])
    draft, = drafts
    assert len(calls) == 3 and draft['details'] == {'amount_ml': 360}
    assert draft['occurred_at'].startswith('2026-01-03T07:00') and not draft['missing_required']
    assert '继续补充' not in answer and '请核对' in answer


@pytest.mark.parametrize('prior, latest', [
    ('我那次喝的是三百六十毫升白水。', '请帮我记录饮水，发生时间是2026-01-03早上七点。'),
    ('我午餐吃了番茄，做饭用的是三百六十毫升白水。', '请帮我记录饮水，发生时间是2026-01-03早上七点。'),
    ('昨天晚上八点我吃了番茄。', '请帮我把刚才说的那次记录为饮水。'),
    ('我在问什么饮水量适合我？', '请帮我把刚才说的那次记录为饮水。'),
])
def test_required_review_skips_absent_or_unrelated_evidence(prior, latest):
    tools = Tools()
    tools.turn(prior)
    tools.db.conn.execute('UPDATE messages SET content=? WHERE id=2', ('我认为可能喝了八百毫升。',))
    drafts, answer, calls = run_turn(tools, latest, [change('water', {}, stamp=None)],
        capability=record.DIALOGUE_PHOTOS_CAPABILITY)
    assert len(calls) == 2 and '必填' in answer
    assert any(field['field'] == 'amount_ml' for field in drafts[0]['missing_required'])


@pytest.mark.parametrize('reviewed', [
    [{'field': 'amount_ml', 'value': 360, 'source': 1, 'quote': '三百六十毫升'}],
    [{'field': 'amount_ml', 'value': 420, 'source': 1, 'quote': '四百二十毫升'},
     {'field': 'amount_ml', 'value': 360, 'source': 1, 'quote': '三百六十毫升'}],
])
def test_required_review_requests_choice_between_explicit_conflicting_amounts(reviewed):
    tools = Tools()
    latest = '请帮我记录饮水，2026-01-03早上七点喝了三百六十毫升。另一处记成了四百二十毫升。'
    drafts, answer, calls = run_turn(tools, latest, [change('water', {}, stamp=None)],
        capability=record.DIALOGUE_PHOTOS_CAPABILITY, reviewed=reviewed)
    draft, = drafts
    assert len(calls) == 3 and '360' in answer and '420' in answer and '哪个' in answer
    assert draft['_draft_conflicts'] == [{'field': 'amount_ml', 'before': 360, 'after': 420}]
    assert draft['missing_required'][0]['field'] == 'amount_ml' and 'amount_ml' not in draft['details']
    after, _, next_calls = run_turn(tools, '按后来那个四百二十毫升。',
        [change('water', {}, stamp=None)], capability=record.DIALOGUE_PHOTOS_CAPABILITY)
    assert len(next_calls) == 2 and after[0]['supersedes_draft_id'] == draft['id']
    assert after[0]['details']['amount_ml'] == 420 and not after[0]['missing_required']


@pytest.mark.parametrize('reviewed', [
    {'unexpected': True},
    [{'field': 'amount_ml', 'value': 800, 'source': 1, 'quote': '三百六十毫升'}],
    [{'field': 'amount_ml', 'value': 360, 'source': 1, 'quote': '白开水三百六十毫升'}],
])
def test_required_review_ignores_malformed_or_ungrounded_results(reviewed):
    drafts, answer, calls = run_turn(Tools(), '请帮我记录2026-01-03早上七点喝了三百六十毫升水。',
        [change('water', {}, stamp=None)], capability=record.DIALOGUE_PHOTOS_CAPABILITY, reviewed=reviewed)
    assert len(calls) == 3 and '饮水量' in answer and drafts[0]['details'] == {}


def test_required_review_preserves_original_photo_and_known_optional_fact():
    tools = Tools()
    tools.add_photo_file(32)
    latest = meal_photo('请帮我记录2026-01-03中午十二点半的午饭，吃了番茄，少盐。原图也保存。')
    drafts, answer, calls = run_turn(tools, latest, [change('diet', {'meal_type': 'lunch', 'notes': '少盐'}, stamp=None)],
        model=VISION, capability=record.DIALOGUE_PHOTOS_CAPABILITY,
        reviewed=[{'field': 'food', 'value': '番茄', 'source': 1, 'quote': '吃了番茄'},
                  {'field': 'notes', 'value': '高盐', 'source': 1, 'quote': '少盐'}])
    draft, = drafts
    assert len(calls) == 3 and draft['photo_file_ids'] == [32] and not draft['missing_required']
    assert draft['details']['food'] == '番茄' and draft['details']['notes'] == '少盐' and '原图' in answer


def test_required_review_relative_date_uses_original_beijing_day():
    from server.record_required_review11863 import required_review_context, reviewed_facts
    draft = {'operation': 'create', 'category': 'water', 'blocked': False, 'details': {'amount_ml': 360},
             'record_time': '07:00', 'missing_required': [{'field': 'record_date'}]}
    context = {
        'record_start': '我今天早上七点喝了水。', 'record_start_at': '2026-01-03T15:57:00Z',
        'recent_dialogue': [{'role': 'assistant', 'text': '可能是明天？'}],
    }
    packet = required_review_context([draft], '我说的是三百六十毫升。', context,
                                     reference_at='2026-01-03T16:03:00Z')
    assert packet['candidates'][0]['reference_at'].startswith('2026-01-03T23:57')
    wrong_day = {'reviewed': [{'field': 'record_date', 'value': '2026-01-04', 'source': 1, 'quote': '今天'}]}
    right_day = {'reviewed': [dict(wrong_day['reviewed'][0], value='2026-01-03')]}
    assert reviewed_facts(wrong_day, packet, draft)['facts'] == {}
    assert reviewed_facts(right_day, packet, draft)['facts'] == {'record_date': '2026-01-03'}


def test_required_review_does_not_take_values_from_another_metric():
    from server.record_required_review11863 import required_review_context, reviewed_facts
    draft = {'operation': 'create', 'category': 'metric', 'blocked': False, 'details': {'kind': 'glucose'},
             'missing_required': [{'field': 'value'}]}
    unrelated = '我刚才量的身高是一百七十厘米。'
    context = {'recent_dialogue': [{'role': 'user', 'text': unrelated}]}
    assert required_review_context([draft], '帮我把刚才说的那次血糖记下来，单位是毫克每分升。', context) is None
    source = '我量的身高是一百七十厘米，血糖是五点五毫摩尔每升。'
    context['recent_dialogue'][0]['text'] = source
    packet = required_review_context([draft], '帮我把刚才说的那次血糖记下来。', context)
    mixed = {'reviewed': [{'field': 'value', 'value': 170, 'source': 1, 'quote': '身高是一百七十厘米'},
                         {'field': 'value', 'value': 5.5, 'source': 1, 'quote': '血糖是五点五毫摩尔每升'}]}
    assert reviewed_facts(mixed, packet, draft)['facts'] == {'value': 5.5}


@pytest.mark.parametrize(('extracted', 'raw_extracted'), [
    ([], None),
    ([change('water', {'amount_ml': 500}, stamp=None)], None),
    ([{'unexpected_field': 13}], None),
    (None, '{"changes":['),
])
def test_exact_clock_conflict_preserves_photo_even_if_primary_extraction_misses_or_is_malformed(extracted, raw_extracted):
    tools = Tools()
    tools.add_photo_file(32)
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    first = meal_photo('帮我记一下昨天中午十二点半的午饭，吃了米饭和番茄，三百千卡。原图也随这条记录保存。')
    original, _, _ = run_turn(tools, first,
        [change('diet', {'meal_type': '午餐', 'food': '米饭、番茄', 'calories_kcal': 300, 'notes': '原图随记录'}, stamp=None)],
        model=VISION, capability=cap)
    assert not original[0]['missing_required'] and not original[0]['missing_optional']
    conflicted, prompt, calls = run_turn(tools, '发生时间是下午一点十五分。', extracted,
        capability=cap, raw_extracted=raw_extracted)
    draft, = conflicted
    assert len(calls) == 2 and '12:30' in prompt and '13:15' in prompt and '以哪个为准' in prompt
    assert draft['supersedes_draft_id'] == original[0]['id'] and draft['photo_file_ids'] == [32]
    assert draft['missing_required'] == [{'field': 'record_time', 'label': '发生时间'}]
    assert draft['details'] == original[0]['details'] and draft['_draft_conflicts'][0]['after'] == '13:15'
    final, _, choice_calls = run_turn(tools, '按后面那个为准。', [], capability=cap, raw_extracted=raw_extracted)
    assert len(choice_calls) == 2 and not final[0]['missing_required']
    assert final[0]['occurred_at'].endswith('T13:15:00+08:00') and final[0]['photo_file_ids'] == [32]
    assert final[0]['supersedes_draft_id'] == draft['id'] and final[0]['details'] == draft['details']


def test_exact_clock_path_does_not_use_unrelated_chat_as_a_record_reply():
    tools = Tools()
    cap = record.DIALOGUE_PHOTOS_CAPABILITY
    original, _, _ = run_turn(tools, '帮我记录昨天中午十二点半喝了三百毫升水。',
        [change('water', {'amount_ml': 300}, stamp=None)], capability=cap)
    assert not original[0]['missing_required']
    same_clock = '发生时间是中午十二点半。'
    unrelated = '发生时间是下午一点十五分，你说演唱会怎么安排？'
    assert not record.explicit_clock_draft_reply(same_clock, {'drafts': original})
    assert not record.explicit_clock_draft_reply(unrelated, {'drafts': original})
    none, _, calls = run_turn(tools, unrelated, [], capability=cap, expect_record=False)
    assert none == [] and len(calls) == 1

def test_explicit_supplement_with_label_preserves_old_notes_even_if_model_asks_to_update():
    tools = Tools()
    tools.add_photo_file(32)
    tools.add_photo_file(33, payload=different_photo())
    tools.add_old(100, 'diet', {'food': '番茄意面', 'notes': 'QA餐照', 'photo_file_ids': [32]})
    latest = [{'type': 'text', 'text': '帮我补充那条饮食记录。把标签原图追加到同一条旧记录，保留原来的餐照，补上热量。'},
              {'type': 'image_url', 'image_url': {'url': different_photo()}}]
    extracted = change('diet', {'calories_kcal': 420, 'notes': 'QA餐照；追加营养标签原图，保留已有餐照'},
                       operation='update', stamp=None, selector={'date': DATE})
    drafts, _, _ = run_turn(tools, latest, [extracted], model=VISION, personal=True,
                             capability=record.DIALOGUE_PHOTOS_CAPABILITY)
    draft, = drafts
    assert draft['operation'] == 'supplement'
    assert draft['details']['notes'] == 'QA餐照' and draft['details']['calories_kcal'] == 420
    assert draft['photo_file_ids'] == [32, 33]


def test_health_note_explicitly_written_by_user_can_mention_a_photo():
    data = change('diet', {'notes': '请保留原图作为下次复诊参考'}, operation='update')
    assert record._respect_photo_request(data, '请修改旧记录，备注写成请保留原图作为下次复诊参考') == data


def test_declined_optional_stays_visible_as_empty_field_without_repeat_invitation():
    options = dict(dialogue_enabled=True, allow_same_day_future=True)
    row = change('diet', {'food':'米饭','meal':'晚餐','notes':'QA'}, stamp=STAMP)
    draft, answer = record.compose(row, latest='热量先不填。', **options)
    assert '热量' not in answer
    projected, = record.project_record_photo_drafts([draft], photo_enabled=True, dialogue_enabled=True)
    assert not projected['missing_optional'] and '_declined_optional' not in projected
    carry, answer = record.compose(change('diet', {}, stamp=None), previous=draft, latest='其他按之前的草稿。', **options)
    assert '热量' not in answer and carry['_declined_optional'] == ['calories_kcal']
    restored, _ = record.compose(change('diet', {'calories_kcal':420}, stamp=None), previous=carry, latest='现在知道了，热量420千卡。', **options)
    assert restored['details']['calories_kcal'] == 420 and '_declined_optional' not in restored
