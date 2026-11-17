"""Synthetic dispatch and exact pending-task lookup across many persisted drafts."""

from datetime import datetime
import json
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from test_record_assistant_try12 import PHOTO, Tools, change, record, run_turn
from server.ai139_tools import AI139Tools, ToolError
from server.mobile_sync149 import NAMESPACE
from server.record_tasks11864 import CAPABILITY, RecordTasks, criteria, parse_reference


class TaskTools(Tools):
    bind_message = AI139Tools.bind_message
    set_record_drafts = AI139Tools.set_record_drafts

    def __init__(self):
        super().__init__()
        self.db.transaction = self.db.connect


def _run(tools, text, fields, **kwargs):
    return run_turn(tools, text, fields, capability=record.DIALOGUE_PHOTOS_CAPABILITY,
                    tasks_enabled=True, **kwargs)


def _seed(tools, index, *, stamp='2026-01-03T08:00:00+08:00', category='water', **fields):
    ident = tools.turn(f'准备草稿第{index}次')
    draft = dict(id=str(uuid4()), category=category, occurred_at=stamp, details=fields,
                 operation='create', content='独立草稿', blocked=False,
                 missing_required=[], missing_optional=[])
    tools.runs[ident] = {'status': 'complete', 'record_drafts': [draft]}
    RecordTasks(tools).apply(tools.db.conn, {'actor': 7, 'conversation_id': 21, 'assistant_message_id': ident},
                            [draft], None)
    return draft


def test_pending_tasks_pagination_exact_filters_and_permission_after_many_updates():
    tools = TaskTools()
    repo = RecordTasks(tools)
    old = _seed(tools, 0, stamp='2026-01-03T23:00:00+08:00', notes='绿保温杯', amount_ml=333)
    _seed(tools, 1, stamp='2026-01-03T02:00:00+08:00', notes='夜里温水', amount_ml=200)
    for index in range(2, 152):
        _seed(tools, index, notes=f'普通玻璃杯{index}', amount_ml=100 + index)

    first = repo.list(tools.principal, 21, limit=13)
    batches = list(first['tasks'])
    cursor = first['next_cursor']
    while cursor:
        page = repo.list(tools.principal, 21, cursor=cursor, limit=13)
        batches += page['tasks']
        assert cursor != page['next_cursor']
        cursor = page['next_cursor']
    assert first['total'] == len({view['id'] for view in batches}) == len(batches) == 152
    assert old['id'] not in {view['draft_id'] for view in first['tasks']}
    exact = repo.list(tools.principal, 21, query='2026-01-03 23:00 饮水 绿保温杯')
    assert exact['total'] == 1 and exact['tasks'][0]['draft_id'] == old['id']
    assert repo.list(tools.principal, 21, query='2026-01-03 夜里 饮水')['total'] == 2
    start = parse_reference('继续2026-01-03 23:00那条饮水补三百五十毫升')
    assert start.reply == '补三百五十毫升'
    task, prior, tied = repo.resolve(tools.principal, {'conversation_id': 21}, start)
    assert task['draft_id'] == old['id'] and prior['drafts'][0] == old and not tied
    focused = repo.focus(tools.principal, 21, task['id'])
    assert focused['draft']['id'] == old['id']
    assert repo.list(tools.principal, 21)['active_task_id'] == task['id']
    stranger = SimpleNamespace(user=SimpleNamespace(id=8, role_code='member', account_status='active'))
    with pytest.raises(ToolError, match='record_task_unavailable'):
        repo.focus(stranger, 21, task['id'])
    with pytest.raises(ToolError, match='record_task_unavailable'):
        repo.focus(stranger, 25, task['id'])
    assert repo.list(stranger, 25)['total'] == 0

    # A mobile save receipt closes only the selected revision, including after
    # restoring the server process and with newer open tasks in the same chat.
    mobile_id = str(uuid5(NAMESPACE, f'7:{old["id"]}'))
    tools.db.conn.execute('INSERT INTO rpc_requests VALUES(?,?,?,?,?)', (
        7, mobile_id, 'old-proof', json.dumps({'mobile149': {'status': 'applied'}}), '2026-01-04T04:00:00.000000Z'))
    assert RecordTasks(tools).list(tools.principal, 21, query='绿保温杯')['total'] == 0
    with pytest.raises(ToolError, match='record_task_unavailable'):
        repo.focus(tools.principal, 21, task['id'])


