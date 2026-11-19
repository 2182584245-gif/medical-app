import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from server.member_ai_categories import _CategorySummaryRequest
from server.member_memory_core import MemberMemoryCore, MemoryCoreError
from server.platform_ai_proxy import AIProxyError, AIRequest, PlatformAIProxy


def prepare(*, worker='matching', enabled=True):
    principal=SimpleNamespace(user=SimpleNamespace(id=7,role_code='member'))
    auth=SimpleNamespace(resolve_token=lambda token:principal,identity=lambda p:nullcontext())
    proxy=object.__new__(PlatformAIProxy)
    proxy.auth=auth;proxy.developer_service=None
    proxy.settings=SimpleNamespace(envelope_path='test-envelope',key_path='test-key')
    proxy.key_loader=lambda *_:'test-only'
    proxy.quota=SimpleNamespace(key=b'test-only',reserve=lambda actor:None)
    proxy.operation_ledger=SimpleNamespace(existing=lambda *args:None,claim=lambda p,op,body,**kw:op)
    proxy.tool_service=SimpleNamespace(memory=SimpleNamespace(settings=lambda actor:{'enabled':enabled,'revision':'a'*64}))
    core=object.__new__(MemberMemoryCore)
    core.clock=lambda:datetime(2026,11,19,tzinfo=timezone.utc)
    def generic_retrieval(actor,question,**kwargs):
        start,end,bucket=core._retrieval_question_range(question,'all')
        core._period(start,end,bucket)
        raise AssertionError('fixture should reproduce the prior date error')
    proxy.multimodal_retrieval=SimpleNamespace(prepare_context=generic_retrieval)
    proxy.conversation_memory=SimpleNamespace()
    token=str(uuid4())
    data=json.dumps({'life_records':[{'day':'2026-11-19','details':{'notes':'测试'}},
                                     {'day':'2026-11-18','details':{'notes':'我喜欢喝温水。'}}]},ensure_ascii=False)
    value=AIRequest(model='deepseek-v4-flash',max_tokens=512,thinking={'type':'disabled'},
                    response_format={'type':'json_object'},personal_context=True,memory_revision='a'*64,
                    operation_kind='member.ai-profile.categories.summarize',operation_id=token,
                    messages=[{'role':'system','content':'只使用指定证据'}, {'role':'user','content':data}])
    req=SimpleNamespace(headers={'authorization':'Bearer '+'a'*43})
    if worker!='http':
        req=_CategorySummaryRequest(req,{'actor':8 if worker=='other actor' else 7,
                                          'token':str(uuid4()) if worker=='other job' else token})
    return data,asyncio.run(proxy.prepare(req,value))


def test_internal_matching_summary_uses_selected_sources_without_extra_generic_lookup():
    data,(_,_,body)=prepare()
    assert body['messages'][-1]['content']==data and '_personal11842' not in body
    assert body['_ai139']['memory_revision']=='a'*64 and body['_ai149']['kind']=='member.ai-profile.categories.summarize'


@pytest.mark.parametrize('worker',['other actor','other job','http'])
def test_user_or_mismatched_job_cannot_select_source_only_path(worker):
    with pytest.raises(MemoryCoreError,match='invalid_memory_range'):
        prepare(worker=worker)


def test_internal_worker_does_not_bypass_revoked_personal_context():
    with pytest.raises(AIProxyError) as error:
        prepare(enabled=False)
    assert error.value.code=='personal_context_changed'
