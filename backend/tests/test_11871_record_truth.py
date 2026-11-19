"""11871: chat wording, consent and successful record commits have separate sources."""
import json
import random
from uuid import uuid4, uuid5

import pytest
from server.ai_persistence_truth import PersistenceTextGuard, immediate_status, status_query, last_record_status
from server.ai139_stream import AI139Runner
from server.mobile_sync149 import NAMESPACE
from test_record_assistant_try12 import PHOTO, Tools, run_turn, change, record
from test_record_tasks11864 import TaskTools, _run as run_task, _seed
from server.record_tasks11864 import RecordTasks
from server.ai139_tools import ToolError


def render(text, pending=False):
    guard = PersistenceTextGuard(lambda: pending)
    return ''.join(guard.feed(letter) for letter in text) + guard.finish()


@pytest.mark.parametrize('text', [
    '今天下午三点，你喝了300毫升温水，这个记录先记下了。',
    '上午10点喝了450毫升温水，我记下了。',
    '我已经帮你记录下来了。', '这条饮水记录已保存。',
    '已经把早餐修改成七点。','我帮你补充好了。','补充成功！', '好的，已更新。','我为你记好了。',
    '好的，我把这件事记下来了。','已为你录入一笔饮水。','这条记录已经补齐。'
])
def test_unverified_current_completion_never_published_even_split_across_tokens(text):
    assert '这次尚未保存记录' in render(text)
    assert '草稿待核对，确认后才会保存' in render(text, True)
    if '300毫升' in text:
        assert '你喝了300毫升温水' in render(text)


@pytest.mark.parametrize('text', [
    '我可以帮你记下来，你愿意吗？', '要把刚才的饮水整理成草稿吗？',
    '你昨天自己已经保存了早餐。', '你说“我已保存”。',
    '提示语示例：已保存。', '应用会显示“已保存。已修改成功”。',
    '已记录的数据可以在记录列表查看。', '已经记录过的早餐可以打开。',
    '如果保存成功，页面会显示结果。','保存成功后，可以在列表查看。',
    '你之前确认的记录里有一条早餐。', '如果你点击确认，草稿就会保存。',
    '这份草稿里的时间已经改好，请核对。','我已经知道该如何记录。',
    '他昨天说：“我帮你记好了。”', '关于引用：\n> 我已经记录了你的早餐。',
    '刚才喝的是温水，收到。','我还没有保存。','这条记录没有保存。',
])
def test_quoted_historical_conditional_draft_or_unrelated_fact_unchanged(text):
    assert render(text) == text


def test_editorial_changes_are_not_formal_health_record_writes():
    prompt='帮我改通顺这句话：“我把今天的水喝得很及时。”'
    for answer in ('我已经把这句话修改好了：今天我及时喝了水。', '修改完成：今天我及时喝了水。'):
        guard=PersistenceTextGuard(lambda:False,prompt)
        assert guard.feed(answer)+guard.finish()==answer
    for answer in ('我已经把饮水记录修改好了。','我已经把这句话保存到饮水记录里了。'):
        guard=PersistenceTextGuard(lambda:False,prompt)
        assert '尚未保存记录' in guard.feed(answer)+guard.finish()


@pytest.mark.parametrize('text', ['刚刚那件事你已经帮我记进记录里了吗？我想知道刚才有没有存。',
    '刚才保存了吗？','已经保存了吗？','这条修改好了吗？','你帮我记录了吗？','存了吗？'])
def test_immediate_status_request_is_read_only(text):
    assert status_query(text) and immediate_status(text)


@pytest.mark.parametrize('text', ['帮我记录今天喝了半升水。','如果没存的话请帮我记一下。','刚才保存了吗？如果没存，请帮我记一下。',
    '我能让你帮我记录吗？','例如“保存了吗”是什么意思？','解释“我已经记录了吗？”的语气。'])
def test_opportunistic_status_does_not_intercept_other_requests(text):
    assert not immediate_status(text)


def previous_run(tools, operation='create', category='water', target=None):
    ident = tools.turn('请帮我记录饮水。')
    draft = {'id':str(uuid4()), 'operation':operation, 'category':category, 'target':target}
    tools.runs[ident] = {'status':'complete', 'record_drafts':[draft]}
    return draft


def put_receipt(tools, draft, *, actor=7, **override):
    identifier = draft['id']
    action = 'health.records.add' if draft['operation']=='create' else 'expansion.records.update'
    metric = draft['category']=='metric'
    if metric:
        action='expansion.observations.' + ('create' if draft['operation']=='create' else 'update')
    receipt={'operation_id':identifier, 'action':action, 'status':'applied',
             'resource':'observations' if metric else 'life_records',
             'result_entity':{'user_id':actor, 'id':(draft.get('target') or {}).get('id',100), 'category':draft['category']},
             **override}
    tools.db.conn.execute('INSERT INTO rpc_requests VALUES(?,?,?,?,?)',
        (actor,str(uuid5(NAMESPACE,f'{actor}:{identifier}')),'unused',json.dumps({'mobile149':receipt}),None))


