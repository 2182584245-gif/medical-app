"""Request-scoped public AI streaming; no keys, raw reasoning or private paths."""
from __future__ import annotations

import json
import time
import math
import threading
from contextvars import ContextVar
from contextlib import contextmanager
from uuid import UUID
from dataclasses import is_dataclass,asdict
from datetime import date,datetime

from mobile_activity import _PROGRESS
from ollama_chat_app.providers.platform_deepseek import _proposal, _sources, _record_drafts
from ollama_chat_app.services.knowledge_chat import _PublicAnswerStream
from ollama_chat_app.web_desktop.bridge import BridgeRequestError


_CANCEL_SCOPE=ContextVar('mobile_chat_cancel',default=None)
_CANCEL_LOCK=threading.Lock()
_CANCEL_REQUESTS={}


@contextmanager
def chat_request_scope(api,operation):
    """Only an already registered request in this member session can stop."""
    event=threading.Event()
    client=api._client
    owner=getattr(api,'_ai149_control_api',api)
    control_client=owner._client
    record=(owner,client,api._actor_id(),getattr(control_client,'session_serial',None),event,control_client)
    with _CANCEL_LOCK:
        if operation in _CANCEL_REQUESTS:
            raise BridgeRequestError('本次对话仍在处理中。','conflict')
        _CANCEL_REQUESTS[operation]=record
    token=_CANCEL_SCOPE.set(event)
    try:yield event
    finally:
        _CANCEL_SCOPE.reset(token)
        with _CANCEL_LOCK:
            if _CANCEL_REQUESTS.get(operation) is record:_CANCEL_REQUESTS.pop(operation,None)


def current_cancellation():return _CANCEL_SCOPE.get()


def cancel_request(api,operation):
    try:operation=str(UUID(operation))
    except (ValueError,TypeError,AttributeError):return False
    with _CANCEL_LOCK:
        record=_CANCEL_REQUESTS.get(operation)
        if record is None:return False
        owner,client,actor,serial,event,control_client=record
        try:current_actor=api._actor_id()
        except Exception:return False
        if owner is not api or api._client is not control_client or current_actor!=actor or getattr(control_client,'session_serial',None)!=serial:
            return False
        event.set()
        return True


def chat_envelope(value):
    """Preserve long completed answers; never silently truncate model text."""
    def convert(item,depth=0):
        if depth>32:raise BridgeRequestError('返回数据层级过深。','protocol')
        if item is None or type(item) in (bool,int):return item
        if type(item) is float:
            if not math.isfinite(item):raise BridgeRequestError('返回数字格式无效。','protocol')
            return item
        if isinstance(item,str):
            if len(item)>2000000:raise BridgeRequestError('返回文本超过模型显示范围。','limit')
            return item
        if isinstance(item,(date,datetime)):return item.isoformat()
        if is_dataclass(item):return convert(asdict(item),depth+1)
        if isinstance(item,dict):
            if len(item)>10000:raise BridgeRequestError('返回项目过多。','limit')
            return {str(key):convert(v,depth+1) for key,v in item.items()}
        if isinstance(item,(tuple,list)):
            if len(item)>10000:raise BridgeRequestError('返回项目过多。','limit')
            return [convert(v,depth+1) for v in item]
        raise BridgeRequestError('返回数据格式无效。','protocol')
    result={'ok':True,'data':convert(value)}
    if len(json.dumps(result,ensure_ascii=False).encode())>32*1024*1024:
        raise BridgeRequestError('本页对话内容过多，请缩小读取范围。','limit')
    return result


def public_stream(chunks):
    projection=_PublicAnswerStream()
    for chunk in chunks:
        yield from projection.feed(str(chunk))
    yield from projection.finish()


