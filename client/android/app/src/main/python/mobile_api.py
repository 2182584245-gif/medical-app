"""Fixed authenticated cloud facade used by the packaged Android WebView."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import nullcontext, contextmanager
from contextvars import ContextVar
import time
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from mobile_providers import MobileMemberBridge, ScopedCloudClient, RECORD_TASK_ACTIONS11864
from mobile_avatar import AvatarStore
from mobile_onboarding11830 import ACTIONS as ONBOARDING_ACTIONS
from mobile_voice import VoiceError, read_capture, upload_capture
from mobile_memory import MEMORY_ROUTES, CATEGORY_READS, CATEGORY_WRITES
from mobile_reading import READING_ACTIONS, READING_READS
from mobile_account_metadata11841 import ACTIONS as ACCOUNT_ACTIONS11841
from mobile_record_assistant import RECORD_DECISIONS
from ollama_chat_app.config import DEFAULT_ALIYUN_ENDPOINT
from ollama_chat_app.web_desktop.member_bridge import MEMBER_ACTIONS
from ollama_chat_app.web_desktop.expansion import EXPANSION_ACTIONS

_LOCK = threading.RLock()
_APP = None
_GUEST_PROGRESS11614=ContextVar('guest_progress11614',default=None)
_REQUEST_IDENTITY149=ContextVar('mobile_request_identity149',default=None)
_MAX_REQUEST_BYTES = 26 * 1024 * 1024
_REMOVED = {name for name in MEMBER_ACTIONS if name.startswith(('files.report', 'files.reports', 'ai.draft', 'ai.proposal'))}
_REMOVED |= {'files.list', 'files.delete', 'files.storage', 'ai.member.draft', 'ai.life-records.propose', 'ai.context', 'voice.transcribe'}
_REMOVED |= {name for name in EXPANSION_ACTIONS if '.memories.' in name or name.startswith('expansion.reviews.')}
MOBILE_ACTIONS = (MEMBER_ACTIONS | EXPANSION_ACTIONS | {'voice.records.propose', 'voice.records.confirm', 'forum.image.get', 'chat.message.attachments', 'chat.image.get', 'mobile.image.analyze', 'mobile.weather.current', 'forum.member.profile'}) - _REMOVED
MOBILE_ACTIONS |= set(MEMORY_ROUTES)
MOBILE_ACTIONS |= {'chat.files.list'}
MOBILE_ACTIONS |= ONBOARDING_ACTIONS | {'chat.greeting.ensure'}
MOBILE_ACTIONS |= {'forum.discovery.search'}
MOBILE_ACTIONS |= {'forum.detail.get','forum.detail.comments','forum.detail.like','forum.detail.bookmark','forum.detail.follow','forum.detail.comment'}
MOBILE_ACTIONS |= {'chat.tools.proposals','chat.tools.confirm','chat.tools.cancel'}
MOBILE_ACTIONS |= RECORD_TASK_ACTIONS11864
MOBILE_ACTIONS |= RECORD_DECISIONS
MOBILE_ACTIONS |= READING_ACTIONS
MOBILE_ACTIONS |= ACCOUNT_ACTIONS11841
LOCAL149_ACTIONS={'local149.draft.get','local149.draft.save',
                  'local149.draft-attachment.get','local149.draft-attachment.save','local149.draft-attachment.delete'}
MOBILE_ACTIONS |= LOCAL149_ACTIONS
MOBILE_ACTIONS |= {'ai.operations.status','ai.operations.cancel'}
_AI_CONTEXT_ACTIONS149=frozenset({'member.ai-profile.categories.get',
    'member.ai-profile.categories.refresh','member.memory.preferences.summarize',
    'member.memory.events.append'})
_ONLINE_READ_ACTIONS11823=frozenset({
    'forum.discovery.search','forum.detail.get','forum.detail.comments','mobile.weather.current','account.metadata.get',
    'member.memory.conversation.status'})
_REMINDER_REFRESH_ACTIONS11828=frozenset({
    'health.reminders.add','health.reminders.update','health.reminders.delete','health.reminders.toggle',
    'expansion.goals.create','expansion.goals.update','expansion.goals.close',
    'expansion.goals.checkin','expansion.goals.uncheckin','expansion.goals.delete',
    'health.records.add','health.records.delete','expansion.records.update','expansion.records.delete',
    'voice.records.confirm','chat.tools.confirm','chat.records.confirm','local149.resolve'})


def _error(code, message):
    return {'ok': False, 'error': {'code': code, 'message': message}}


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class MobileApplication:
    def __init__(self, root, *, bridge=None, client_factory=None, base_url=None, vault=None, native_hooks=None, guest_enabled=False, guest_transport=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge or MobileMemberBridge(base_url=base_url or DEFAULT_ALIYUN_ENDPOINT, client_factory=client_factory or ScopedCloudClient)
        self._finished = {}
        self._finished_actors = {}
        self._intent_path = self.root / 'mobile-operation-status.json'
        self._intents = self._read_intents()
        self._avatars = AvatarStore(self.root,vault=vault,endpoint=self.bridge._api._base_url)
        self._intent_lock=threading.RLock();self._auth_lock=threading.RLock();self._auth_epoch=0
        self._active_condition=threading.Condition(threading.RLock());self._active_requests={}
        self._account_reset=None
        self._account_generation=None
        self._native_hooks=native_hooks;self.local=None;self.session=None
        self._vault=vault;self._erasure_journal=None;self._erasure_pending=None;self._erasure_lock=threading.RLock()
        self.guest_enabled=bool(guest_enabled);self.guest=None
        if self.guest_enabled:
            if vault is None:raise ValueError("Guest secure storage unavailable")
            from mobile_guest11614 import GuestLifecycle,migrate_auto_login
            self.guest=GuestLifecycle(self.root,vault,self.bridge._api._base_url,transport=guest_transport)
            migrated=migrate_auto_login(self.root)
            if migrated and native_hooks is not None:native_hooks.deactivate()
        if vault is not None:
            from mobile_session149 import LocalSession149
            from mobile_sync149 import LocalFirst149
            from mobile_client_reset import ErasureJournal
            self._erasure_journal=ErasureJournal(self.root,vault,self.bridge._api._base_url)
            self._erasure_pending=self._erasure_journal.load()
            self.session=LocalSession149(self.root,vault,self.bridge._api._base_url)
            from mobile_account_reset11831 import AccountReset11831
            self._account_reset=AccountReset11831(self)
            if self._erasure_pending is None and not self._account_reset.pending and not (self.guest and self.guest.pending):self.session.restore(self.bridge._api)
            self.local=LocalFirst149(self.root,vault,self._local_identity,self._online,
                self._cloud_transport,request_sync=self._request_sync,background_handler=self._background_read)
            from mobile_account_generation11853 import AccountGeneration11853
            self._account_generation=AccountGeneration11853(self)
            if self._erasure_pending is not None:self._run_erasure('pending-erasure-receipt-check')
            self._attach_payload_store()
        if self.guest and self._erasure_pending is None and not (self._account_reset and self._account_reset.pending) and not self.guest.pending and not (self.session and self.session.user()):self.guest.activate()

    def _local_identity(self):
        if self._erasure_pending is not None or self._account_reset and self._account_reset.pending or self.guest and self.guest.pending:return {'authorized':False,'epoch':self._auth_epoch}
        if self.session is None:return {'authorized':False,'epoch':self._auth_epoch}
        if self._account_generation is not None and self._account_generation.required:
            return {'authorized':False,'epoch':self._auth_epoch,'account_verification_required':True}
        return self.session.identity()

    def _verify_account_generation(self):
        if self._account_generation is None:
            from mobile_local149 import LocalDataError
            raise LocalDataError('account_verification_required','本机安全核验暂不可用，请重新打开应用。')
        return self._account_generation.ensure()

    def _verification_bootstrap(self):
        data=self.bridge._api._bootstrap_data()
        value=self.session.value if self.session is not None else None
        user=(value or{}).get('user')or{}
        data['user']=({key:user[key]for key in ('id','username','role_code','account_status')if key in user}|{'display_name':''})if user else None
        data['account_verification_required']=True
        data['cloud']={**data.get('cloud',{}),'authenticated':False}
        return {'ok':True,'result':data}

    def _attach_payload_store(self):
        if self.local is None or self.bridge._api._client is None:return
        if self._local_identity().get('authorized') is not True:
            self.bridge._api._client.ai149_payload_store=None
            return
        identity=self.local.identity()
        def payload_store(identifier,payload=None):
            if str(UUID(identifier))!=identifier:raise ValueError('Invalid AI operation')
            self.local._guard(identity)
            key='ai-payload:'+identifier
            result=(self.local.store.get(identity,'meta',key) if payload is None else
                self.local.store.put_if_absent(identity,'meta',key,payload))
            self.local._guard(identity);return result
        self.bridge._api._client.ai149_payload_store=payload_store

    def native_scope(self):
        if self.guest and (self.guest.pending or self.guest.facade):
            descriptor=self.guest.descriptor()
            return {'kind':'guest','scope_id':'guest:'+descriptor['id'],'guest_id':descriptor['id'],
                'session_epoch':descriptor['session_epoch'],'cleanup_pending':descriptor['cleanup_pending'],
                'private_root':str(self.guest.base/descriptor['id']),'camera_root':str(self.guest.base/'picked-images'/descriptor['id'])}
        if(self._account_reset and self._account_reset.pending or self._erasure_pending is not None
            or self._account_generation is not None and self._account_generation.required):
            return {'kind':'unavailable','scope_id':'','session_epoch':str(self._auth_epoch),'cleanup_pending':True}
        current=self.bridge._api._user or {}
        return {'kind':'member','actor_id':current.get('id',0),'scope_id':'member:'+str(current.get('id',0)),
            'session_epoch':str(self._auth_epoch),'private_root':str(self.root)}

    def _guest_request(self,params,progress=None):
        from mobile_guest11614 import GuestError
        if not self.guest or self.guest.pending:return _error('guest_cleanup_pending'if self.guest and self.guest.pending else'login_required','请先完成登录或体验资料清理。')
        facade=self.guest.facade
        if facade is None:return _error('account_changed','体验会话已切换。')
        if set(params)-{'action','payload','request_id','guest_id'} or params.get('guest_id')!=facade.id:return _error('account_changed','体验会话已切换。')
        body=params.get('payload',{})
        if not isinstance(body,dict):return _error('validation','体验请求内容无效。')
        body=dict(body);owner=body.pop('_fawnsee_actor_id',facade.id)
        if owner!=facade.id:return _error('account_changed','体验会话已切换。')
        try:
            value=facade.request(params.get('action'),body,params.get('request_id'),progress=progress)
            if params.get('action')=='chat.messages.list'and isinstance(value,dict):value={**value,'next_cursor':value.get('next_before_sequence_no')}
            return {'ok':True,'result':value}
        except GuestError as error:return _error(error.code,str(error))

    def _guest_bootstrap(self):
        if not self.guest:return None
        if not self.guest.pending and self.guest.facade is None and not (self.bridge._api._user or self.session and self.session.user()):self.guest.activate()
        if self.guest.pending or self.guest.facade:
            value=self.bridge._api._bootstrap_data();value['user']=None;value['cloud']={**value.get('cloud',{}),'authenticated':False};value['guest']=self.guest.descriptor()
            return {'ok':True,'result':value}
        return None

    def _finalize_member_login(self,reply,auto_login):
        self.session.remember(self.bridge._api._client,self.bridge._api._user,auto_login=auto_login)
        if self.guest and self.guest.pending:self.guest.finish()
        try:self._verify_account_generation()
        except Exception:
            # Login succeeds independently; private views stay locked until a
            # separate online generation check succeeds.
            self.bridge._api._client.ai149_payload_store=None
            return reply
        self._attach_payload_store();self._request_sync()
        if self._native_hooks is not None:self._native_hooks.remindersChanged()
        return reply

    def _resume_guest_cleanup(self):
        if not self.guest or not self.guest.pending:return {'ok':True,'result':{'completed':True}}
        try:
            self.guest.cleanup(self._native_hooks);pending=self.guest.pending;value=pending['session']
            self.bridge._api._ensure_client();client=self.bridge._api._client
            envelope={'access_token':value['token'],'token_type':'bearer','user':value['user'],'offline_lease':value['lease'],
                'recovery_token':value.get('recovery'),'refresh_token':value.get('refresh_token'),'refresh_expires_at':value.get('refresh_expires_at'),'sync_protocol':2}
            try:current=client._accept_mobile_session(envelope)
            except Exception:
                if not value.get('refresh_token'):return self._guest_reauthentication()
                identifier=value.get('refresh_request_id')or str(uuid4());value['refresh_request_id']=identifier;self.guest.save()
                try:current=client.mobile_refresh(value['refresh_token'],identifier)
                except Exception as error:
                    if getattr(error,'code',None)in {'authentication','not_authenticated','permission','session_expired'}:return self._guest_reauthentication()
                    raise
                # A rotated refresh response is sealed before auto-login or
                # exposure. A lost response retries its previously frozen ID.
                self.session.remember(client,current,auto_login=False);pending['session']=dict(self.session.value);self.guest.save()
            self.bridge._api._user=current;self.bridge._api._reset_services()
            self._finalize_member_login(pending['reply'],pending['auto_login'])
            return {'ok':True,'result':{'completed':True}}
        except Exception:return _error('guest_cleanup_pending','登录已验证，体验资料清理尚未完成。请点击继续完成清理，资料不会混入正式账号。')

    def _guest_reauthentication(self):
        # Guest payloads are already purged. A revoked formal session must not
        # trap the app forever in cleanup; require a fresh explicit login.
        if not self.guest or not self.guest.pending or self.guest.pending['phase']!='local_done':raise ValueError('Guest cleanup not confirmed')
        self.guest.finish();self.session.deactivate();self.bridge._api._user=None
        if self.bridge._api._client is not None:self.bridge._api._client.clear_session(clear_recovery=True)
        self.bridge._api._reset_services();self._auth_epoch+=1;self.guest.activate()
        return {'ok':True,'result':{'completed':True,'authentication_required':True}}

    def _request_sync(self):
        if self._native_hooks is not None:self._native_hooks.requestSync()

    def _revoke_online(self,actor,epoch):
        with self._auth_lock:
            if epoch!=self._auth_epoch or (self.bridge._api._user or {}).get('id')!=actor:return
            self._auth_epoch+=1
            if self._account_generation is not None:self._account_generation.suspend()
            if self.session is not None:self.session.deactivate()
            client=self.bridge._api._client
            if client is not None:client.clear_session(clear_recovery=False)
            self.bridge._api._user=None;self.bridge._api._reset_services();self.bridge._generation+=1
            if self._native_hooks is not None:self._native_hooks.deactivate()

    def _set_intent(self,key,value):
        with self._intent_lock:
            owner=_REQUEST_IDENTITY149.get()
            if owner and owner[1]!=self._auth_epoch:return
            self._intents[key]={**value,**({'actor':owner[0]}if owner and type(owner[0])is int and owner[0]>0 else {})};self._save_intents()

    def _cloud_transport(self,method,path,*,expected_identity=None,**kwargs):
        from mobile_ai149_recovery import bind_ai_bridge
        expected=expected_identity or self._local_identity()
        captured=self._auth_epoch
        def guard():
            identity=self._local_identity()
            return (captured==self._auth_epoch and identity.get('authorized') is True
                and identity.get('actor_id')==expected.get('actor_id')
                and str(identity.get('epoch'))==str(expected.get('epoch'))
                and identity.get('endpoint')==expected.get('endpoint'))
        if not guard():raise ValueError('Account changed before synchronization')
        try:
            with bind_ai_bridge(self.bridge,guard=guard) as bound:
                return bound._api._client.request(method,path,**kwargs)
        except Exception as error:
            if getattr(error,'code',None)=='account_data_reset':
                from mobile_local149 import Identity
                with self._auth_lock:
                    if captured==self._auth_epoch and(self.bridge._api._user or{}).get('id')==expected.get('actor_id'):
                        self.local.store.invalidate_account(Identity.checked(expected))
                        self._auth_epoch+=1;self.bridge._generation+=1
                        if self._account_generation is not None:self._account_generation.suspend()
                        if self._native_hooks is not None:
                            invalidate=getattr(self._native_hooks,'invalidateGuestScope',None)
                            if callable(invalidate):invalidate()
                            self._native_hooks.deactivate()
            if getattr(error,'code',None) in {'authentication','not_authenticated'}:
                self._revoke_online(expected.get('actor_id'),captured)
            raise

    def _background_read(self,action,body,operation_id,expected_identity):
        from mobile_sync149 import READ_ACTIONS
        from mobile_ai149_recovery import bind_ai_bridge
        if action not in READ_ACTIONS:return _error('permission','此操作不能后台执行。')
        captured=self._auth_epoch
        def guard():
            current=self._local_identity()
            return (captured==self._auth_epoch and current.get('authorized') is True
                and current.get('actor_id')==expected_identity.get('actor_id')
                and current.get('endpoint')==expected_identity.get('endpoint')
                and str(current.get('epoch'))==str(expected_identity.get('epoch')))
        if not guard():return _error('account_changed','账号已改变。')
        if action in _AI_CONTEXT_ACTIONS149:
            # A worker already holds the sync lock. Never start another sync or
            # let a conflicted privacy change reach an AI-triggering read.
            body,error=self._prepare_ai_context149(action,body,synchronize=False)
            if error:return error
            if not guard():return _error('account_changed','账号已改变。')
        with bind_ai_bridge(self.bridge,guard=guard) as bound:
            with (bound._api._client.operation(operation_id) if hasattr(bound._api._client,'operation') else nullcontext()):
                reply=self._translate(bound.request(action,{**body,'_fawnsee_actor_id':expected_identity['actor_id']}))
        if not reply.get('ok') and reply.get('error',{}).get('code') in {'authentication','not_authenticated'}:
            self._revoke_online(expected_identity['actor_id'],captured)
        return reply

    def _prepare_ai_context149(self,action,body,*,synchronize=True):
        body=dict(body)
        if self.local is None:return body,None
        personal=self.local.ai_context_allowed()
        personal_only=action in {'member.ai-profile.categories.refresh','member.memory.preferences.summarize'}
        needs_personal=(personal_only or action=='ai.statistics.explain' or action=='expansion.analyses.run'
            and (body.get('consent') is True or body.get('include_memory') is True))
        settings_actions={'preferences.update','member.memory.controls.update','member.memory.forget',
            'member.ai-profile.save','member.ai-profile.categories.save'}
        pending=self.local.pending()
        settings_pending=any(row['action'] in settings_actions for row in pending)
        needs_fresh_personal=personal and (action=='chat.send' and body.get('share_context') is True
            or needs_personal or action in _AI_CONTEXT_ACTIONS149)
        if settings_pending or needs_fresh_personal and pending:
            if not synchronize:
                return body,_error('local_data_pending','本机还有待同步或冲突的修改，请先在本机数据页面核对。')
            try:self.local.sync_once()
            except Exception:
                return body,_error('local_data_pending','本机设置和修改已保存，联网同步后即可继续。')
            remaining=self.local.pending()
            if any(row['action'] in settings_actions for row in remaining) or needs_fresh_personal and remaining:
                return body,_error('local_data_pending','本机还有待同步或冲突的修改，请先在本机数据页面核对。')
            personal=self.local.ai_context_allowed()
        if not personal:
            if action=='chat.send':body['share_context']=False
            elif needs_personal:
                return body,_error('consent_required','请先同步资料授权，或开启生活资料参考后再整理个人趋势。')
        return body,None

    def _online(self,action,body,operation):
        from mobile_ai149_recovery import INFERENCE_ACTIONS,bind_ai_bridge
        client=self.bridge._api._client
        context=client.operation(operation) if hasattr(client,'operation') and operation else nullcontext()
        if action in INFERENCE_ACTIONS or action in _AI_CONTEXT_ACTIONS149:
            owner=_REQUEST_IDENTITY149.get()
            if owner is None:raise ValueError('AI request has no bound account')
            actor,epoch=owner
            if epoch!=self._auth_epoch or actor!=(self.bridge._api._user or {}).get('id'):
                return _error('account_changed','账号已改变，请重新打开当前页面。')
            body,error=self._prepare_ai_context149(action,body)
            if error:return error
            if action == 'chat.send':
                body = {key: value for key, value in body.items() if key != 'decided_record_draft_ids'}
                if self.local is not None:
                    from mobile_record_assistant import recent_decided_record_drafts
                    identity = self.local.identity()
                    if identity.actor_id != actor:
                        return _error('account_changed','账号已改变，请重新打开当前页面。')
                    decided = recent_decided_record_drafts(self.local, identity)
                    if decided:
                        body['decided_record_draft_ids'] = decided
            with bind_ai_bridge(self.bridge,guard=lambda:self._auth_epoch==epoch and (self.bridge._api._user or {}).get('id')==actor) as bound:
                with (bound._api._client.operation(operation) if hasattr(bound._api._client,'operation') else nullcontext()):
                    reply=self._translate(bound.request(action,body))
                    if not reply.get('ok') and reply.get('error',{}).get('code') in {'authentication','not_authenticated'}:
                        self._revoke_online(actor,epoch)
                    return reply
        owner=_REQUEST_IDENTITY149.get()
        from mobile_sync149 import READ_ACTIONS
        if action in READ_ACTIONS or action in _ONLINE_READ_ACTIONS11823 or action in ONBOARDING_ACTIONS or action in RECORD_TASK_ACTIONS11864 or action in {
                'chat.greeting.ensure','files.upload','expansion.meals.update'}:
            if owner is None:return _error('account_changed','账号已改变，请重新打开当前页面。')
            actor,epoch=owner
            guard=lambda:self._auth_epoch==epoch and (self.bridge._api._user or {}).get('id')==actor
            # The request-local client captures the member token and checks the
            # same generation on both sides. Do not retain the shared member
            # lock while a cold read or immutable file creation waits. File
            # uploads still have one serial lane and the original operation ID.
            with bind_ai_bridge(self.bridge,guard=guard) as bound:
                with (bound._api._client.operation(operation) if hasattr(bound._api._client,'operation') and operation else nullcontext()):
                    reply=self._translate(bound.request(action,body))
            if not reply.get('ok') and reply.get('error',{}).get('code') in {'authentication','not_authenticated'}:
                self._revoke_online(actor,epoch)
            return reply
        with context:reply=self._translate(self.bridge.request(action,body))
        if owner and not reply.get('ok') and reply.get('error',{}).get('code') in {'authentication','not_authenticated'}:
            self._revoke_online(*owner)
        return reply

    def _local_action(self,action,body):
        if self.local is None:return _error('feature_unavailable','本机数据服务尚未就绪。')
        allowed={'_fawnsee_actor_id'}
        if action=='local149.resolve':allowed|={'operation_id','decision'}
        if action.startswith(('local149.draft.','local149.draft-attachment.')):allowed|={'key'}
        if action=='local149.draft-attachment.save':allowed|={'attachment'}
        if action=='local149.draft.save':allowed|={'text'}
        if set(body)-allowed:return _error('validation','本机数据请求包含不支持的字段。')
        identity=self.local.identity()
        if _REQUEST_IDENTITY149.get()!=(identity.actor_id,self._auth_epoch):return _error('account_changed','账号已改变，请重新打开页面。')
        if body.get('_fawnsee_actor_id')!=identity.actor_id:return _error('account_changed','账号已改变，请重新打开页面。')
        if action=='local149.status':result=self.local.status()
        elif action=='local149.sync':self._request_sync();result={'scheduled':True,**self.local.status()}
        elif action=='local149.pending':result=self.local.pending()
        elif action=='local149.resolve':result=self.local.resolve(body.get('operation_id'),body.get('decision'));self._request_sync()
        elif action=='local149.clear-cache':result=self.local.clear_cache()
        elif action=='local149.draft.get':result=self.local.draft(body.get('key'))
        elif action.startswith('local149.draft-attachment.'):
            result=self.local.draft_attachment(body.get('key'),body.get('attachment') if action.endswith('.save') else None,discard=action.endswith('.delete'))
        else:result=self.local.draft(body.get('key'),body.get('text'))
        return {'ok':True,'result':result,'_local149':self.local.status()}

    def _read_intents(self):
        try:
            if self._intent_path.is_file() and self._intent_path.stat().st_size <= 2*1024*1024:
                value = json.loads(self._intent_path.read_text(encoding='utf8'))
                if isinstance(value, dict):
                    return {k:v for k,v in value.items() if isinstance(k,str) and isinstance(v,dict)
                        and {'digest','action','status'}<=set(v)<= {'digest','action','status','attempt','actor'}
                        and ('actor'not in v or type(v['actor'])is int and v['actor']>0)}
        except (OSError, ValueError):
            pass
        return {}

    def _save_intents(self):
        # Persist only UUID/hash/action/status, never private input or tokens.
        if len(self._intents)>512:
            removable=[key for key,value in self._intents.items() if value.get('status')!='uncertain']
            for key in removable[:len(self._intents)-512]:self._intents.pop(key,None)
        temporary = self._intent_path.with_suffix('.tmp')
        temporary.write_text(_encode(self._intents), encoding='utf8')
        os.replace(temporary, self._intent_path)

    def _translate(self, value):
        if not isinstance(value, dict):
            return _error('protocol', '服务响应无法读取，请重新打开页面。')
        if value.get('ok'):
            return {'ok': True, 'result': value.get('data')}
        error = value.get('error') or {}
        return _error(str(error.get('code') or 'request_failed'), str(error.get('message') or '操作未完成，请保留输入后重试。'))

    def handle(self,action,params):
        if self.guest and self.guest.pending and action not in {'app.bootstrap','bootstrap','auth.state','guest.cleanup.retry'}:
            return _error('guest_cleanup_pending','请先继续完成体验资料清理。')
        if self._erasure_pending is not None and action not in {'app.bootstrap','bootstrap','auth.state','account.erase'}:
            return _error('account_erasure_pending','账号注销尚未确认，请先继续完成注销。')
        if action in {'account.reset','account.reset.reauthorize','account.reset.phone.send'}:
            if self._erasure_pending is not None:return _error('account_erasure_pending','请先继续完成账号注销。')
            if self._account_reset is None:return _error('configuration','本机安全存储暂不可用。')
            with self._auth_lock:owner=((self.bridge._api._user or {}).get('id'),self._auth_epoch)
            token=_REQUEST_IDENTITY149.set(owner)
            try:
                if action.endswith('.phone.send'):return self._account_reset.send_phone_code(params)
                return(self._account_reset.reauthorize(params)if action.endswith('.reauthorize')else self._account_reset.run(params))
            finally:_REQUEST_IDENTITY149.reset(token)
        auth_actions={'auth.login','auth.register','auth.logout','auth.phone.send','auth.phone.login','auth.phone.entry','auth.phone.quick-entry',
            'auth.register.complete','auth.register.verified','auth.phone.bind','account.erase','guest.request','guest.cleanup.retry'}
        if (action not in auth_actions and(self.bridge._api._user or{}).get('id')and not(self._account_reset and self._account_reset.pending)
                and not(self.guest and(self.guest.pending or self.guest.facade))):
            try:
                checked=self._verify_account_generation()
                if checked['generation_changed']and action not in {'app.bootstrap','bootstrap','auth.state'}:
                    return _error('account_data_reset','旧账号资料已失效并已清理，请重新载入云端资料。')
            except Exception as error:
                if action in {'app.bootstrap','bootstrap','auth.state'}:return self._verification_bootstrap()
                return _error(getattr(error,'code','account_verification_required'),str(error))
        with self._auth_lock:
            if self._account_reset and self._account_reset.pending:
                if action in {'app.bootstrap','bootstrap','auth.state'}:
                    if params:return _error('validation','启动请求不接受参数。')
                    return self._account_reset.bootstrap()
                code='local_reset_failed'if self._account_reset.pending['phase']=='cloud_confirmed'else'account_reset_pending'
                return _error(code,'请先继续完成当前账号的数据清空。')
            actor=(self.bridge._api._user or {}).get('id')
            owner=(actor,self._auth_epoch)
            track_active=type(actor)is int and actor>0 and action not in auth_actions
            if track_active:
                with self._active_condition:self._active_requests[actor]=self._active_requests.get(actor,0)+1
        token=_REQUEST_IDENTITY149.set(owner)
        try:
            result=self._handle(action,params)
            if result.get('ok')and action in {'app.bootstrap','bootstrap','auth.state'}and isinstance(result.get('result'),dict):
                state=self._account_generation.state if self._account_generation is not None else None
                if state is not None and not self._account_generation.required:result['result']['data_generation']=state['data_generation']
            if (result.get('ok') and action in {'member.request','request'} and isinstance(params,dict)
                    and params.get('action') in _REMINDER_REFRESH_ACTIONS11828 and self._native_hooks is not None):
                self._native_hooks.remindersChanged()
            return result
        except Exception as error:
            code=getattr(error,'code',None)
            if isinstance(code,str) and (code in {'not_authenticated','validation','conflict','not_found','profile_sync_pending'} or code.startswith(('local_','sync_','offline_','account_','dependency_'))):
                return _error(code,str(error))
            raise
        finally:
            _REQUEST_IDENTITY149.reset(token)
            if track_active:
                with self._active_condition:
                    count=self._active_requests.get(actor,0)-1
                    if count>0:self._active_requests[actor]=count
                    else:self._active_requests.pop(actor,None)
                    self._active_condition.notify_all()

    def _begin_login(self):
        self._finished.clear();self._finished_actors.clear()
        with self._auth_lock:
            if self._account_reset and self._account_reset.pending:
                from mobile_account_reset11831 import ResetError
                raise ResetError('account_reset_pending','请先继续完成当前账号的数据清空。')
            self._auth_epoch+=1
            if self._account_generation is not None:self._account_generation.suspend()
            if self.session is not None:self.session.suspend()
            self.bridge._api._user=None;self.bridge._api._reset_services()
            if self.bridge._api._client is not None:self.bridge._api._client.clear_session(clear_recovery=True)
            if self._native_hooks is not None:self._native_hooks.deactivate()

    def _complete_login(self,reply,auto_login):
        if reply.get('ok') and self.session is not None:
            if self.guest and self.guest.facade:
                try:
                    self.session.remember(self.bridge._api._client,self.bridge._api._user,auto_login=False)
                    self.guest.begin(dict(self.session.value),reply,auto_login)
                    self.guest.cleanup(self._native_hooks)
                except Exception:return _error('guest_cleanup_pending','登录已验证，体验资料清理尚未完成。请点击继续完成清理。')
            return self._finalize_member_login(reply,auto_login)
        return reply

    def _purge_erased_account(self):
        from mobile_local149 import Identity
        from mobile_client_reset import clear_voice_files
        pending=self._erasure_pending
        identity=Identity(pending['actor'],pending['endpoint'],'erasure-confirmed')
        if self.local is not None:
            with self.local._sync_lock:self.local.store.purge_account(identity)
        self._avatars.delete(pending['actor']);clear_voice_files(self.root)
        if self.session is not None:self.session.deactivate()
        with self._intent_lock:self._intents.clear();self._save_intents()
        if self._native_hooks is not None:self._native_hooks.clearAccountCaches()
        self._vault.clearActorKey(pending['actor'])
        self._erasure_journal.clear();self._erasure_pending=None
        self._finished.clear();self._finished_actors.clear();self.bridge._generation+=1
        self.bridge._api._user=None;self.bridge._api._reset_services()
        if self.bridge._api._client is not None:self.bridge._api._client.clear_session(clear_recovery=True)

    def _run_erasure(self,password):
        with self._erasure_lock:
            pending=self._erasure_pending
            if pending is None:return _error('not_authenticated','请先登录要注销的账号。')
            with self._auth_lock:
                self._auth_epoch+=1
                if self.session is not None:self.session.suspend()
                self.bridge._api._user=None;self.bridge._api._reset_services()
                if self._native_hooks is not None:self._native_hooks.deactivate()
            if pending['phase']!='cloud_confirmed':
                try:
                    self.bridge._api._ensure_client();client=self.bridge._api._client
                    with client._lock:
                        client._token=pending['bearer']
                        result=client.request('POST','/v1/auth/account/erase',json={
                            'current_password':password,'confirmation':'删除账号及全部数据','request_id':pending['request_id']})
                    if result!={'erased':True,'local_purge_required':True}:raise ValueError('Unconfirmed erasure')
                    pending={**pending,'phase':'cloud_confirmed'}
                    self._erasure_journal.save(pending);self._erasure_pending=pending
                except Exception:return _error('account_erasure_pending','注销结果尚未确认，请输入原账号密码继续确认。')
            try:self._purge_erased_account()
            except Exception:return _error('local_erasure_failed','云端已确认注销，本机清理尚未完成，请继续完成清理。')
            return {'ok':True,'result':{'erased':True,'local_purged':True}}

    @staticmethod
    def _phone_body(action,params):
        required={'auth.phone.send':{'phone','purpose'},
            'auth.phone.login':{'phone','challenge_id','code'},
            'auth.phone.entry':{'phone','challenge_id','code'},
            'auth.phone.quick-entry':{'phone','challenge_id','code'},
            'auth.credentials.get':set(),
            'auth.credentials.complete':{'username','password','request_id'},
            'auth.register.verified':{'username','password','registration_token'},
            'auth.register.complete':{'username','password','phone','challenge_id','code'},
            'auth.phone.get':set(),
            'auth.phone.bind':{'phone','challenge_id','code','current_password'}}[action]
        body=dict(params);auto_login=body.pop('auto_login',True)
        if type(auto_login) is not bool or ('auto_login' in params and action not in {'auth.phone.login','auth.phone.entry','auth.phone.quick-entry','auth.register.complete','auth.register.verified'}):
            raise ValueError('自动登录设置无效。')
        optional={'current_challenge_id'} if action=='auth.phone.send' else set()
        if not required<=set(body) or set(body)-required-optional or any(type(value) is not str for value in body.values()):
            raise ValueError('请完整填写手机号与验证码。')
        if 'phone' in body and not re.fullmatch(r'(?:\+86)?1[3-9][0-9]{9}',body['phone']):raise ValueError('请输入有效的中国大陆手机号。')
        if 'purpose' in body and body['purpose'] not in {'register','login','bind','change'}:raise ValueError('验证码用途无效。')
        for key in ('challenge_id','current_challenge_id'):
            if key in body and not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',body[key]):raise ValueError('请重新获取验证码。')
        if 'code' in body and not re.fullmatch(r'[0-9]{6}',body['code']):raise ValueError('请输入六位短信验证码。')
        if 'username' in body and not 1<=len(body['username'])<=64:raise ValueError('账号长度不符合要求。')
        if 'registration_token' in body and (not 50<=len(body['registration_token'])<=512 or re.fullmatch(r'pr1_[A-Za-z0-9_-]+\.[a-f0-9]{64}',body['registration_token'])is None):raise ValueError('手机号验证已失效，请重新验证手机号。')
        if action in {'auth.register.verified','auth.credentials.complete'} and not 8<=len(body['password'])<=256:raise ValueError('请设置8至256位密码。')
        if action=='auth.credentials.complete':
            try:
                if str(UUID(body['request_id']))!=body['request_id']:raise ValueError
            except ValueError:raise ValueError('保存操作编号无效，请重新打开设置。') from None
        for key in ('password','current_password'):
            if key in body and not 1<=len(body[key])<=256:raise ValueError('请填写有效密码。')
        # The resend marker belongs to the UI/native contract; the strict
        # server send endpoint accepts only the phone and purpose.
        body.pop('current_challenge_id',None)
        return body,auto_login

    def _handle(self, action, params):
        if type(params) is not dict:
            return _error('validation', '请求参数格式不正确。')
        if action=='guest.request':return self._guest_request(params,_GUEST_PROGRESS11614.get())
        if action=='guest.cleanup.retry':
            if params:return _error('validation','清理请求不接受参数。')
            return self._resume_guest_cleanup()
        if self.guest and self.guest.facade and action in {'member.request','request','account.erase','avatar.local.get','avatar.local.save','member.capabilities','getcapabilities'}:
            return _error('login_required','此功能需要登录后使用。')
        if action in {'record.photo.stage','record.photo.get','record.photo.discard'}:
            actor=(self.bridge._api._user or {}).get('id')
            if (self.local is None or type(actor)is not int or actor<=0
                    or (self.bridge._api._user or {}).get('role_code')!='member'):
                return _error('login_required','请先登录后记录照片。')
            allowed={'_fawnsee_actor_id','image'if action.endswith('.stage')else'file_id'}
            if set(params)!=allowed:return _error('validation','照片请求包含不受支持的字段。')
            if type(params['_fawnsee_actor_id'])is not int or params['_fawnsee_actor_id']!=actor or _REQUEST_IDENTITY149.get()!=(actor,self._auth_epoch):
                return _error('account_changed','账号已改变，请重新打开记录页面。')
            try:
                from mobile_record_photos11839 import stage_record_photo,get_record_photo,discard_record_photo
                identity=self.local.identity()
                if identity.actor_id!=actor:return _error('account_changed','账号已改变，请重新打开记录页面。')
                handler=stage_record_photo if action.endswith('.stage')else get_record_photo if action.endswith('.get')else discard_record_photo
                result=handler(self.local,identity,params.get('image')if action.endswith('.stage')else params['file_id'])
                self.local._guard(identity)
                return {'ok':True,'result':result}
            except Exception as error:return self._translate(self.bridge._api._failure(error))
        if action in {'avatar.local.get', 'avatar.local.save'}:
            current = self.bridge._api._user or {}
            actor = current.get('id')
            if type(actor) is not int or actor <= 0 or current.get('role_code') != 'member':
                return _error('not_authenticated', '请先登录会员账号。')
            if type(params.get('_fawnsee_actor_id')) is not int or params['_fawnsee_actor_id'] != actor:
                return _error('account_changed', '账号已变更，本次头像未写入当前账号。')
            allowed = {'_fawnsee_actor_id'} if action.endswith('.get') else {'_fawnsee_actor_id', 'avatar_data', 'sync_pending'}
            if set(params) != allowed:
                return _error('validation', '头像请求包含不受支持的字段。')
            try:
                identity=self.local.identity() if self.local is not None else None
                if identity is not None and (identity.actor_id!=actor or _REQUEST_IDENTITY149.get()!=(actor,self._auth_epoch)):
                    return _error('account_changed','账号已改变，请重新打开页面。')
                result = self._avatars.get(actor) if action.endswith('.get') else self._avatars.save(actor, params['avatar_data'], params['sync_pending'])
                if identity is not None:self.local._guard(identity)
                return {'ok': True, 'result': result}
            except ValueError as error:
                return _error('validation', str(error))
            except OSError:
                return _error('storage', '头像未能保存在此设备，请稍后重试。')
        if action in {'app.bootstrap', 'bootstrap', 'auth.state'}:
            if not params and self._erasure_pending is None:
                guest=self._guest_bootstrap()
                if guest is not None:return guest
            if params:
                return _error('validation', '启动请求不接受连接地址或文件路径。')
            if self._erasure_pending is not None:
                self.bridge._api._user=None
                result=self.bridge._api._bootstrap_data()
                result.update(account_erasure_pending=True,
                    account_erasure_local_pending=self._erasure_pending['phase']=='cloud_confirmed')
                return {'ok':True,'result':result}
            if self.session is not None:
                current=self.session.user()
                if current is not None:
                    self.bridge._api._user=current
                    return {'ok':True,'result':self.bridge._api._bootstrap_data()}
                if self.session.value is not None:
                    self.bridge._api._user=None
                    if self.bridge._api._client is not None:self.bridge._api._client.clear_session(clear_recovery=False)
                    return {'ok':True,'result':self.bridge._api._bootstrap_data()}
            return self._translate(self.bridge.bootstrap())
        if action=='account.erase':
            if (set(params)-{'current_password','confirmation','request_id'} or not {'current_password','confirmation'}<=set(params)
                    or type(params['current_password']) is not str or not 1<=len(params['current_password'])<=256
                    or params['confirmation']!='删除账号及全部数据'):
                return _error('validation','请填写当前密码并确认删除账号及全部数据。')
            try:
                request_id=params.get('request_id')
                if request_id is not None and str(UUID(request_id))!=request_id:raise ValueError
                with self._erasure_lock:
                    if self._erasure_journal is None:return _error('configuration','本机安全存储暂不可用。')
                    with self._auth_lock:
                        if self._account_reset and self._account_reset.pending:
                            return _error('account_reset_pending','请先继续完成当前账号的数据清空。')
                        if self._erasure_pending is None:
                            actor=(self.bridge._api._user or {}).get('id');client=self.bridge._api._client
                            if type(actor) is not int or actor<=0 or client is None or not client._token:return _error('not_authenticated','请先登录要注销的账号。')
                            pending={'actor':actor,'endpoint':self.bridge._api._base_url.rstrip('/'),
                                'request_id':request_id or str(uuid4()),'bearer':client._token,'phase':'submitted'}
                            self._erasure_journal.save(pending);self._erasure_pending=pending
                        elif request_id is not None and request_id!=self._erasure_pending['request_id']:
                            return _error('account_erasure_pending','请继续此前的注销，不要重新创建操作。')
                    return self._run_erasure(params['current_password'])
            except (ValueError,TypeError,AttributeError):return _error('validation','注销操作编号无效。')
        if action in {'auth.login', 'auth.register'}:
            auto_login=params.get('auto_login',not self.guest_enabled)
            if (type(auto_login) is not bool or set(params)-{'username','password','auto_login'}
                    or not {'username','password'}<=set(params) or any(type(params[k]) is not str for k in ('username','password'))):
                return _error('validation', '请填写账号和密码。')
            if not 1 <= len(params['username']) <= 64 or not 1 <= len(params['password']) <= 1024:
                return _error('validation', '账号或密码长度不符合要求。')
            self._begin_login()
            if action=='auth.register':
                registered=self._translate(self.bridge.register(params['username'],params['password']))
                if not registered.get('ok'):return registered
                # Legacy staged registration returns a user only. Obtain the
                # authenticated envelope using the same server's password route;
                # a full server may reject registration and is never bypassed.
            reply=self._translate(self.bridge.login(params['username'],params['password']))
            if action=='auth.register' and not reply.get('ok'):
                return _error('registration_login_required','账号已创建，请使用账号密码登录。')
            return self._complete_login(reply,auto_login)
        if action in {'auth.credentials.get','auth.credentials.complete'}:
            try:body,_=self._phone_body(action,params)
            except ValueError as error:return _error('validation',str(error))
            try:
                with self._auth_lock:
                    actor=(self.bridge._api._user or {}).get('id');epoch=self._auth_epoch
                    if type(actor)is not int or actor<=0 or _REQUEST_IDENTITY149.get()!=(actor,epoch):return _error('not_authenticated','请先登录会员账号。')
                    captured=self.bridge.capture_credentials(action)
                # Neither the app nor bridge-wide lock covers network latency.
                # The client pins this I/O to the captured bearer and validates
                # it again before a short, separately guarded session commit.
                value=self.bridge.fetch_credentials(action,body,captured)
                with self._auth_lock:
                    if (epoch!=self._auth_epoch or (self.bridge._api._user or {}).get('id')!=actor
                            or self._erasure_pending is not None or self._account_reset and self._account_reset.pending
                            or self.guest and self.guest.pending):return _error('account_changed','账号状态已改变，请重新打开当前页面。')
                    reply=self._translate(self.bridge.commit_credentials(action,value,captured))
                    if reply.get('ok') and action=='auth.credentials.complete' and self.session is not None:
                        self.session.remember(self.bridge._api._client,self.bridge._api._user,auto_login=bool(self.session.persistent),preserve_other_accounts=True)
                        self._attach_payload_store();self._request_sync()
                    return reply
            except Exception as error:return self._translate(self.bridge._api._failure(error))
        if action in {'auth.phone.send','auth.register.complete','auth.register.verified','auth.phone.login','auth.phone.entry','auth.phone.quick-entry','auth.phone.get','auth.phone.bind'}:
            if self.guest_enabled and action in {'auth.register.complete','auth.register.verified','auth.phone.login','auth.phone.entry','auth.phone.quick-entry'}:params={**params,'auto_login':params.get('auto_login',False)}
            try:body,auto_login=self._phone_body(action,params)
            except ValueError as error:return _error('validation',str(error))
            login=action in {'auth.register.complete','auth.register.verified','auth.phone.login','auth.phone.entry','auth.phone.quick-entry'}
            if login:self._begin_login()
            reply=self._translate(self.bridge.phone_auth(action,body))
            if action in {'auth.phone.entry','auth.phone.quick-entry'}:
                return self._complete_login(reply,auto_login) if reply.get('ok') and reply.get('result',{}).get('outcome')=='login' else reply
            return self._complete_login(reply,auto_login) if login else reply
        if action == 'auth.logout':
            if params:
                return _error('validation', '退出请求参数无效。')
            self._finished.clear();self._finished_actors.clear()
            with self._auth_lock:
                if self._account_reset and self._account_reset.pending:
                    return _error('account_reset_pending','请先继续完成当前账号的数据清空。')
                self._auth_epoch+=1
                if self._account_generation is not None:self._account_generation.suspend()
                if self.session is not None:self.session.deactivate()
                if self._native_hooks is not None:self._native_hooks.deactivate()
            reply=self._translate(self.bridge.logout())
            if self.bridge._api._client is not None:self.bridge._api._client.clear_session(clear_recovery=True)
            if self.guest:self.guest.activate()
            return reply
        if action in {'member.capabilities', 'getcapabilities'}:
            return self._translate(self.bridge.request('member.capabilities', {}))
        if action not in {'member.request', 'request'}:
            return _error('permission', '此操作未向会员应用开放。')
        if set(params) - {'action', 'payload', 'request_id'}:
            return _error('validation', '请求中有不受支持的字段。')
        member_action = params.get('action')
        body = params.get('payload') or {}
        if member_action not in MOBILE_ACTIONS or type(body) is not dict:
            return _error('permission', '此功能暂未接入手机端。')
        if member_action in LOCAL149_ACTIONS:return self._local_action(member_action,body)
        if member_action in {'ai.operations.status','ai.operations.cancel'}:
            from mobile_ai149_recovery import query_operation,cancel_operation,INFERENCE_ACTIONS,bind_ai_bridge
            if set(body)-{'operation_id','kind','_fawnsee_actor_id'} or body.get('kind') not in INFERENCE_ACTIONS:
                return _error('validation','AI状态查询参数无效。')
            owner=_REQUEST_IDENTITY149.get()
            if owner is None or body.get('_fawnsee_actor_id')!=owner[0]:return _error('account_changed','账号已改变。')
            identifier=str(UUID(body.get('operation_id')))
            key=hashlib.sha256(_encode([owner[0],identifier]).encode()).hexdigest()
            with self._intent_lock:journal=dict(self._intents.get(key) or {})
            if journal and journal.get('action')!=body['kind']:return _error('conflict','AI操作类别不一致。')
            if journal.get('status')=='rejected_result':
                return {'ok':True,'result':{'operation_id':identifier,'kind':body['kind'],'state':'failed','retry_safe':True}}
            submitted=(journal.get('attempt') or {}).get('submitted') or []
            child=submitted[-1].get('operation_id') if submitted else None
            with bind_ai_bridge(self.bridge,guard=lambda:self._auth_epoch==owner[1] and (self.bridge._api._user or {}).get('id')==owner[0]) as bound:
                result=(query_operation if member_action.endswith('status') else cancel_operation)(bound._api._client,identifier,body['kind'])
                if child and child!=identifier:
                    child_result=(query_operation if member_action.endswith('status') else cancel_operation)(bound._api._client,child,body['kind'])
                    if child_result['state']=='running':
                        result={**result,'state':'running','retry_safe':False}
                    elif member_action.endswith('cancel') and child_result['state'] in {'cancelled','failed','expired'} and result['state']=='not_started':
                        result={**result,'state':'cancelled','retry_safe':True}
            return {'ok':True,'result':result}
        if member_action == 'expansion.analyses.create' and body.get('kind') != 'nutrition_check':
            return _error('permission', '手机端仅开放当前填写内容的饮食教育整理。')
        if any(key in body for key in {'user_id', 'actor_user_id', 'member_user_id', 'actor_id', 'api_key', 'base_url', 'path'}):
            return _error('permission', '请求不能指定其他会员或本机路径。')
        try:
            operation = str(UUID(params.get('request_id') or body.get('request_id') or str(uuid4())))
        except (ValueError, TypeError, AttributeError):
            return _error('validation', '操作编号无效。')
        body = dict(body)
        if (member_action == 'member.ai-profile.save' or member_action in CATEGORY_WRITES or member_action in {'chat.tools.confirm','chat.tools.cancel'}) and body.get('request_id') != operation:
            return _error('validation', '档案保存操作编号不一致，请重新核对。')
        if 'request_id' in body and not member_action.startswith('expansion.') and member_action not in {'member.ai-profile.save','forum.detail.comment'} and member_action not in CATEGORY_WRITES and member_action not in {'chat.tools.confirm','chat.tools.cancel'}|RECORD_DECISIONS:
            body.pop('request_id')
        actor = (self.bridge._api._user or {}).get('id')
        if self.session is not None and self.session.user() is not None:
            actor=self.session.user()['id']
        if type(actor) is not int or actor<=0:return _error('not_authenticated','请先登录会员账号。')
        owner=_REQUEST_IDENTITY149.get()
        if owner!=(actor,self._auth_epoch):return _error('account_changed','账号已改变，请重新打开当前页面。')
        captured_local=self.local.identity() if self.local is not None else None
        if self.local is not None and body.get('_fawnsee_actor_id')!=actor:
            return _error('account_changed','账号已改变，请重新打开当前页面。')
        if member_action in RECORD_DECISIONS:
            if captured_local is None:return _error('local_not_ready','请在新版手机应用登录后核对记录。')
            from mobile_record_assistant import decide
            return decide(self.local,captured_local,self._online,member_action,body,operation)
        digest = hashlib.sha256(_encode([actor, member_action, body]).encode()).hexdigest()
        intent_key = hashlib.sha256(_encode([actor, operation]).encode()).hexdigest()
        with self._intent_lock:known=dict(self._intents[intent_key]) if intent_key in self._intents else None
        if known and known['digest'] != digest:
            return _error('conflict', '同一操作编号不能用于不同内容，请重新核对。')
        with self._intent_lock:finished=self._finished.get(intent_key)
        if finished is not None:
            if owner!=(actor,self._auth_epoch):return _error('account_changed','账号已改变。')
            return finished
        from mobile_ai149_recovery import INFERENCE_ACTIONS,inference_scope,query_operation
        self.bridge._api._ensure_client()
        client = self.bridge._api._client
        inference=member_action in INFERENCE_ACTIONS
        if inference and known and known.get('status')=='rejected_result':
            return _error('ai_attempt_finished','上次结果未通过格式核对，请重新发送；原话已保留。')
        mutation=bool(re.search(r'\.(add|create|update|delete|toggle|checkin|uncheckin|close|set|checkout|interested|confirm|send|save|like|comment|upload|activate|request)$',member_action))
        if self.local is not None and member_action=='member.sync':
            status=self.local.status()
            # Larger pages are only advertised after both personal resources
            # are on-device. A cold online fallback retains the cloud's limit.
            ready=all(self.local._resource(captured_local,name) is not None for name in ('life_records','observations'))
            return {'ok':True,'result':{'revision':hashlib.sha256(_encode(status).encode()).hexdigest(),
                'local_record_page_limit':1000 if ready else 100},'_local149':status}
        if inference and self.local is not None:
            identity=self.local.identity()
            saved=self.local.store.get(identity,'meta','ai-result:'+operation)
            if saved is not None:
                self.local._guard(identity)
                if owner!=(actor,self._auth_epoch):return _error('account_changed','账号已改变。')
                if member_action=='chat.send':
                    self.local.observe(member_action,body,saved,identity=identity)
                    self.local._guard(identity)
                return saved
        if known and known['status']=='uncertain' and inference:
            attempt=known.get('attempt') or {};submitted=attempt.get('submitted') or []
            lookup=submitted[-1]['operation_id'] if submitted else operation
            try:
                from mobile_ai149_recovery import bind_ai_bridge
                with bind_ai_bridge(self.bridge,guard=lambda:self._auth_epoch==owner[1]) as bound:
                    receipt=query_operation(bound._api._client,lookup,member_action)
            except Exception:return _error('outcome_uncertain','正在核对上次 AI 结果，请稍后点击重试；不会重复提交。')
            if receipt['state']=='running':return _error('ai_in_progress','上次 AI 仍在处理中，请稍后查看当前对话。')
            legacy_rejected=(member_action=='chat.send' and not attempt and
                bool(set(body)&{'thinking_enabled','reasoning_effort','web_search','tools_enabled'}))
            if not submitted and receipt['state']=='not_started' and not legacy_rejected and (not attempt or attempt.get('phase')!='preflight'):
                return _error('outcome_uncertain','上次结果仍需核对，请查看记录后重新发起本次操作。')
            if receipt['state'] in {'failed','cancelled','expired'}:
                return _error('ai_attempt_finished','上次 AI 已停止，已保留记录；请重新发送或重新识别。')
            if receipt['state']=='complete' and member_action=='chat.send':
                from mobile_ai149_recovery import bind_ai_bridge,restore_chat_completion
                owner=_REQUEST_IDENTITY149.get()
                with bind_ai_bridge(self.bridge,guard=lambda:self._auth_epoch==owner[1]) as bound:
                    restored=restore_chat_completion(bound._api,operation,body,receipt)
                if restored is not None:
                    result={'ok':True,'result':restored}
                    if self.local is not None:
                        self.local._guard(captured_local)
                        self.local.observe(member_action,body,result,identity=captured_local)
                        self.local._guard(captured_local)
                        self.local.store.put(captured_local,'meta','ai-result:'+operation,result)
                        self.local._guard(captured_local)
                    self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'complete'})
                    return result
        if inference:
            self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'uncertain','attempt':{'phase':'preflight','submitted':[]}})
        from mobile_ai139 import chat_request_scope
        chat_scope=chat_request_scope(self.bridge._api,operation) if member_action=='chat.send' and type(actor) is int and actor>0 else nullcontext()
        def checkpoint(value):self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'uncertain','attempt':value})
        attempt_context=inference_scope(operation,member_action,checkpoint) if inference else nullcontext()
        with chat_scope,attempt_context as attempt:
            if self.local is not None:
                if member_action=='voice.records.confirm':
                    from mobile_providers import validate_record_candidates
                    if set(body)-{'records','operation_id','_fawnsee_actor_id'} or body.get('operation_id')!=operation:
                        return _error('validation','本次语音记录编号不一致，请重新核对。')
                    rows=validate_record_candidates(body.get('records'));saved=[]
                    for index,row in enumerate(rows):
                        identifier=str(uuid5(UUID(operation),'life-record:'+str(index)))
                        action='expansion.observations.create' if row['category']=='metric' else 'health.records.add'
                        payload=({**row['details'],'measured_at':row['occurred_at']} if row['category']=='metric' else
                            {key:row[key] for key in ('category','occurred_at','content','details')}|{'source':'ai_confirmed'})
                        response=self.local.handle(action,{**payload,'_fawnsee_actor_id':actor},identifier)
                        if not response.get('ok'):return response
                        value=response['result'];saved.append({**row,'id':value.get('id') if isinstance(value,dict) else value})
                    result={'ok':True,'result':{'complete':True,'saved':saved,'remaining':[],'operation_id':operation},'_local149':self.local.status()}
                else:result=self.local.handle(member_action,body,operation)
            else:result=self._online(member_action,body,operation)
        if member_action=='chat.messages.list' and captured_local is not None:
            from mobile_record_assistant import annotate_history
            return annotate_history(self.local,captured_local,result)
        if member_action in READING_READS or member_action in CATEGORY_READS or member_action in {'chat.tools.proposals','chat.messages.list'}:
            # Reading assets and live category-job polling have their own
            # bounded views. Do not retain them as mutation receipts or write
            # an operation journal for every scrolled image or status poll.
            return result
        partial = result.get('ok') and isinstance(result.get('result'), dict) and result['result'].get('complete') is False
        if result.get('ok') and not partial:
            if owner!=(actor,self._auth_epoch):return _error('account_changed','账号已改变，本次结果仍保留在原账号。')
            if (inference or mutation) and member_action!='chat.send' and len(_encode(result).encode())<=128000:
                with self._intent_lock:
                    self._finished[intent_key]=result;self._finished_actors[intent_key]=actor
            if inference and self.local is not None:
                self.local._guard(captured_local)
                self.local.store.put(captured_local,'meta','ai-result:'+operation,result)
                self.local._guard(captured_local)
            if inference or mutation:self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'complete'})
            with self._intent_lock:
                if len(self._finished)>128:
                    removed=next(iter(self._finished));self._finished.pop(removed);self._finished_actors.pop(removed,None)
        elif inference and not attempt.has_submitted and attempt.snapshot().get('requires_new_attempt') is not True:
            self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'retry_same_id'})
        elif (inference and attempt.snapshot().get('phase')=='received' and not result.get('ok')
                and result.get('error',{}).get('code') in {'invalid_response','validation'}):
            # The paid result is known, but unusable. An explicit new user
            # attempt may regenerate; never silently repay or replay bad JSON.
            self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'rejected_result','attempt':attempt.snapshot()})
        elif inference and attempt.snapshot().get('requires_new_attempt') is True and not result.get('ok'):
            self._set_intent(intent_key,{'digest':digest,'action':member_action,'status':'rejected_result','attempt':attempt.snapshot()})
        return result


def initialize(private_dir,vault=None,native_hooks=None):
    global _APP
    with _LOCK:
        try:
            if not isinstance(private_dir, str) or not private_dir or len(private_dir) > 4096:
                return _encode(_error('configuration', '应用私有存储不可用。'))
            root = Path(private_dir).resolve()
            if root.is_symlink():
                return _encode(_error('configuration', '应用私有存储无效。'))
            if _APP is None:
                _APP = MobileApplication(root,vault=vault,native_hooks=native_hooks,guest_enabled=vault is not None)
            elif _APP.root != root:
                return _encode(_error('configuration', '应用存储位置已改变，请重新启动。'))
            return _encode({'ok': True, 'result': {'name': 'FAWNSEE-MOBILEv1.0.0', 'cloud_configured': True}})
        except Exception:
            return _encode(_error('configuration', '应用初始化失败，请重新打开。'))


def cancel_chat(operation_id):
    """Native control executor only; deliberately never waits on dispatch's lock."""
    from mobile_ai139 import cancel_request
    application=_APP
    stopped=(application.guest.facade.cancel(operation_id)if application and application.guest and application.guest.facade else application is not None and cancel_request(application.bridge._api,operation_id))
    return _encode({'ok':True,'result':{'requested':bool(stopped),'cancelled':bool(stopped)}})