def status(tools, words='刚才这条记录保存了吗？', **extra):
    current=tools.turn(words)
    return last_record_status(tools,tools.principal,{'conversation_id':21,'assistant_message_id':current,**extra},words)


@pytest.mark.parametrize('operation,category,expected', [('create','diet','记录已保存'),('update','water','修改已保存'),
                                                       ('supplement','metric','补充已保存')])
def test_positive_requires_matching_receipt_and_preserves_actual_confirm(operation,category,expected):
    tools=Tools()
    draft=previous_run(tools,operation,category,{'id':100} if operation!='create' else None)
    assert '没有查到' in status(tools,decided_record_draft_ids=[draft['id']])
    put_receipt(tools,draft)
    assert expected in status(tools)
    assert expected in status(tools)
    tools.turn('我早上另外喝了两口水。')
    assert '没有查到' in status(tools)


@pytest.mark.parametrize('override', [{'status':'conflict'},{'operation_id':str(uuid4())},{'actor':8},
    {'result_entity':{'id':101,'user_id':7,'category':'water'}}, {'result_entity':{'id':100,'user_id':8,'category':'water'}},
    {'resource':'goals'}, {'action':'health.records.add'}, {'result_entity':None}])
def test_cancelled_failed_foreign_and_unrelated_receipts_never_support_success(override):
    tools=Tools()
    draft=previous_run(tools,'update','water',{'id':100})
    put_receipt(tools,draft,**override)
    assert '没有查到' in status(tools,decided_record_draft_ids=[draft['id']])


def test_legacy_linked_row_must_bind_meal_owner_and_target():
    tools=Tools()
    draft=previous_run(tools,'update','diet',{'id':9,'linked_meal_id':22,'linked_meal':{'id':22,'version':'before'}})
    def substitute(data):
        tools.db.conn.execute('INSERT OR REPLACE INTO rpc_requests VALUES(?,?,?,?,?)',(7,draft['id'],'irrelevant',json.dumps(data),None))
    substitute({'result':{'id':22,'user_id':8,'life_record_id':9,'version':'after'}})
    assert '没有查到' in status(tools)
    substitute({'result':{'id':22,'user_id':7,'life_record_id':9,'version':'after'}})
    assert '修改已保存' in status(tools)


def test_status_query_from_real_route_is_not_a_second_specialist_request():
    tools=Tools()
    cap=record.DIALOGUE_PHOTOS_CAPABILITY
    run_turn(tools,'请帮我记录今天三点喝了三百毫升水。',[change('water',{'amount_ml':300})],capability=cap)
    drafts,reply,upstream=run_turn(tools,'刚刚那件事你已经帮我记进记录里了吗？我想知道刚才有没有存。',
                                   [],capability=cap,expect_record=False,tasks_enabled=True)
    assert not drafts and not upstream and '没有查到' in reply
    assert all(v['status']=='complete' for v in tools.runs.values())


def test_receipt_must_not_validate_an_unrelated_or_named_past_record():
    tools=Tools()
    draft=previous_run(tools)
    put_receipt(tools,draft)
    assert '类别不一致' in status(tools,'刚才这条睡眠记录保存了吗？')
    assert '记录已保存' in status(tools,'刚才这条饮水记录保存了吗？')
    assert not immediate_status('我给小明的消息刚才保存了吗？')
    assert not immediate_status('我刚才说的昨天早餐小米粥保存了吗？')


def test_read_only_general_history_question_still_uses_model_but_no_record_tools():
    cap=record.DIALOGUE_PHOTOS_CAPABILITY
    drafts,_,upstream=run_turn(Tools(),'我去年所有的体重数据是否已经记录了？',[],capability=cap,expect_record=False)
    assert not drafts and upstream
    assert all(row['function']['name'] not in {'prepare_record','organize_record_drafts'}
               for row in upstream[0].get('tools',()))


@pytest.mark.parametrize('position',[381,382,383,384,508,509,510,511,512,765])
@pytest.mark.parametrize('fragmentation',['single','random','entire'])
def test_unpunctuated_long_public_stream_cannot_leak_joined_success_claim(position,fragmentation):
    text='嗯'*position+'我已经把你刚才说的事情保存到健康记录里了。'
    guard=PersistenceTextGuard(lambda:False)
    out=[]
    rand=random.Random(11871)
    index=0
    while index<len(text):
        size=1 if fragmentation=='single' else rand.randint(1,97) if fragmentation=='random' else len(text)
        out.append(guard.feed(text[index:index+size]))
        index+=size
    out.append(guard.finish())
    assert ''.join(out)=='嗯'*position+'这次尚未保存记录。'


