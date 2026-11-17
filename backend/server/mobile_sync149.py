"""Android local-first snapshots and explicitly scoped queued writes.

Full resource fingerprints include deletions. Clients atomically replace a
completed resource snapshot and overlay their own pending intents; they never
upload a stale full database. Existing tables, RLS and append-only RPC receipts
are reused without changing any database grants or schema.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ollama_chat_app.data.database import timestamp_to_db
from .platform_leases import server_instance_id

NAMESPACE=UUID('24033c6e-3369-4c62-a2b7-35785c09fc68')
PAGE_LIMIT=100
PAGE_BYTES=4*1024*1024
SCAN_BATCH=16
RESOURCES=frozenset({'profile','preferences','life_records','reminders','goals','observations',
    'reading','reading_social','ai_preferences','memory_settings','ai_profile'})
PURE_SNAPSHOT_RESOURCES=frozenset({'profile','preferences','life_records','reminders',
    'goals','observations','reading','reading_social','memory_settings','ai_profile'})
MULTIMODAL_RESOURCE_DOMAINS={
    'profile':('profile',),'preferences':None,'life_records':('life_records',),
    'reminders':('reminders',),'goals':('goals_habits',),'observations':('life_records',),
    'reading':('reading',),'reading_social':('reading',),'ai_preferences':('preferences',),
    'memory_settings':None,'ai_profile':('preferences',),
}
# ai_preferences deliberately remains outside: categories.view() may initialize
# missing category rows, which must not be attempted in a read-only snapshot.
ACTION_RESOURCE={
    'health.profile.save':'profile','health.records.add':'life_records','health.records.delete':'life_records',
    'expansion.records.update':'life_records','expansion.records.delete':'life_records',
    'health.reminders.add':'reminders','health.reminders.update':'reminders',
    'health.reminders.toggle':'reminders','health.reminders.delete':'reminders',
    'preferences.update':'preferences',
    **{'expansion.goals.'+suffix:'goals' for suffix in ('create','update','close','delete','checkin','uncheckin')},
    **{'expansion.observations.'+suffix:'observations' for suffix in ('create','update','delete')},
    'expansion.content.progress.update':'reading',
    **{name:'reading_social' for name in ('reading.social.set','reading.share','reading.comment.add','reading.comment.like')},
    'member.ai-profile.categories.save':'ai_preferences','member.ai-profile.save':'ai_profile',
    'member.memory.forget':'memory_settings','member.memory.controls.update':'memory_settings',
}
APPENDS=frozenset({'health.records.add','health.reminders.add','expansion.goals.create',
    'expansion.goals.checkin','expansion.observations.create',
    'reading.social.set','reading.share','reading.comment.add','reading.comment.like'})
PREFERENCE_MAP={
    'ai_context_consent':'mobile_ai_library_enabled','ai_personal_context_consent':'mobile_ai_personal_enabled',
    **{'ai_'+key:'mobile_ai_'+key for key in ('thinking_enabled','reasoning_effort','web_enabled','tools_enabled')},
}
FORBIDDEN=frozenset({'actor_id','user_id','actor_user_id','member_user_id','_fawnsee_actor_id',
    'password','password_hash','api_key','access_token','refresh_token','secret','token'})


def canonical(value):
    return json.dumps(jsonable_encoder(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


class LocalSyncError(RuntimeError):
    def __init__(self,code,status=422):self.code,self.status=code,status;super().__init__(code)


def valid_uuid(value):
    try:
        if type(value) is not str or str(UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise LocalSyncError('invalid_operation_id') from None
    return value


def frontend_preferences(value):
    result=dict(value)
    for external,stored in PREFERENCE_MAP.items():
        if stored in result:result[external]=result.pop(stored)
    result.pop('ai_library_enabled',None)
    return result


class MobileSync149:
    def __init__(self,auth,services,*,memory_service,category_service,clock=None):
        self.auth,self.db,self.services=auth,auth.database,services
        self.memory,self.categories=memory_service,category_service
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.instance_id=server_instance_id(auth.settings.token_pepper.get_secret_value())

    def _actor(self,principal,connection):
        actor=principal.user.id
        row=connection.execute('SELECT role_code,account_status FROM users WHERE id=?',(actor,)).fetchone()
        if row is None or row['role_code']!='member' or row['account_status']!='active':
            raise LocalSyncError('local_sync_permission',403)
        session=connection.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',
            (principal.token_digest,actor,timestamp_to_db(self.clock()))).fetchone()
        if session is None:raise LocalSyncError('authentication_required',401)
        return actor

    def _walk(self,conn,actor,table,*,key='id'):
        # Table/key identifiers come only from private constants below.
        cursor='' if key=='content_id' else 0
        while True:
            rows=conn.execute(f'SELECT * FROM {table} WHERE user_id=? AND {key}>? ORDER BY {key} LIMIT ?',
                (actor,cursor,SCAN_BATCH)).fetchall()
            if not rows:return
            for row in rows:yield dict(row)
            cursor=rows[-1][key]

    def _fingerprint(self,conn,actor,resource,value=None):
        tables={
            'life_records':(('life_records','id'),('expansion_meals','id')),
            'reminders':(('reminders','id'),),
            'goals':(('expansion_goals','id'),('expansion_habit_entries','id'),('reminders','id')),
            'observations':(('expansion_observations','id'),),
            'reading':(('expansion_content_progress','content_id'),),
        }
        # Empty additive fields preserve old outbox CAS until genuinely filled.
        if resource=='profile'and isinstance(value,dict):
            value=dict(value)
            if value.get('age')is None:value.pop('age',None)
            if value.get('weight_kg')is None:value.pop('weight_kg',None)
            if value.get('personal_introduction','')==(value.get('dietary_preferences')or''):
                # A synchronized alias carries no new fact. Preserve pre-upgrade CAS.
                value.pop('personal_introduction',None)
        if resource=='preferences'and isinstance(value,dict)and value.get('mobile_onboarding_v1')is None:
            value={key:item for key,item in value.items()if key!='mobile_onboarding_v1'}
        if resource not in tables:return digest(value)
        result=hashlib.sha256(canonical({'resource':resource,'schema':1}).encode())
        if resource=='goals':result.update(self.clock().astimezone(timezone(timedelta(hours=8))).date().isoformat().encode())
        for table,key in tables[resource]:
            result.update(table.encode())
            for row in self._walk(conn,actor,table,key=key):
                encoded=canonical(row).encode();result.update(len(encoded).to_bytes(8,'big'));result.update(encoded)
        return result.hexdigest()

    def _value(self,conn,actor,resource,offset):
        health=self.services['health'];expansion=self.services['expansion']
        if resource=='reading_social':
            value=self.services['reading_social'].snapshot(conn,actor) if 'reading_social' in self.services else {'articles':{},'follows':{},'comment_likes':{}}
            return value,1,None
        if resource=='profile':return health.get_profile(actor),1,None
        if resource=='preferences':
            result=dict(self.services['preferences'].get(actor));result.pop('avatar_path',None)
            return result,1,None
        if resource=='reminders':
            count=conn.execute('SELECT COUNT(*) AS n FROM reminders WHERE user_id=?',(actor,)).fetchone()['n']
            rows=conn.execute("SELECT * FROM reminders WHERE user_id=? ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,scheduled_at,id LIMIT ? OFFSET ?",
                (actor,PAGE_LIMIT,offset)).fetchall()
            values=[health._reminder_from_row(row) for row in rows]
            for value in values:value['version']=digest(value)
            return values,count,offset+len(values) if offset+len(values)<count else None
        if resource=='memory_settings':return self.memory.settings(actor),1,None
        if resource=='ai_preferences':return self.categories.view(actor),1,None
        if resource=='ai_profile':return self.memory.ai_profile(actor),1,None
        if resource=='reading':
            result={}
            for row in self._walk(conn,actor,'expansion_content_progress',key='content_id'):
                row['saved']=bool(row['saved']);result[row['content_id']]=row
                if len(canonical(result).encode())>PAGE_BYTES:raise LocalSyncError('local_resource_too_large',413)
            return result,len(result),None
        if resource=='goals':
            total=conn.execute('SELECT COUNT(*) AS n FROM expansion_goals WHERE user_id=?',(actor,)).fetchone()['n']
            ids=conn.execute('SELECT id FROM expansion_goals WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                (actor,PAGE_LIMIT,offset)).fetchall()
            items=[];used=4096
            for row in ids:
                value=expansion.get_goal(actor,{'goal_id':row['id']});size=len(canonical(value).encode())
                if used+size>PAGE_BYTES:
                    if not items:raise LocalSyncError('local_resource_too_large',413)
                    break
                items.append(value);used+=size
            return {'items':items,'total':total,'limit':len(items),'offset':offset},total,offset+len(items) if offset+len(items)<total else None
        function={'life_records':expansion.list_records,'goals':expansion.list_goals,
            'observations':expansion.list_observations}[resource]
        page=function(actor,{'limit':PAGE_LIMIT,'offset':offset})
        items=[];used=4096
        for row in page['items']:
            size=len(canonical(row).encode())
            if used+size>PAGE_BYTES:
                if not items:raise LocalSyncError('local_resource_too_large',413)
                break
            items.append(row);used+=size
        total=page['total'];next_offset=offset+len(items) if offset+len(items)<total else None
        value=items if resource=='life_records' else {**page,'items':items,'limit':len(items)}
        return value,total,next_offset

    def _state(self,conn,actor,resource,offset=0,version=None):
        if resource not in RESOURCES or type(offset) is not int or not 0<=offset<=100000:
            raise LocalSyncError('invalid_sync_resource')
        if offset and (type(version) is not str or not re.fullmatch('[a-f0-9]{64}',version)):
            raise LocalSyncError('sync_version_required')
        value,count,next_offset=self._value(conn,actor,resource,offset)
        current=self._fingerprint(conn,actor,resource,value)
        if version is not None and (type(version) is not str or not hmac.compare_digest(version,current)):
            raise LocalSyncError('sync_resource_changed',409)
        if len(canonical(value).encode())>PAGE_BYTES:raise LocalSyncError('local_resource_too_large',413)
        config=self.memory.settings(actor)
        generation=self._account_state(conn,actor)['data_generation']
        return {'schema_version':1,'actor_id':actor,'server_instance_id':self.instance_id,
            'data_generation':generation,
            'resource':resource,'version':current,'value':jsonable_encoder(value),
            'offset':offset,'count':count,'complete':next_offset is None,'next_offset':next_offset,
            'memory_revision':config['revision'],'generated_at':self.clock().isoformat()}

    def _account_state(self,conn,actor):
        from .account_data_generation11853 import account_data_state
        return account_data_state(conn,actor,self.auth.settings.token_pepper.get_secret_value())

    def account_state(self,principal):
        with self.auth.identity(principal),self.db.connect()as conn:
            actor=self._actor(principal,conn)
            return self._account_state(conn,actor)

    def _check_generation(self,conn,actor,expected,*,require_after_reset=True):
        from .account_data_generation11853 import generation_matches,HEX64
        if expected is not None and(type(expected)is not str or HEX64.fullmatch(expected)is None):
            raise LocalSyncError('invalid_data_generation')
        if not generation_matches(expected,self._account_state(conn,actor),require_after_reset=require_after_reset):
            raise LocalSyncError('account_data_reset',409)

    def state(self,principal,resource,offset=0,version=None,data_generation=None):
        scope=(getattr(self.db,'snapshot',self.db.connect)
               if resource in PURE_SNAPSHOT_RESOURCES else self.db.transaction)
        with self.auth.identity(principal),scope() as conn:
            actor=self._actor(principal,conn)
            # Legacy state reads remain possible; an explicitly supplied
            # generation must bind every page before any personal value read.
            self._check_generation(conn,actor,data_generation,require_after_reset=False)
            return self._state(conn,actor,resource,offset,version)

    def _receipt_id(self,actor,op):return str(uuid5(NAMESPACE,f'{actor}:{op}'))

    def _result_entity(self,conn,actor,action,payload,result):
        if action=='health.records.add' and type(result) is int:
            return self.services['expansion'].get_record(actor,{'record_id':result})
        if action in {'expansion.records.update','expansion.observations.create','expansion.observations.update',
                      'expansion.goals.create','expansion.goals.update','expansion.goals.close'}:
            return result
        if action in {'expansion.goals.checkin','expansion.goals.uncheckin'}:
            return result.get('goal') if isinstance(result,dict) else None
        if action in {'health.reminders.add','health.reminders.update','health.reminders.toggle'}:
            ident=result if type(result) is int else payload.get('reminder_id')
            row=conn.execute('SELECT * FROM reminders WHERE user_id=? AND id=?',(actor,ident)).fetchone()
            if row:
                value=self.services['health']._reminder_from_row(row);return {**value,'version':digest(value)}
        return None

    def _safe_payload(self,payload):
        if type(payload) is not dict or set(payload)&FORBIDDEN or len(canonical(payload).encode())>768*1024:
            raise LocalSyncError('invalid_local_operation')
        return dict(payload)

    def _shrinks_privacy(self,action,payload):
        if action=='member.memory.forget':return True
        if action!='member.memory.controls.update':return False
        values=payload.get('controls')
        return type(values) is list and bool(values) and all(type(row) is dict
            and row.get('enabled_for_ai') is False and row.get('collect_events') is False for row in values)

    def _dispatch(self,actor,op,action,payload):
        body=dict(payload);body.pop('request_id',None)
        if action.startswith('reading.'):
            return self.services['reading_social'].mutate(actor,op,action,body)
        health=self.services['health'];expansion=self.services['expansion']
        if action=='health.profile.save':return health.save_profile(actor,body)
        if action=='health.records.add':return health.add_life_record(actor,**body)
        if action=='health.records.delete':
            if set(body)!={'record_ids'}:raise LocalSyncError('invalid_local_operation')
            return health.delete_life_records(actor,body['record_ids'])
        if action=='health.reminders.add':return health.add_reminder(actor,**body)
        if action in {'health.reminders.update','health.reminders.toggle'}:
            if 'reminder_id' not in body:raise LocalSyncError('invalid_local_operation')
            ident=body.pop('reminder_id')
            if action.endswith('.update'):return health.update_reminder(actor,ident,**body)
            if set(body)-{'enabled'}:raise LocalSyncError('invalid_local_operation')
            return health.toggle_reminder(actor,ident,body.get('enabled'))
        if action=='health.reminders.delete':
            if set(body)!={'reminder_ids'}:raise LocalSyncError('invalid_local_operation')
            return health.delete_reminders(actor,body['reminder_ids'])
        if action=='preferences.update':
            if set(body)&({'ai_library_enabled','avatar_path'}|set(PREFERENCE_MAP.values())):
                raise LocalSyncError('invalid_local_operation')
            body={PREFERENCE_MAP.get(key,key):value for key,value in body.items()}
            return frontend_preferences(self.services['preferences'].update(actor,body))
        if action=='member.ai-profile.categories.save':
            if set(body)!={'categories'}:raise LocalSyncError('invalid_local_operation')
            return self.categories.save(actor,body['categories'],op)
        if action=='member.ai-profile.save':
            if set(body)!={'expected_revision','entries'}:raise LocalSyncError('invalid_local_operation')
            return self.memory.save_ai_profile(actor,body['expected_revision'],op,body['entries'])
        if action=='member.memory.forget':
            if set(body)!={'domains'}:raise LocalSyncError('invalid_local_operation')
            return self.memory.forget(actor,body['domains'])
        if action=='member.memory.controls.update':
            if set(body)!={'controls'}:raise LocalSyncError('invalid_local_operation')
            return self.memory.change_controls(actor,body['controls'])
        method={
            'expansion.records.update':'update_record','expansion.records.delete':'delete_record',
            **{'expansion.goals.'+suffix:method for suffix,method in (
                ('create','create_goal'),('update','update_goal'),('close','close_goal'),('delete','delete_goal'),
                ('checkin','checkin_goal'),('uncheckin','uncheckin_goal'))},
            **{'expansion.observations.'+suffix:method for suffix,method in (
                ('create','create_observation'),('update','update_observation'),('delete','delete_observation'))},
            'expansion.content.progress.update':'update_content_progress',
        }.get(action)
        if method is None:raise LocalSyncError('offline_operation_not_permitted',403)
        return getattr(expansion,method)(actor,body)

    def apply(self,principal,operation):
        return self._apply_with_status(principal,operation)[0]

    def _apply_with_status(self,principal,operation):
        """Return a private fresh-commit flag without changing public receipts."""
        op=valid_uuid(operation['operation_id']);action=operation['action']
        if action not in ACTION_RESOURCE:raise LocalSyncError('offline_operation_not_permitted',403)
        payload=self._safe_payload(operation['payload']);base=operation.get('base_version')
        local_id=operation.get('local_id')
        if local_id is not None:valid_uuid(local_id)
        if base is not None and (type(base) is not str or not re.fullmatch('[a-f0-9]{64}',base)):
            raise LocalSyncError('invalid_resource_version')
        # The new authorization fence is not part of the legacy business
        # digest: migrated uncertain operations retain their immutable receipt.
        fingerprint=digest({key:value for key,value in operation.items()if key!='data_generation'});resource=ACTION_RESOURCE[action]
        with self.auth.identity(principal),self.db.transaction() as conn:
            actor=self._actor(principal,conn)
            self._check_generation(conn,actor,operation.get('data_generation'))
            rid=self._receipt_id(actor,op)
            previous=conn.execute('SELECT request_digest,response_json FROM rpc_requests WHERE user_id=? AND request_id=?',(actor,rid)).fetchone()
            if previous:
                if not hmac.compare_digest(previous['request_digest'],fingerprint):
                    raise LocalSyncError('operation_payload_conflict',409)
                value=json.loads(previous['response_json'])
                if type(value) is not dict or set(value)!={'mobile149'}:raise LocalSyncError('local_receipt_invalid',503)
                receipt=value['mobile149']
                if action.startswith('expansion.goals.') and action!='expansion.goals.delete' and receipt.get('status')=='applied':
                    result=receipt.get('result')
                    entity=receipt.get('result_entity')
                    if not isinstance(entity,dict):entity=result.get('goal',result)if isinstance(result,dict)else{}
                    goal_id=entity.get('id')or payload.get('goal_id')
                    if type(goal_id)is int and goal_id>0 and conn.execute('SELECT id FROM expansion_goals WHERE user_id=? AND id=?',(actor,goal_id)).fetchone()is None:
                        current=self._state(conn,actor,'goals')
                        receipt={**receipt,'result':{'deleted':True,'goal_id':goal_id,'deleted_count':0,'reminder_ids':[]},
                                 'result_entity':None,'result_deleted':True,'server_version':current['version']}
                return receipt,False
            if action in {'health.records.add','expansion.records.update','expansion.observations.create',
                          'expansion.observations.update'}:
                from .record_assistant_try12 import draft_was_replaced
                if draft_was_replaced(conn,actor,op):
                    raise LocalSyncError('record_draft_superseded',422)
            before=self._state(conn,actor,resource)
            required=action not in APPENDS and not self._shrinks_privacy(action,payload)
            if required and base is None:raise LocalSyncError('resource_version_required')
            if base is not None and not hmac.compare_digest(base,before['version']):
                answer={'status':'conflict','operation_id':op,'action':action,'resource':resource,
                    'local_id':local_id,'result':None,'server_version':before['version'],
                    'previous_version':base,'current':before}
            else:
                source_scope=getattr(self.memory,'multimodal_mutation_invalidator',None)
                source_handled=False
                source_methods={
                    'preferences.update':('preferences','update_preferences',{'patch':{
                        PREFERENCE_MAP.get(key,key):value for key,value in payload.items()}}),
                    'health.records.delete':('health','delete_life_records',payload),
                    'expansion.records.update':('expansion','update_record',{'payload':payload}),
                    'expansion.records.delete':('expansion','delete_record',{'payload':payload}),
                    'expansion.observations.update':('expansion','update_observation',{'payload':payload}),
                    'expansion.observations.delete':('expansion','delete_observation',{'payload':payload}),
                }
                if callable(source_scope)and action in source_methods:
                    service,method,arguments=source_methods[action]
                    source_handled=source_scope(actor,service,method,arguments,conn)
                result=self._dispatch(actor,op,action,payload)
                invalidate=getattr(self.memory,'multimodal_invalidator',None)
                if callable(invalidate)and action not in APPENDS and not source_handled:
                    invalidate(actor,domains=MULTIMODAL_RESOURCE_DOMAINS[resource],connection=conn)
                if action in {'reading.comment.add','reading.comment.like'} and isinstance(result,dict) and isinstance(result.get('comment'),dict):
                    comment=result['comment']
                    if len(str(comment.get('avatar_data') or ''))>16384:
                        # Keep the complete confirmation inside the immutable
                        # receipt budget. Public comment pages retain the full
                        # avatar; the local pending copy can fill it meanwhile.
                        result={**result,'comment':{**comment,'avatar_data':'','avatar_omitted':True}}
                after=self._state(conn,actor,resource)
                answer={'status':'applied','operation_id':op,'action':action,'resource':resource,
                    'local_id':local_id,'result':jsonable_encoder(result),
                    'result_entity':jsonable_encoder(self._result_entity(conn,actor,action,payload,result)),
                    'previous_version':before['version'],'server_version':after['version']}
            encoded=canonical({'mobile149':answer})
            if len(encoded.encode())>120000:
                # Conflict details need not duplicate a whole large snapshot in
                # the immutable receipt; clients fetch current using its version.
                if answer['status']=='conflict':
                    answer['current']={key:value for key,value in before.items() if key!='value'}
                    encoded=canonical({'mobile149':answer})
                elif answer['status']=='applied':
                    answer.update(result=None,result_entity=None,result_available_in_state=True,result_resource=resource)
                    encoded=canonical({'mobile149':answer})
                if len(encoded.encode())>120000:raise LocalSyncError('local_result_too_large',413)
            conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
                (actor,rid,fingerprint,encoded,timestamp_to_db(self.clock())))
        return answer,answer['status']=='applied'


def schedule_projection149(request,background,principal,domains):
    """Best-effort postcommit projection; never replay a committed mutation.

    The existing coalescing worker retains failed domains for its next mutation,
    and dated reads repair them independently. Retry enqueue failures in the
    response's background phase, keeping the original successful reply intact.
    This queues derived refreshes only and does not append user action events.
    """
    projector=getattr(request.app.state,'memory_projection',None)
    if projector is None or not domains:return
    try:
        if projector.schedule(background,principal,domains):return
    except Exception:pass
    async def retry():
        import asyncio
        for _ in range(3):
            tasks=BackgroundTasks()
            try:
                if projector.schedule(tasks,principal,domains):
                    await tasks();return
            except Exception:pass
            await asyncio.sleep(0.1)
    background.add_task(retry)


class LocalOperation(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True)
    operation_id:str=Field(min_length=36,max_length=36)
    action:str=Field(min_length=3,max_length=90)
    payload:dict
    base_version:str|None=Field(default=None,min_length=64,max_length=64)
    local_id:str|None=Field(default=None,min_length=36,max_length=36)
    data_generation:str|None=Field(default=None,min_length=64,max_length=64,pattern=r'^[a-f0-9]{64}$')


def create_mobile_sync149_router(auth,service):
    router=APIRouter()
    async def identity(request):
        import asyncio
        header=request.headers.get('authorization','')
        if not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}',header):raise LocalSyncError('authentication_required',401)
        return await asyncio.to_thread(auth.resolve_token,header[7:])
    def failed(error):
        return JSONResponse({'detail':error.code},status_code=error.status,headers={'Cache-Control':'no-store'})
    @router.get('/v1/member/local-sync/account-state')
    async def account_state(request:Request):
        import asyncio
        try:
            principal=await identity(request)
            result=await asyncio.to_thread(service.account_state,principal)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except LocalSyncError as error:return failed(error)
        except Exception as error:
            status=getattr(error,'status_code',503)
            return failed(LocalSyncError('authentication_required'if status in{401,403}else'local_sync_unavailable',status))
    @router.get('/v1/member/local-sync/state')
    async def state(resource:str,request:Request,offset:int=0,version:str|None=None,data_generation:str|None=None):
        import asyncio
        try:
            principal=await identity(request)
            result=await asyncio.to_thread(service.state,principal,resource,offset,version,data_generation)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except LocalSyncError as error:return failed(error)
        except Exception as error:
            status=getattr(error,'status_code',503)
            return failed(LocalSyncError('authentication_required' if status in {401,403} else 'local_sync_unavailable',status))
    @router.post('/v1/member/local-sync/operations')
    async def operation(value:LocalOperation,request:Request,background:BackgroundTasks):
        import asyncio
        try:
            principal=await identity(request)
            result,fresh=await asyncio.to_thread(service._apply_with_status,principal,value.model_dump())
            if fresh:
                domains={'life_records':('life_records',),'observations':('life_records',),
                    'reminders':('reminders',),'goals':('goals_habits','reminders'),'reading':('reading',),'reading_social':('reading',)}.get(result['resource'],())
                schedule_projection149(request,background,principal,domains)
                conversation = getattr(request.app.state, 'conversation_memory', None)
                if conversation is not None:
                    background.add_task(conversation.start, request, principal)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except LocalSyncError as error:return failed(error)
        except Exception as error:
            status=getattr(error,'status_code',None)
            if status in {401,403}:return failed(LocalSyncError('authentication_required',status))
            if status in {409,422}:return failed(LocalSyncError('local_data_changed' if status==409 else 'invalid_local_operation',status))
            code=getattr(error,'code',None)
            if code in {'conflict','invalid_state','not_found'}:
                return failed(LocalSyncError('local_data_changed',409))
            if isinstance(error,(TypeError,ValueError,LookupError)):
                return failed(LocalSyncError('invalid_local_operation'))
            return failed(LocalSyncError('local_sync_unavailable',503))
    return router