def test_chat_switch_updates_old_draft_after_new_task_and_stale_selection_fails():
    tools = TaskTools()
    first, _, _ = _run(tools, '请记录2026-01-03早上八点喝水。',
                       [change('water', {}, stamp=None)])
    assert {'amount_ml'}.issubset({item['field'] for item in first[0]['missing_required']})
    repo = RecordTasks(tools)
    old = repo.list(tools.principal, 21, query='饮水')['tasks'][0]
    old_revision = repo._get(tools.db.conn, 7, 21, old['id'])
    second, _, _ = _run(tools, '先另外新建一条饮食记录，2026-01-03中午十二点半吃米饭。',
                        [change('diet', {'food': '米饭', 'meal_type': '午餐'}, stamp=None)])
    assert first[0]['id'] != second[0]['id']
    assert repo.list(tools.principal, 21)['total'] == 2
    revised, _, calls = _run(tools, '回到2026-01-03早上八点的那条饮水草稿，帮我补充饮水量三百五十毫升。',
                              [change('water', {'amount_ml': 350}, stamp=None)])
    assert revised[0]['details']['amount_ml'] == 350
    assert revised[0]['supersedes_draft_id'] == first[0]['id']
    assert revised[0]['occurred_at'].startswith('2026-01-03T08:00')
    assert record.draft_was_replaced(tools.db.conn, 7, first[0]['id'])
    assert not record.draft_was_replaced(tools.db.conn, 7, second[0]['id'])
    assert repo.list(tools.principal, 21, '饮水')['tasks'][0]['id'] == old['id']
    assert repo.list(tools.principal, 21, '午餐')['tasks'][0]['draft_id'] == second[0]['id']
    context_call = next(req for req in calls if not req['stream'])
    context = json.dumps(context_call, ensure_ascii=False)
    assert '吃米饭' not in context
    with pytest.raises(ToolError, match='record_draft_changed'):
        repo.finish_focus(tools.db.conn, {'actor': 7, 'conversation_id': 21}, old_revision)
    _, answer, calls = _run(tools, '回到2026-01-03那条饮水草稿。', [], expect_record=False)
    assert '已切回' in answer and not calls
    _, answer, calls = _run(tools, '刚才那条。', [], expect_record=False)
    assert '找到多份' in answer and '饮水' in answer and '午餐' in answer and not calls


def test_parse_exact_date_time_and_reject_invalid_clock_or_day():
    ref = datetime.fromisoformat('2026-11-16T14:00:00+08:00')
    pick = parse_reference('回到昨天早上八点的那条饮水草稿，350毫升', reference=ref)
    assert (pick.lookup.day, pick.lookup.clock, pick.lookup.category, pick.reply) == (
        '2026-11-15', '08:00', 'water', '350毫升')
    assert criteria('饮水 8:00').clock == '08:00'
    assert parse_reference('给昨天晚饭的草稿补备注少盐', reference=ref).reply == '补备注少盐'
    with pytest.raises(ToolError, match='invalid_record_task_date'):
        criteria('2026-02-31 08:00 饮水')
    with pytest.raises(ToolError, match='invalid_record_task_time'):
        criteria('2026-02-03 25:00 饮水')


def test_switch_preserves_photo_and_an_unresolved_conflict_without_polluting_other_tasks():
    tools = TaskTools()
    tools.add_photo_file(45)
    photo_request = [{'type': 'text', 'text': '请记录2026-01-03中午十二点半吃米饭，这张照片也加上。'},
                     {'type': 'image_url', 'image_url': {'url': PHOTO}}]
    initial, _, _ = _run(tools, photo_request,
                          [change('diet', {'food': '米饭', 'meal_type': '午餐'}, stamp=None)])
    assert initial[0]['photo_file_ids'] == [45]
    repo = RecordTasks(tools)
    unit = repo.list(tools.principal, 21, query='午餐')['tasks'][0]
    conflict, _, _ = _run(tools, '发生时间是下午一点十五分。', [])
    assert conflict[0]['_draft_conflicts'] and conflict[0]['photo_file_ids'] == [45]
    _run(tools, '另开一条饮水记录，2026-01-03晚上六点喝了两百毫升水。',
         [change('water', {'amount_ml': 200}, stamp=None)])
    matched = repo.list(tools.principal, 21, query='午餐')['tasks'][0]
    assert matched['status'] == 'conflict' and matched['id'] == unit['id']
    view = repo.focus(tools.principal, 21, unit['id'])
    assert view['draft']['id'] == conflict[0]['id'] and view['draft']['photo_file_ids'] == [45]
    revised, _, _ = _run(tools, '按后一个。', [], task_id=unit['id'])
    assert revised[0]['occurred_at'].startswith('2026-01-03T13:15')
    assert revised[0]['photo_file_ids'] == [45] and not revised[0].get('_draft_conflicts')
    assert repo.list(tools.principal, 21, query='午餐')['tasks'][0]['id'] == unit['id']
    assert repo.list(tools.principal, 21, query='饮水')['tasks'][0]['status'] == 'ready'