class ChatStreamBridge:
    """Each native event is tied to one member, client session and exchange."""

    def __init__(self, api, exchange, *, context=None, personal=False, activity=None):
        self.api=api;self.client=api._client;self.actor=api._actor_id()
        self.serial=getattr(self.client,'session_serial',None)
        self.conversation_id=exchange.conversation.id
        self.assistant_id=exchange.assistant_message.id
        self.context=context;self.personal=personal;self.activity=activity
        self.seq=0;self.checked_at=0;self.chunks_since_check=0
        self.sources=[];self.proposals=[];self.record_drafts=[]
        self.progress=_PROGRESS.get()

    def current(self, *, force=False, private_delivery=False):
        guard=getattr(self.api,'_ai149_guard',None)
        if callable(guard):guard()
        if (self.api._client is not self.client or self.api._actor_id()!=self.actor
                or getattr(self.client,'session_serial',None)!=self.serial):
            raise BridgeRequestError('账号已变化，本次回答已停止。','account_changed')
        now=time.monotonic()
        self.chunks_since_check+=1
        if self.personal and self.context is not None and (force or private_delivery and
                (not self.checked_at or now-self.checked_at>=1 or self.chunks_since_check>=16)):
            self.context.assert_current()
            self.checked_at=now;self.chunks_since_check=0

    def emit(self, kind, **fields):
        self.current(private_delivery=kind=='answer_delta')
        self.seq+=1
        value={'type':kind,'conversation_id':self.conversation_id,
               'assistant_message_id':self.assistant_id,'seq':self.seq,**fields}
        if len(json.dumps(value,ensure_ascii=False).encode())>256*1024:
            raise BridgeRequestError('AI 操作信息超过显示范围。','protocol')
        callback=getattr(self.progress,'onStream',None)
        if callable(callback):
            try:callback(json.dumps(value,ensure_ascii=False,separators=(',',':')))
            except Exception:pass

    def begin(self):
        self.emit('start',welcome_groups=getattr(self,'welcome_groups',[]),welcome_cleanup=getattr(self,'welcome_cleanup',None))

    def delta(self, value):
        if not isinstance(value,str):raise BridgeRequestError('AI 文本格式无效。','protocol')
        for offset in range(0,len(value),2048):
            self.emit('answer_delta',delta=value[offset:offset+2048])

    def observed(self, value):
        if not isinstance(value,dict):return
        kind=value.get('type')
        if kind=='web_sources':
            self.sources=_sources(value.get('sources'))
            value={'type':kind,'sources':self.sources}
        elif kind=='proposal':
            proposal=_proposal(value.get('proposal'))
            if not proposal:return
            self.proposals=[p for p in self.proposals if p['id']!=proposal['id']]+[proposal]
            value={'type':kind,'proposal':proposal}
        elif kind=='record_drafts':
            drafts=_record_drafts(value.get('drafts'))
            self.record_drafts=_record_drafts(self.record_drafts+drafts)
            value={'type':kind,'drafts':drafts}
        if kind not in {'thinking','tool_start','tool_done','web_sources','proposal','record_drafts'}:return
        if self.activity is not None:self.activity.stream_event(value)
        self.emit(kind,**{k:v for k,v in value.items() if k!='type'})

    def metadata(self):
        return {'sources':list(self.sources),'proposals':list(self.proposals),'record_drafts':list(self.record_drafts)}


def metadata_view(value, *, expected_message=None, expanded_photos=True):
    if not isinstance(value,dict):raise BridgeRequestError('工具记录响应无效。','protocol')
    if expected_message is not None and value.get('assistant_message_id')!=expected_message:
        raise BridgeRequestError('工具记录身份不一致。','protocol')
    proposals=[]
    for row in value.get('proposals',[]) if isinstance(value.get('proposals'),list) else []:
        clean=_proposal(row)
        if clean:proposals.append(clean)
    result={'sources':_sources(value.get('sources')),'proposals':proposals[:8],
            'record_drafts':_record_drafts(value.get('record_drafts'),expanded_photos=expanded_photos)}
    receipts = value.get('record_receipts')
    if isinstance(receipts, list):
        ids = {row.get('id') for row in receipts if isinstance(row, dict)
               and isinstance(row.get('id'), str) and row.get('status') == 'cancelled'}
        verified = [{'id':draft['id'], 'status':'cancelled'} for draft in result['record_drafts'] if draft['id'] in ids]
        if verified:
            result['record_receipts'] = verified
    if value.get('record_drafts_ready') is True:
        result['record_drafts_ready']=True
    return result


def source_footer(sources, answer):
    """Persist actual public sources even when the model omits its citations."""
    missing=[r for r in _sources(sources) if r['url'] not in answer]
    if not missing:return ''
    lines=['\n\n参考来源：']
    for row in missing:
        title=row['title'].replace('[','（').replace(']','）').replace('\n',' ')
        date=' · '+row['date'] if row.get('date') else ''
        url=row['url'].replace(' ','%20').replace('(','%28').replace(')','%29')
        lines.append('- ['+title+']('+url+')'+date)
    return '\n'.join(lines)
