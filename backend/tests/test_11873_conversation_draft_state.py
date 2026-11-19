"""The list, immutable receipts, the main model prompt and streamed dialogue agree."""
import json
from types import SimpleNamespace

import pytest

from server.ai139_tools import ToolError
from server.ai_persistence_truth import PersistenceTextGuard, conversation_draft_state, declines_draft_nudges
from server.record_tasks11864 import RecordTasks
from test_11871_record_truth import put_receipt
from test_record_tasks11864 import TaskTools, _run, _seed, change


SHARE = '今天做完饭听了会儿音乐，心情很好，就是想和你分享一下，不用记录。'
INTRO = '林先生，听您这么说，我也觉得挺高兴。做完饭再听会儿音乐，松一松手、缓一缓神，这种小小的满足感很珍贵。'
NO_WRITE = '您说不用记录，那就纯粹当作聊天，我不生成任何草稿，也不会保存。'
NUDGE = '对了，今天上午九点那杯两百毫升温水，草稿还在等您核对。'
DEFER = '如果您想等会儿再处理，也完全没问题。'


def read(tools, *, personal=True):
    return conversation_draft_state(tools, tools.principal, {'conversation_id':21,'personal_context':personal})


def stream(value, input_text, total, *, current=False):
    guard = PersistenceTextGuard(lambda:current, input_text)
    guard.open_drafts = total
    guard.no_record_nudge = declines_draft_nudges(input_text)
    return (''.join(guard.feed(char) for char in value) + guard.finish()).strip()


def test_real_observed_reply_streamed_with_conversation_633_state():
    reply = '\n\n'.join((INTRO, NO_WRITE, NUDGE + DEFER))
    assert stream(reply, SHARE, 0) == '\n\n'.join((INTRO, NO_WRITE))
    assert stream(reply, SHARE, 1) == '\n\n'.join((INTRO, NO_WRITE))
    assert stream(NUDGE, '接着说吧。', 0) == ''
    assert stream(NUDGE, '上一张草稿什么状态？', 1) == NUDGE
    assert stream(NUDGE, '帮我记下这杯温水。', 0, current=True) == NUDGE
    assert stream('听音乐很惬意，草稿还在等您核对。', SHARE, 0) == '听音乐很惬意。'
    assert '草稿' not in stream('嗯' * 379 + '饮水草稿仍然待核对' + '好' * 150 + '。', SHARE, 0)


@pytest.mark.parametrize('line', [
    '你当时的草稿还在等您核对。', '草稿已经取消，不用核对。', '草稿没有待核对的内容。',
    '原话是“草稿还在等您核对”。', '如果草稿还在等您核对，可以在待办找到。',
    '你的论文草稿还在等您核对。', '我可以和你聊聊今天做的饭。'])
def test_history_quote_conditions_and_unrelated_conversation_remain_intact(line):
    assert stream(line, SHARE, 0) == line


def test_explicit_opt_out_without_reminders_preserves_status_question_and_field_refusal():
    assert declines_draft_nudges(SHARE)
    assert stream('要不要核对上一份草稿？', SHARE, 1) == ''
    for value in ('新情况不用记录，之前还有几份草稿？', '不用记录，原草稿怎么样了吗？',
                  '照片不用记录，继续补充原草稿。', '备注不用记录。', '请解释“不用记录”。'):
        assert not declines_draft_nudges(value)


def test_scope_lifecycle_no_copy_of_notes_and_no_status_from_untrusted_inputs():
    tools = TaskTools()
    repo = RecordTasks(tools)
    first = _seed(tools, 1, amount_ml=333, notes='测试备注不可进入系统消息')
    second = _seed(tools, 2, stamp='2026-01-03T09:00:00+08:00', amount_ml=200)
    assert read(tools)['待处理总数'] == 2
    put_receipt(tools, first)
    task = next(row for row in repo.list(tools.principal,21)['tasks'] if row['draft_id']==second['id'])
    receipt = repo.cancel_draft(tools.principal, task['assistant_message_id'], second['id'])
    assert receipt == {'id':second['id'],'status':'cancelled'}
    assert repo.list(tools.principal,21)['total'] == 0
    state = read(tools)
    assert state == {'待处理总数':0,'最近状态（新到旧）':[
        {'状态':'已取消，未保存','类别':'饮水','时间':'09:00'}, {'状态':'已确认保存','类别':'饮水','时间':'08:00'}]}
    prompt = []
    _, _, calls = _run(tools, SHARE, [], expect_record=False, personal=False)
    prompt.extend(m['content'] for m in calls[0]['messages'] if m['role']=='system' and '待处理总数' in m['content'])
    assert len(prompt)==1 and '已取消，未保存' in prompt[0] and '已确认保存' in prompt[0]
    assert '09:00' not in prompt[0] and '测试备注' not in prompt[0] and '333' not in prompt[0]
    assert all(set(row)=={'状态'} for row in read(tools,personal=False)['最近状态（新到旧）'])
    assert json.loads(json.dumps(state,ensure_ascii=False))==state
    outsider = SimpleNamespace(user=SimpleNamespace(id=8,role_code='member',account_status='active'))
    with pytest.raises(ToolError,match='record_task_unavailable'):
        conversation_draft_state(tools,outsider,{'conversation_id':21})
    assert conversation_draft_state(tools,outsider,{'conversation_id':25}) == {'待处理总数':0,'最近状态（新到旧）':[]}


def test_empty_initial_list_does_not_override_fresh_draft_or_its_confirmation():
    tools=TaskTools()
    assert read(tools)['待处理总数']==0
    drafts,reply,calls=_run(tools,'请记录今天早上九点喝了200毫升水。',[change('water',{'amount_ml':200})])
    assert len(drafts)==1 and '请核对' in reply
    assert not any('本轮开始时，本账号当前对话' in str(m.get('content','')) for call in calls for m in call['messages'])


def test_failed_receipt_and_replaced_revision_are_never_reported_saved_or_cancelled():
    from server.record_assistant_try12 import draft_replacement_marker
    tools = TaskTools()
    draft = _seed(tools,1,amount_ml=200)
    put_receipt(tools,draft,status='conflict')
    assert read(tools)['最近状态（新到旧）'][0]['状态']=='待核对'
    actor=tools.principal.user.id
    tools.db.conn.execute('INSERT INTO rpc_requests VALUES(?,?,?,?,?)',
        (actor,draft_replacement_marker(actor,draft['id']),'replacement',json.dumps({'replaced_draft':draft['id']}),None))
    assert read(tools) == {'待处理总数':0,'最近状态（新到旧）':[]}
