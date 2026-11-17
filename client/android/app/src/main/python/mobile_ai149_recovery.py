"""Account-bound AI intents, frozen payloads and owner-checked cloud receipts.

No unknown inference is automatically retried under another identifier. Only
the native facade decides how an explicit user retry proceeds after readback.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
from copy import deepcopy
import json
import re
import base64
from uuid import UUID,uuid5
from urllib.parse import urlsplit

from ollama_chat_app.web_desktop.bridge import BridgeRequestError

_SCOPE=ContextVar('mobile_ai149_inference',default=None)
STATES=frozenset({'not_started','running','complete','failed','cancelled','expired'})
INFERENCE_ACTIONS=frozenset({'chat.send','voice.records.propose','voice.smart-record',
    'mobile.image.analyze','expansion.analyses.run','ai.statistics.explain','voice.polish'})


def normalize_image_payload(payload):
    """Create JPEG transport copies of WebP images; leave originals untouched.

    The mobile file store accepts WebP, while the authenticated proxy's public
    image contract accepts JPEG/PNG. This common boundary covers both current
    attachments and historical images, and runs before payload freezing.
    """
    if type(payload) is not dict:return payload
    result=None;converted={}
    for index,message in enumerate(payload.get('messages',[])):
        if not isinstance(message,dict) or not isinstance(message.get('content'),list):continue
        for offset,part in enumerate(message['content']):
            if not isinstance(part,dict) or part.get('type')!='image_url':continue
            image=part.get('image_url');url=image.get('url') if isinstance(image,dict) else None
            prefix='data:image/webp;base64,'
            if not isinstance(url,str) or not url.startswith(prefix):continue
            if url not in converted:converted[url]=_webp_to_jpeg(url[len(prefix):])
            if result is None:result=deepcopy(payload)
            result['messages'][index]['content'][offset]['image_url']={**image,'url':'data:image/jpeg;base64,'+converted[url]}
    return result if result is not None else payload


def _webp_to_jpeg(encoded):
    try:
        raw=base64.b64decode(encoded,validate=True)
        if not 20<=len(raw)<=2*1024*1024 or raw[:4]!=b'RIFF' or raw[8:12]!=b'WEBP' or int.from_bytes(raw[4:8],'little')+8!=len(raw):raise ValueError
        try:from java import jclass
        except ImportError:
            import io,warnings
            from PIL import Image
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as original:
                    if original.format!='WEBP' or max(original.size)>12000 or original.width*original.height>20_000_000:raise ValueError
                    original.load()
                    image=Image.new('RGB',original.size,'white')
                    if 'A' in original.getbands():image.paste(original.convert('RGB'),mask=original.getchannel('A'))
                    else:image.paste(original.convert('RGB'))
                    stream=io.BytesIO();image.save(stream,format='JPEG',quality=95);data=stream.getvalue()
        else:
            value=json.loads(str(jclass('cn.healthlife.local.NativeImages').toJpeg(encoded)))
            if value.get('ok') is not True or value.get('mime')!='image/jpeg':raise ValueError
            data=base64.b64decode(value['data_base64'],validate=True)
        if not 0<len(data)<=2*1024*1024 or data[:2]!=b'\xff\xd8' or data[-2:]!=b'\xff\xd9':raise ValueError
        return base64.b64encode(data).decode('ascii')
    except Exception:
        raise BridgeRequestError('这张图片暂时无法转换，请重新选择较小的清晰图片。','invalid_image') from None


def operation_uuid(value):
    try:
        if type(value) is not str or str(UUID(value))!=value:raise ValueError
        return value
    except (ValueError,TypeError,AttributeError):
        raise BridgeRequestError('AI 操作编号无效。','validation') from None


@dataclass
class InferenceAttempt:
    operation_id:str
    operation_kind:str
    checkpoint:object=None
    phase:str='preflight'
    submitted:list=field(default_factory=list)
    counters:dict=field(default_factory=dict)
    last_operation_id:str|None=None
    requires_new_attempt:bool=False

    @property
    def has_submitted(self):return bool(self.submitted)

    def snapshot(self):
        return {'phase':self.phase,'operation_id':self.operation_id,
                'operation_kind':self.operation_kind,'has_submitted':self.has_submitted,
                'last_operation_id':self.last_operation_id,
                'requires_new_attempt':self.requires_new_attempt,
                'submitted':[dict(row) for row in self.submitted]}

    def publish(self):
        if callable(self.checkpoint):self.checkpoint(self.snapshot())

    def prepare(self,client,payload,*,stream=False):
        if type(payload) is not dict:raise BridgeRequestError('AI 请求格式无效。','validation')
        if stream and self.operation_kind=='chat.send':
            identifier=self.operation_id;phase='answer'
        else:
            phase='json' if payload.get('response_format',{}).get('type')=='json_object' else 'complete'
            ordinal=self.counters.get(phase,0)+1;self.counters[phase]=ordinal
            identifier=str(uuid5(UUID(self.operation_id),'ai:'+phase+':'+str(ordinal)))
        prepared={**payload,'operation_id':identifier,'operation_kind':self.operation_kind}
        # Encoding validation happens before an intent is marked submitted.
        try:
            encoded=json.dumps(prepared,ensure_ascii=False,allow_nan=False,separators=(',',':'))
            if len(encoded.encode('utf8'))>32*1024*1024:raise ValueError
        except (TypeError,ValueError,UnicodeError):
            raise BridgeRequestError('AI 请求超过有效范围。','validation') from None
        frozen=getattr(client,'ai149_payload_store',None)
        if callable(frozen):
            saved=frozen(identifier,json.loads(encoded))
            if type(saved) is not dict or saved.get('operation_id')!=identifier or saved.get('operation_kind')!=self.operation_kind:
                raise BridgeRequestError('本机 AI 请求记录无法核对。','storage')
            legacy_webp=any(isinstance(part,dict) and isinstance(part.get('image_url'),dict)
                and str(part['image_url'].get('url','')).startswith('data:image/webp;base64,')
                for message in saved.get('messages',[]) if isinstance(message,dict) and isinstance(message.get('content'),list)
                for part in message['content'])
            if legacy_webp:
                receipt=query_operation(client,identifier,self.operation_kind)
                if receipt['state']=='not_started':
                    self.requires_new_attempt=True;self.phase='preflight_rejected';self.last_operation_id=identifier;self.publish()
                    raise BridgeRequestError('旧附件尚未提交给 AI，请点击重新发送以使用兼容的图片格式。','ai_attempt_finished')
                raise BridgeRequestError('正在核对旧附件处理结果，请先查看当前对话。',
                                         'ai_in_progress' if receipt['state']=='running' else 'outcome_uncertain')
            # A retry must never silently restore revoked sharing/settings.
            controls=('personal_context','memory_revision','web_search','tool_mode','assistant_team','record_tasks','record_task_id')
            if any(saved.get(key)!=prepared.get(key) for key in controls):
                raise BridgeRequestError('资料授权或 AI 设置已改变，请先核对上次回答后重新提问。','ai_context_changed')
            prepared=saved
        self.phase='submitted';self.last_operation_id=identifier
        self.submitted.append({'operation_id':identifier,'kind':self.operation_kind,'phase':phase})
        self.publish()
        return prepared

    def completed(self,identifier):
        self.phase='received';self.last_operation_id=identifier;self.publish()


@contextmanager
def inference_scope(operation_id,kind,checkpoint=None):
    identifier=operation_uuid(operation_id)
    if kind not in INFERENCE_ACTIONS:raise BridgeRequestError('AI 功能类别无效。','validation')
    attempt=InferenceAttempt(identifier,kind,checkpoint)
    token=_SCOPE.set(attempt)
    try:
        attempt.publish()
        yield attempt
    finally:_SCOPE.reset(token)


def current_attempt():return _SCOPE.get()


def prepare_payload(client,payload,*,stream=False):
    attempt=current_attempt()
    return attempt.prepare(client,payload,stream=stream) if attempt else payload


def received(payload):
    attempt=current_attempt()
    if attempt and payload.get('operation_id'):attempt.completed(payload['operation_id'])


def _status(value,identifier,kind):
    if (type(value) is not dict or value.get('operation_id')!=identifier or value.get('kind')!=kind
            or value.get('state') not in STATES or type(value.get('retry_safe')) is not bool):
        raise BridgeRequestError('AI 操作状态暂时无法核对，请稍后重试。','protocol')
    result={key:value[key] for key in ('operation_id','kind','state','retry_safe')}
    for key in ('conversation_id','assistant_message_id'):
        item=value.get(key)
        if item is not None:
            if type(item) is not int or not 1<=item<=9007199254740991:
                raise BridgeRequestError('AI 对话状态暂时无法核对。','protocol')
            result[key]=item
    error=value.get('error_code')
    if error is not None:
        if type(error) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,79}',error):
            raise BridgeRequestError('AI 状态代码无法核对。','protocol')
        result['error_code']=error
    if value.get('response') is not None:
        response=value['response']
        if (result['state']!='complete' or type(response) is not dict
                or type(response.get('content')) is not str or not response['content'].strip()
                or len(response['content'])>2_000_000 or response.get('model')!='deepseek-flash'):
            raise BridgeRequestError('AI 已完成结果无法核对，请查看对话记录。','protocol')
        from ollama_chat_app.providers.platform_deepseek import _sources,_proposal,_record_drafts
        sources=_sources(response.get('sources',[]));proposals=[]
        for item in response.get('proposals',[])[:8] if isinstance(response.get('proposals',[]),list) else []:
            parsed=_proposal(item)
            if parsed:proposals.append(parsed)
        result['response']={'content':response['content'],'model':'deepseek-flash','sources':sources,'proposals':proposals,'record_drafts':_record_drafts(response.get('record_drafts'))}
    return result


def query_operation(client,operation_id,kind):
    identifier=operation_uuid(operation_id)
    if kind not in INFERENCE_ACTIONS:raise BridgeRequestError('AI 功能类别无效。','validation')
    value=client.request('GET','/v1/ai/operations/'+identifier,params={'kind':kind})
    return _status(value,identifier,kind)


def cancel_operation(client,operation_id,kind):
    identifier=operation_uuid(operation_id)
    if kind not in INFERENCE_ACTIONS:raise BridgeRequestError('AI 功能类别无效。','validation')
    value=client.request('POST','/v1/ai/operations/'+identifier+'/cancel',json={'kind':kind})
    return _status(value,identifier,kind)


def restore_chat_completion(api,operation_id,body,status):
    """Finalize/read the exact completed paid answer, without another model call.

    A completed JSON selection child is not the chat answer and deliberately
    returns None, so its cached result can be replayed by the coordinator.
    """
    identifier=operation_uuid(operation_id)
    if (not isinstance(status,dict) or status.get('state')!='complete'
            or status.get('operation_id')!=identifier or status.get('kind')!='chat.send'):
        return None
    packet=api._client.request('POST','/v1/ai/operations/'+identifier+'/restore-chat',json={'kind':'chat.send'})
    if type(packet) is not dict:
        raise BridgeRequestError('上次回答暂时无法恢复，请稍后重试。','protocol')
    conversation=packet.get('conversation');user=packet.get('user_message');assistant=packet.get('assistant_message')
    if any(type(item) is not dict for item in (conversation,user,assistant)):
        raise BridgeRequestError('上次对话记录无法核对。','protocol')
    for item in (conversation,user,assistant):
        if type(item.get('id')) is not int or not 1<=item['id']<=9007199254740991:
            raise BridgeRequestError('上次对话编号无法核对。','protocol')
    if (assistant.get('role')!='assistant' or assistant.get('status')!='complete'
            or type(assistant.get('content')) is not str or not assistant['content'].strip()
            or len(assistant['content'])>2_000_000 or user.get('role')!='user'
            or type(user.get('content')) is not str or user['content']!=body.get('content')
            or packet.get('operation_id',identifier)!=identifier
            or any(item.get('conversation_id',conversation['id'])!=conversation['id'] for item in (user,assistant))
            or body.get('conversation_id') is not None and body['conversation_id']!=conversation['id']
            or status.get('conversation_id') is not None and status['conversation_id']!=conversation['id']
            or status.get('assistant_message_id') is not None and status['assistant_message_id']!=assistant['id']):
        raise BridgeRequestError('上次回答与本次请求不一致，请查看对话记录。','conflict')
    message_fields={'id','conversation_id','sequence_no','role','content','status','error_message','provider','model','created_at','updated_at'}
    clean_user={key:value for key,value in user.items() if key in message_fields}
    clean_assistant={key:value for key,value in assistant.items() if key in message_fields}
    from mobile_ai139 import metadata_view
    extra=metadata_view({'sources':assistant.get('sources',packet.get('sources',[])),
                         'proposals':assistant.get('proposals',packet.get('proposals',[])),
                         'record_drafts':assistant.get('record_drafts',packet.get('record_drafts',[]))})
    clean_assistant.update(extra)
    activity=assistant.get('activity',packet.get('activity'))
    if activity is not None:
        from ollama_chat_app.services.chat_activity import validate_activity
        clean_assistant['activity']=validate_activity(activity)
    return {'conversation':{key:value for key,value in conversation.items() if key in
        {'id','title','provider','model','created_at','updated_at'}},
        'user_message':clean_user,'assistant_message':clean_assistant,
        'sources':extra['sources'],'proposals':extra['proposals'],'record_drafts':extra['record_drafts'],'model':'deepseek-flash',
        'recovered':True,**({'activity':clean_assistant['activity']} if 'activity' in clean_assistant else {})}


@contextmanager
def bind_ai_bridge(main_bridge,guard=None):
    """Freeze identity in a request-local API and independent HTTP connection.

    The main client's stream/transport lock is never retained. A source test
    double may be shared only when it is explicitly not a CloudAPIClient.
    """
    from mobile_providers import MobileMemberBridge,ScopedCloudClient
    from ollama_chat_app.services.cloud_client import CloudAPIClient
    import httpx
    with main_bridge._lock:
        api=main_bridge._api;api._ensure_client()
        source=api._client;user=deepcopy(api._user or {})
        actor=user.get('id');generation=main_bridge._generation
        if type(actor) is not int or actor<=0 or user.get('role_code')!='member':
            raise BridgeRequestError('请先登录会员账号。','not_authenticated')
        serial=getattr(source,'session_serial',None)
        endpoint=getattr(source,'base_url',None) or api._base_url
        overrides=dict(getattr(api,'_services_override',{}))
        if isinstance(source,CloudAPIClient):
            with source._lock:
                token=source._token
                if not token:raise BridgeRequestError('连接网络并登录后可使用 AI。','offline')
                snapshot={name:deepcopy(getattr(source,name,None)) for name in (
                    '_user','_session_serial','_application_settings_revision',
                    '_configured_read_timeout','sync_protocol','_data_generation')}
                payload_store=getattr(source,'ai149_payload_store',None)
                transport=source._http._transport if isinstance(source._http._transport,httpx.MockTransport) else None
        else:token=None;snapshot={};payload_store=getattr(source,'ai149_payload_store',None);transport=None

    def require_current():
        valid=(main_bridge._generation==generation and main_bridge._api is api and api._client is source
               and (api._user or {}).get('id')==actor and getattr(source,'session_serial',None)==serial)
        if not valid or callable(guard) and not guard():
            raise BridgeRequestError('账号已变化，本次 AI 操作已停止。','account_changed')
        return True

    class BoundClient(ScopedCloudClient):
        def request(self,method,path,**kwargs):
            require_current();value=super().request(method,path,**kwargs);require_current();return value
        def ai_stream(self,payload,**kwargs):
            require_current()
            for event in super().ai_stream(payload,**kwargs):
                require_current();yield event

    class BoundBridge(MobileMemberBridge):
        def request(self,action,payload=None):
            try:
                require_current();value=super().request(action,payload);require_current();return value
            except BridgeRequestError as error:return self._api._failure(error)

    created=isinstance(source,CloudAPIClient)
    parsed=urlsplit(endpoint)
    # Only preserve an explicit, already-validated developer loopback source;
    # normal application endpoints retain HTTPS enforcement.
    loopback=(parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'})
    client=BoundClient(endpoint,transport=transport,allow_loopback_http=loopback) if created else source
    if created:
        client._token=token
        for name,value in snapshot.items():setattr(client,name,value)
    if created:client.ai149_payload_store=payload_store
    bound=BoundBridge(base_url=endpoint,client=client,services=overrides)
    bound._generation=generation;bound._api._user=user
    bound._api._ai149_control_api=api;bound._api._ai149_guard=require_current
    try:
        require_current();yield bound
    finally:
        if created:client.close()