def dispatch(request_json, progress=None):
    request_id=None
    try:
        if not isinstance(request_json,str) or len(request_json.encode('utf8'))>_MAX_REQUEST_BYTES:raise ValueError
        request=json.loads(request_json,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(request) is not dict or set(request)!={'id','action','params'}:raise ValueError
        request_id=request['id']
        if type(request_id) not in {str,int} or isinstance(request_id,str) and len(request_id)>100 or not isinstance(request['action'],str):raise ValueError
        with _LOCK:application=_APP
        if application is None:return _encode({'id':request_id,**_error('not_initialized','请重新打开应用。')})
        from mobile_activity import progress_scope
        token=_GUEST_PROGRESS11614.set(progress)
        try:
            with progress_scope(progress):return _encode({'id':request_id,**application.handle(request['action'],request['params'])})
        finally:_GUEST_PROGRESS11614.reset(token)
    except (ValueError,TypeError):return _encode({'id':request_id,**_error('validation','请求格式不正确。')})
    except Exception:return _encode({'id':request_id,**_error('request_failed','操作未完成，请保留输入后重试。')})


def on_foreground():
    application=_APP
    if application is None or application.local is None:return _encode(_error('not_authenticated','请先登录。'))
    try:
        application._verify_account_generation()
        status=application.local.status();application._request_sync()
        if application._native_hooks is not None:application._native_hooks.remindersChanged()
        return _encode({'ok':True,'result':status})
    except Exception as error:return _encode(_error(getattr(error,'code','local_storage'),'本机数据暂时无法读取，请重新登录后重试。'))


def sync_background():
    application=_APP
    if application is None or application.local is None:return _encode(_error('not_authenticated','请先登录。'))
    try:
        application._verify_account_generation()
        result=application.local.sync_once()
        # Keep draining a large offline queue without waiting for the periodic
        # job. A retry reuses frozen IDs; conflicts stay for the user's choice.
        ready=[row for row in application.local.pending() if row['status'] in {'queued','uncertain'}]
        if ready and result.get('applied',0)>=50:result['more_pending']=True
        if application._native_hooks is not None:application._native_hooks.remindersChanged()
        return _encode({'ok':True,'result':result})
    except Exception as error:
        code=getattr(error,'code','network')
        return _encode(_error(code if isinstance(code,str) else 'network','本机修改已保留，稍后继续同步。'))


def _local_reminder_rows():
    application=_APP
    if application is None or application.local is None:return None,[]
    guest=getattr(application,'guest',None)
    if guest and (guest.pending or guest.facade):return None,[]
    application._verify_account_generation()
    identity=application.local.identity()
    resources=application.local._project(identity,{'reminders','goals'})
    value=resources.get('reminders',[])
    rows=value if isinstance(value,list) else value.get('items',[])
    application.local._guard(identity)
    return identity,rows


def local_reminders(slots_json='[]'):
    try:
        from mobile_reminders149 import schedule
        from mobile_routine11828 import checked_slots,scope_for
        identity,rows=_local_reminder_rows()
        if identity is None:return _encode({'ok':True,'result':{'actor_id':0,'scope':'','items':[]}})
        slots=checked_slots(slots_json)
        _APP.local._guard(identity)
        return _encode({'ok':True,'result':{'actor_id':identity.actor_id,'scope':scope_for(identity),'items':schedule(rows)+slots}})
    except Exception:return _encode({'ok':True,'result':{'actor_id':0,'scope':'','items':[]}})


def reminder_fired(actor,identifier,at):
    try:
        from mobile_reminders149 import delivery
        identity,rows=_local_reminder_rows()
        if identity is None or type(actor) is not int or actor!=identity.actor_id:return _encode({'ok':True,'result':{'show':False}})
        title=delivery(rows,str(identifier),int(at))
        return _encode({'ok':True,'result':{'show':bool(title),'actor_id':actor,'title':title or ''}})
    except Exception:return _encode({'ok':True,'result':{'show':False}})


def routine_fired11828(actor,scope,slot_json,now_ms):
    """Native-only generation verification before reading notification facts."""
    empty={'show':False,'actor_id':0,'scope':'','title':'','body':''}
    try:
        from mobile_routine11828 import checked_slot,notification,scope_for
        application=_APP
        if application is None or application.local is None:return _encode({'ok':True,'result':empty})
        guest=getattr(application,'guest',None)
        if guest and (guest.pending or guest.facade):return _encode({'ok':True,'result':empty})
        application._verify_account_generation()
        identity=application.local.identity()
        if type(actor) is not int or actor!=identity.actor_id or scope!=scope_for(identity):return _encode({'ok':True,'result':empty})
        if not isinstance(slot_json,str) or len(slot_json.encode('utf8'))>4096:return _encode({'ok':True,'result':empty})
        slot=checked_slot(json.loads(slot_json))
        records=application.local._project(identity,{'life_records'}).get('life_records',[])
        message=notification(records,slot,now_ms)
        application.local._guard(identity)
        result={**empty,'actor_id':actor,'scope':scope,'show':message is not None}
        if message:result.update(message)
        return _encode({'ok':True,'result':result})
    except Exception:return _encode({'ok':True,'result':empty})


@contextmanager
def _voice_lock(capture_path):
    # _APP publication and auth references are local snapshots; the recording
    # must not queue behind startup or other UI work for an eight-second timeout.
    # Both sides of ASR still validate the captured account and client epoch.
    if not isinstance(capture_path, str) or not Path(capture_path).is_file():
        raise VoiceError('cancelled', '本次语音已取消。')
    yield


def transcribe_voice_file(capture_path, expected_member_id, progress=None):
    """Native-only seam. Snapshot auth, release the UI facade lock for ASR,
    then discard a result if the user logged out or changed accounts meanwhile.
    """
    try:
        with _voice_lock(capture_path):
            application = _APP
            if application is None:
                return _encode(_error('not_authenticated', '请先登录会员账号。'))
            application._verify_account_generation()
            api = application.bridge._api
            current = api._user or {}
            if (type(expected_member_id) is not int or expected_member_id <= 0
                    or current.get('id') != expected_member_id or current.get('role_code') != 'member'):
                return _encode(_error('account_changed', '账号已变更，本次语音已取消。'))
            api._ensure_client()
            client = api._client
            with client._lock:
                actual = getattr(client, "_user", None)
                if isinstance(actual, dict) and actual.get("id") != expected_member_id:
                    return _encode(_error("account_changed", "账号已变更，本次语音已取消。"))
                serial, token, endpoint = client.session_serial, client._token, client.base_url
            if not token:
                return _encode(_error('cloud_unavailable', '语音识别需要联网，请连接网络后重试，也可以输入文字。'))
            private_root = application.root
        try:
            data = read_capture(private_root, capture_path)
            result = upload_capture(endpoint, token, data,
                                    cancelled=lambda: not Path(capture_path).is_file(),
                                    on_partial=(lambda text: progress.onPartial(text)) if progress is not None else None)
            failure = None
        except VoiceError as error:
            if error.code == 'cancelled':
                return _encode(_error('cancelled', '本次语音已取消。'))
            result, failure = None, _error(error.code, str(error))
        finally:
            # Neither credentials nor recordings are persisted by this operation.
            token = None
        with _voice_lock(capture_path):
            current = api._user or {}
            if (_APP is not application or api._client is not client
                    or client.session_serial != serial or current.get('id') != expected_member_id
                    or current.get('role_code') != 'member'):
                return _encode(_error('account_changed', '账号已变更，本次语音已取消。'))
            return _encode(failure or {'ok': True, 'result': result})
    except VoiceError as error:
        return _encode(_error(error.code, str(error)))
    except Exception:
        return _encode(_error('cloud_unavailable', '语音服务暂时不可用，请稍后重新录入。'))


def native_scope11614():
    application=_APP
    return _encode(application.native_scope()if application else{'kind':'unavailable'})

def transcribe_guest_voice_file(capture_path,scope_id,session_epoch,progress=None):
    application=_APP
    facade=application.guest.facade if application and application.guest and not application.guest.pending else None
    if facade is None or scope_id!='guest:'+facade.id or session_epoch!=facade.epoch:return _encode(_error('account_changed','体验会话已切换，本次语音已取消。'))
    try:return _encode({'ok':True,'result':facade.transcribe(capture_path,progress)})
    except Exception as error:
        code=getattr(error,'code','cloud_unavailable')
        if code in {'network','guest_unavailable','guest_session_gone'}:code='cloud_unavailable'
        return _encode(_error(code,'本次语音已取消。'if code in {'cancelled','account_changed'}else'语音服务暂时不可用，请稍后重新录入。'))