def test_multimodal_status_checks_saved_photo_ids_and_never_creates_a_draft():
    tools=Tools()
    draft=previous_run(tools,category='diet')
    draft['photo_file_ids']=[41]
    put_receipt(tools,draft,result_entity={'id':9,'user_id':7,'category':'diet','details':{'photo_file_ids':[42]}})
    parts=[{'type':'text','text':'刚才图片存了吗？'},{'type':'image_url','image_url':{'url':PHOTO}}]
    drafts,reply,upstream=run_turn(tools,parts,[],capability=record.DIALOGUE_PHOTOS_CAPABILITY,
                                  expect_record=False,tasks_enabled=True)
    assert not drafts and not upstream and '没有查到' in reply
    tools.db.conn.execute('DELETE FROM rpc_requests')
    put_receipt(tools,draft,result_entity={'id':9,'user_id':7,'category':'diet','details':{'photo_file_ids':[41,42]}})
    assert '原草稿中的照片也保存在' in status(tools,'刚才图片存了吗？')


@pytest.mark.parametrize('mode', ['confirm','none','read'])
def test_owned_mobile_stream_guard_applies_with_every_tool_setting_but_not_json(mode):
    from types import SimpleNamespace
    proxy=SimpleNamespace(_capacity=None)
    opts={'model':'test','messages':[{'role':'user','content':'我喝了水'}],
          '_ai139':{'tool_mode':mode,'assistant_message_id':40,'conversation_id':3}}
    assert AI139Runner(proxy,None,('',None,opts)).presentation is not None
    assert AI139Runner(proxy,None,('',None,{**opts,'response_format':{'type':'json_object'}})).presentation is None


def test_cancel_is_an_owned_durable_idempotent_decision_and_does_not_leave_pending_task():
    from types import SimpleNamespace
    tools=TaskTools()
    repo=RecordTasks(tools)
    first=_seed(tools,1,amount_ml=300)
    put_receipt(tools,first)
    second=_seed(tools,2,amount_ml=200)
    task,=repo.list(tools.principal,21)['tasks']
    mid=task['assistant_message_id']
    assert task['status']=='ready' and task['draft_id']==second['id']
    tools.runs[mid].update(assistant_message_id=mid,sources=[],proposals=[])
    stranger=SimpleNamespace(user=SimpleNamespace(id=8,role_code='member',account_status='active'))
    for principal,message,draft_id in ((stranger,mid,second['id']),(tools.principal,mid-2,second['id']),
                                        (tools.principal,mid,str(uuid4()))):
        with pytest.raises(ToolError,match='record_task_unavailable'):
            repo.cancel_draft(principal,message,draft_id)
    assert repo.list(tools.principal,21)['total']==1
    with pytest.raises(ToolError,match='record_draft_changed'):
        repo.cancel_draft(tools.principal,mid-2,first['id'])
    ack=repo.cancel_draft(tools.principal,mid,second['id'])
    assert ack=={'id':second['id'],'status':'cancelled'}
    assert repo._get(tools.db.conn,7,21,task['id'],opened=False)['status']=='cancelled'
    assert tools._public_run(tools.runs[mid])['record_receipts']==[ack]
    assert tools.runs[mid]['record_drafts'][0]['blocked'] and record.draft_was_replaced(tools.db.conn,7,second['id'])
    assert RecordTasks(tools).list(tools.principal,21)=={'total':0,'active_task_id':None,'next_cursor':None,'tasks':[]}
    rows=tools.db.conn.execute('SELECT COUNT(*) FROM rpc_requests').fetchone()[0]
    assert RecordTasks(tools).cancel_draft(tools.principal,mid,second['id'])==ack
    assert rows==tools.db.conn.execute('SELECT COUNT(*) FROM rpc_requests').fetchone()[0]
    assert '已取消，没有保存' in status(tools)
    assert tools.db.conn.execute('SELECT COUNT(*) FROM life_records').fetchone()[0]==0


def test_cancelled_stale_card_cannot_close_or_reopen_newer_revision():
    tools=TaskTools()
    old,_,_=run_task(tools,'请记录2026-01-03早上八点喝水。',[change('water',{},stamp=None)])
    repo=RecordTasks(tools)
    before,=repo.list(tools.principal,21)['tasks']
    new,_,_=run_task(tools,'回到2026-01-03早上八点的那条饮水草稿，帮我补充饮水量三百五十毫升。',
                     [change('water',{'amount_ml':350},stamp=None)])
    with pytest.raises(ToolError,match='record_draft_changed'):
        repo.cancel_draft(tools.principal,before['assistant_message_id'],old[0]['id'])
    current,=repo.list(tools.principal,21)['tasks']
    assert current['id']==before['id'] and current['draft_id']==new[0]['id']
    repo.cancel_draft(tools.principal,current['assistant_message_id'],new[0]['id'])
    with pytest.raises(ToolError,match='record_task_unavailable'):
        repo.focus(tools.principal,21,before['id'])
    assert repo.list(tools.principal,21)['total']==0
