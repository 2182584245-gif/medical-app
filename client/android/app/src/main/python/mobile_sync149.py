"""Local-first member facade and durable, owner-bound synchronization.

Reads observe local resources plus pending edits. All server writes use the
fixed authenticated mobile sync endpoint and a stable operation UUID. Public
transactions, AI generation and approvals keep their existing online paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import re
import base64
import hashlib
import heapq
import sqlite3
import threading
import time
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from mobile_local149 import Identity, LocalDataError, LocalStore149, digest, json_text
from mobile_onboarding11830 import ACTIONS as ONBOARDING_ACTIONS11830, observe as observe_onboarding11830, cached_complete as cached_onboarding11830
from mobile_record_photos11839 import wire_record_photos
from mobile_reading_social import READS as READING_SOCIAL_READS, WRITES as READING_SOCIAL_WRITES, project as project_reading_social, read as read_reading_social, check as check_reading_social, bounded_comments
from ollama_chat_app.services.health import HealthService, PROFILE_FIELDS, LIFE_RECORD_CATEGORIES
from ollama_chat_app.services.preferences import PreferencesService, DEFAULT_PREFERENCES, validate_diet_library
from ollama_chat_app.data.database import timestamp_to_db

STATE_PATH='/v1/member/local-sync/state'
OPERATIONS_PATH='/v1/member/local-sync/operations'
RESOURCES=('profile','preferences','life_records','reminders','goals','observations','reading','reading_social','ai_preferences','memory_settings','ai_profile')
WRITE_RESOURCES={
 'health.profile.save':'profile','preferences.update':'preferences',
 'health.records.add':'life_records','health.records.delete':'life_records',
 'expansion.records.update':'life_records','expansion.records.delete':'life_records',
 'health.reminders.add':'reminders','health.reminders.update':'reminders',
 'health.reminders.toggle':'reminders','health.reminders.delete':'reminders',
 'expansion.goals.create':'goals','expansion.goals.update':'goals','expansion.goals.close':'goals','expansion.goals.delete':'goals',
 'expansion.goals.checkin':'goals','expansion.goals.uncheckin':'goals',
 'expansion.observations.create':'observations','expansion.observations.update':'observations',
 'expansion.observations.delete':'observations','expansion.content.progress.update':'reading',
 **{name:'reading_social' for name in READING_SOCIAL_WRITES},
 'member.ai-profile.categories.save':'ai_preferences',
 'member.ai-profile.save':'ai_profile','member.memory.forget':'memory_settings','member.memory.controls.update':'memory_settings',
}
APPENDS=frozenset({'health.records.add','health.reminders.add','expansion.goals.create',
 'expansion.observations.create','expansion.goals.checkin','member.memory.forget'}) | READING_SOCIAL_WRITES
CREATE_IDS={'health.records.add':'life_records','health.reminders.add':'reminders',
 'expansion.goals.create':'goals','expansion.observations.create':'observations'}
RESOURCE_READS={
 'health.profile.get':'profile','preferences.get':'preferences',
 'health.records.list':'life_records','expansion.records.list':'life_records',
 'expansion.records.get':'life_records','expansion.records.revisions':'life_records',
 'health.reminders.list':'reminders','expansion.goals.list':'goals','expansion.goals.get':'goals',
 'expansion.observations.list':'observations','expansion.observations.get':'observations',
 'expansion.content.progress':'reading','member.ai-profile.categories.get':'ai_preferences',
 'member.memory.settings.get':'memory_settings','member.ai-profile.get':'ai_profile',
}
READ_ACTIONS=READING_SOCIAL_READS|frozenset(RESOURCE_READS)|frozenset({
 'health.dashboard','health.statistics','health.service-summary','chat.conversations.list','chat.messages.list',
 'chat.message.attachments','chat.image.get','chat.files.list','chat.tools.proposals',
 'reading.catalog','reading.get','reading.article','reading.asset','library.catalog','library.search','library.card','library.source',
 'forum.topics','forum.posts','forum.posts.comments','forum.image.get','forum.member.profile',
 'service.appointments','service.history','service.tasks','shopping.products','shopping.cart','shopping.favorites',
 'shopping.orders','shopping.recommendations','member.memory.settings.get','member.memory.overview',
 'member.memory.days','member.memory.preferences.list','member.ai-profile.get','expansion.growth.summary',
 'expansion.content.list','expansion.content.get','expansion.settings.get','expansion.bootstrap',
})
_ID_FIELDS=frozenset({'goal_id','observation_id','record_id','reminder_id'})
_FORBIDDEN=frozenset({'user_id','member_user_id','actor_id','actor_user_id','api_key','password','token','access_token','base_url','path'})
IMMUTABLE_READS=frozenset({'reading.asset','chat.image.get','chat.message.attachments','forum.image.get'})
NO_BACKGROUND_READS=IMMUTABLE_READS|frozenset({'expansion.bootstrap'})


def _projection_resources(action,*,write=False):
    """Declare only data the existing projection/validator actually consumes."""
    if action in READING_SOCIAL_READS:return {'reading_social','reading'}
    if action in READING_SOCIAL_WRITES:return {'reading_social','reading','profile','preferences'}
    if action in {'health.dashboard','health.statistics'}:return {'profile','life_records','reminders'}
    resource=(WRITE_RESOURCES if write else RESOURCE_READS).get(action)
    if resource in {'reminders','goals'}:return {'reminders','goals'}
    if action=='member.memory.forget':return {'memory_settings','ai_preferences','ai_profile'}
    return {resource} if resource else set()


def _plain(value):return json.loads(json_text(value))
def _now():return datetime.now(timezone.utc).isoformat()
def _day():return datetime.now(timezone(timedelta(hours=8))).date().isoformat()
def _iso(value):
    if isinstance(value,datetime):stamp=value
    elif isinstance(value,str):stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    else:raise ValueError('日期格式无效。')
    if stamp.tzinfo is None:raise ValueError('请填写含时区的日期。')
    return stamp
def _text(value,maximum,*,optional=False):
    if value is None and optional:return None
    if not isinstance(value,str) or len(value)>maximum or (not optional and not value.strip()):raise ValueError('请检查填写内容。')
    return value.strip()
def _number(value,low,high,*,optional=False):
    if value is None and optional:return None
    if type(value) not in {int,float} or not math.isfinite(value) or not low<=value<=high:raise ValueError('数值超出可填写范围。')
    return value
def _local_number(identifier):return -(int(UUID(identifier))%(2**52-1)+1)
def _items(value):
    if isinstance(value,list):return value
    return value.get('items',[]) if isinstance(value,dict) else []

def _bounded_chat_page(value,limit=40):
    """Repair a cached page index, never the durable conversation itself."""
    if type(limit)is not int or not 1<=limit<=200:return None
    rows=value if isinstance(value,list)else value.get('messages',value.get('items'))if isinstance(value,dict)else None
    if not isinstance(rows,list):return value
    if len(rows)<=limit:return value
    # Old greeting receipts could append indefinitely to the newest-page cache.
    # A real sequence cursor keeps earlier messages available through pagination.
    if any(not isinstance(row,dict)or type(row.get('id'))is not int or row['id']<=0
           or type(row.get('sequence_no'))is not int or row['sequence_no']<0 for row in rows):return None
    unique={row['id']:row for row in rows}
    ordered=sorted(unique.values(),key=lambda row:row['sequence_no'])
    selected=ordered[-limit:];page=value if isinstance(value,dict)else {}
    more=page.get('has_more')is True or len(ordered)>limit
    cursor=selected[0]['sequence_no']if more and selected else None
    if more and(not isinstance(cursor,int)or cursor<=0):return None
    visible={row['id']for row in selected};groups=[]
    for group in page.get('welcome_groups',[]):
        if not isinstance(group,dict)or not isinstance(group.get('message_ids'),list):continue
        ids=[identifier for identifier in group['message_ids']if identifier in visible]
        if ids:groups.append({**group,'message_ids':ids})
    from ollama_chat_app.services.first_meet_greeting import annotate_welcome_rows
    result={**page,'messages':annotate_welcome_rows(selected,groups),'welcome_groups':groups,'has_more':more,'next_cursor':cursor}
    if 'items'in result:result['items']=deepcopy(result['messages'])
    if 'next_before_sequence_no'in result:result['next_before_sequence_no']=cursor
    return result

def _scope(identity):
    return {'actor_id':identity.actor_id,'endpoint':identity.endpoint,'epoch':identity.epoch,
        'server_instance_id':identity.server_instance_id,'namespace':identity.namespace,'data_generation':identity.data_generation}


class _HealthRAM:
    """Reuse the existing health calculations over an in-memory projection."""
    def __init__(self,actor,resources):
        self.connection=sqlite3.connect(':memory:',isolation_level=None)
        self.connection.row_factory=sqlite3.Row
        self.connection.executescript('''PRAGMA temp_store=MEMORY;
          CREATE TABLE users(id INTEGER PRIMARY KEY,role_code TEXT,account_status TEXT);
          CREATE TABLE life_records(id INTEGER PRIMARY KEY,user_id INTEGER,category TEXT,occurred_at TEXT,local_date TEXT,
            timezone_offset_minutes INTEGER,content TEXT,details_json TEXT,source TEXT,created_at TEXT,updated_at TEXT);
          CREATE TABLE reminders(id INTEGER PRIMARY KEY,user_id INTEGER,title TEXT,reminder_type TEXT,scheduled_at TEXT,
            status TEXT,created_at TEXT,updated_at TEXT,repeat_rule TEXT,paused_until TEXT);
          CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,actor_user_id INTEGER,action TEXT,entity_type TEXT,entity_id INTEGER,details_json TEXT,created_at TEXT);
        ''')
        columns=','.join(name+' REAL' if name in {'height_cm','weight_kg'} else name+' TEXT' for name in sorted(PROFILE_FIELDS))
        self.connection.execute('CREATE TABLE member_profiles(user_id INTEGER PRIMARY KEY,'+columns+',phone_default_initialized INTEGER NOT NULL DEFAULT 0,personal_introduction_updated_at TEXT,created_at TEXT,updated_at TEXT)')
        self.connection.execute("INSERT INTO users VALUES(?,'member','active')",(actor,))
        profile={**{name:None for name in PROFILE_FIELDS},**(resources.get('profile') or {})}
        profile.setdefault('reminder_frequency','normal')
        names=['user_id',*sorted(PROFILE_FIELDS),'created_at','updated_at']
        self.connection.execute('INSERT INTO member_profiles('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',
            [actor,*[profile.get(key) for key in sorted(PROFILE_FIELDS)],str(profile.get('created_at') or _now()),str(profile.get('updated_at') or _now())])
        for row in _items(resources.get('life_records',[])):
            occurred=_iso(row['occurred_at']);details=row.get('details')
            if details is None and row.get('details_json'):details=json.loads(row['details_json'])
            self.connection.execute('INSERT INTO life_records VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'],actor,row['category'],timestamp_to_db(occurred),row.get('local_date') or occurred.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                 row.get('timezone_offset_minutes',480),row.get('content',''),json_text(details) if details is not None else None,row.get('source','manual'),
                 timestamp_to_db(_iso(row.get('created_at') or _now())),timestamp_to_db(_iso(row.get('updated_at') or _now()))))
        for row in _items(resources.get('reminders',[])):
            self.connection.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?)',
                (row['id'],actor,row['title'],row.get('reminder_type','custom'),timestamp_to_db(_iso(row['scheduled_at'])),row.get('status','active'),
                 timestamp_to_db(_iso(row.get('created_at') or _now())),timestamp_to_db(_iso(row.get('updated_at') or _now())),row.get('repeat_rule','none'),row.get('paused_until')))
    def initialize(self):pass
    @contextmanager
    def connect(self):yield self.connection
    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN')
        try:yield self.connection;self.connection.execute('COMMIT')
        except BaseException:self.connection.execute('ROLLBACK');raise
    def close(self):self.connection.close()


def _preferences(raw):
    result={**DEFAULT_PREFERENCES,**(raw or {})}
    result['ai_context_consent']=result.pop('mobile_ai_library_enabled',True) is True
    result['ai_personal_context_consent']=result.pop('mobile_ai_personal_enabled',True) is True
    for key,default in (('thinking_enabled',True),('reasoning_effort','high'),('web_enabled',True),('tools_enabled',True)):
        result['ai_'+key]=result.pop('mobile_ai_'+key,default)
    result.pop('ai_library_enabled',None)
    return result


def _preference_patch(body):
    values=dict(body)
    forbidden={'ai_library_enabled','mobile_ai_library_enabled','mobile_ai_personal_enabled',
        'mobile_ai_thinking_enabled','mobile_ai_reasoning_effort','mobile_ai_web_enabled','mobile_ai_tools_enabled'}
    if set(values)&forbidden:raise ValueError('请通过设置页面保存选项。')
    if 'ai_context_consent' in values:values['mobile_ai_library_enabled']=values.pop('ai_context_consent')
    if 'ai_personal_context_consent' in values:values['mobile_ai_personal_enabled']=values.pop('ai_personal_context_consent')
    for key in ('thinking_enabled','reasoning_effort','web_enabled','tools_enabled'):
        if 'ai_'+key in values:values['mobile_ai_'+key]=values.pop('ai_'+key)
    return PreferencesService.validate(values)


def _preference_snapshot(body,*,frontend=True):
    """Normalize a confirmed read without granting its extra fields write access.

    Mobile GET replies use aliases, whereas sync-state resources retain the
    desktop's independent legacy consent fields. Keep that distinction explicit.
    Server-only JSON (onboarding, diet and future read fields) remains available
    through local reads and pending-setting projections, but never enters the
    strict ordinary preference-patch validator.
    """
    if not isinstance(body,dict):raise ValueError('设置资料格式无效。')
    values=_plain(body)
    if frontend:
        aliases={'ai_context_consent':'mobile_ai_library_enabled',
            'ai_personal_context_consent':'mobile_ai_personal_enabled',
            **{'ai_'+name:'mobile_ai_'+name for name in
               ('thinking_enabled','reasoning_effort','web_enabled','tools_enabled')}}
        for alias,target in aliases.items():
            if alias not in values:continue
            value=values.pop(alias)
            if target in values and (type(values[target]) is not type(value) or values[target]!=value):
                raise ValueError('设置资料中的选项不一致，请重新读取。')
            values[target]=value
    known={name:value for name,value in values.items() if name in DEFAULT_PREFERENCES}
    return {**values,**PreferencesService.validate(known)}


def _library_value(value):
    clean=validate_diet_library(value)
    return clean if clean is not None else {'schema_version':1,'foods':[],'recipes':[]}


def _library_only(operation):
    return (operation.get('action')=='preferences.update' and operation.get('resource')=='preferences'
        and isinstance(operation.get('payload'),dict) and set(operation['payload'])=={'diet_library_v1'})


def _library_intent(before,after):
    before,after=_library_value(before),_library_value(after)
    return {'schema_version':1,'before':before,'after':after,
        'added':{kind:[x for x in after[kind]if x not in before[kind]]for kind in('foods','recipes')},
        'removed':{kind:[x for x in before[kind]if x not in after[kind]]for kind in('foods','recipes')}}


def _captured_library(operation):
    value=operation.get('library_intent')
    try:
        if not isinstance(value,dict) or value.get('schema_version')!=1:return None
        expected=_library_intent(value['before'],value['after'])
        return expected if value==expected and expected['after']==_library_value(operation['payload']['diet_library_v1'])else None
    except (ValueError,TypeError,KeyError):return None


def _merge_library(cloud,intent):
    result=_library_value(cloud)
    for kind in('foods','recipes'):
        result[kind]=[x for x in result[kind]if x not in intent['removed'][kind]]
        result[kind].extend(x for x in intent['added'][kind]if x not in result[kind])
    return _library_value(result)


def _library_projection_order(operations):
    if not any(row.get('library_retry')for row in operations):return operations
    prefs={row['operation_id']:row for row in operations if row['resource']=='preferences'}
    indegree={key:0 for key in prefs};children={key:[]for key in prefs}
    for key,row in prefs.items():
        parent=row.get('depends_on')
        if parent in prefs:indegree[key]+=1;children[parent].append(key)
    ready=[(row['sequence_no'],key)for key,row in prefs.items()if not indegree[key]];heapq.heapify(ready);ordered=[]
    while ready:
        _,key=heapq.heappop(ready);ordered.append(prefs[key])
        for child in children[key]:
            indegree[child]-=1
            if not indegree[child]:heapq.heappush(ready,(prefs[child]['sequence_no'],child))
    if len(ordered)!=len(prefs):raise LocalDataError('conflict','待同步收藏的依赖需要先核对。')
    values=iter(ordered)
    return [next(values)if row['resource']=='preferences'else row for row in operations]


class LocalFirst149:
    def __init__(self,root,vault,identity_provider,online_handler,cloud_transport,*,request_sync=None,background_handler=None,cache_budget=200*1024*1024,clock=time.time):
        self.store=LocalStore149(root,vault,cache_budget=cache_budget,clock=clock)
        self.identity_provider=identity_provider;self.online_handler=online_handler;self.cloud_transport=cloud_transport
        self.request_sync=request_sync or (lambda:None);self.clock=clock;self._sync_lock=threading.Lock();self._last_error=None
        self._blocked={}
        self._record_receipt_lock=threading.RLock()
        self.record_assistant_lock=threading.Lock()
        self.background_handler=background_handler;self._last_sync_request=float('-inf')

    def _ask_sync(self,*,force=False):
        now=self.clock()
        if force or now-self._last_sync_request>=2:
            self._last_sync_request=now;self.request_sync()

    def _queue_read_refresh(self,identity,action,body):
        if self.background_handler is None or action in NO_BACKGROUND_READS:return
        key=[action,body]
        if self.store.get(identity,'refresh',key) is None:
            self.store.put(identity,'refresh',key,{'action':action,'payload':body})
        self._ask_sync()

    def identity(self):
        value=Identity.checked(self.identity_provider(),now=self.clock())
        if self._blocked.get(value.namespace)==value.epoch:
            raise LocalDataError('not_authenticated','登录授权已失效，请联网重新登录。')
        return value
    def _guard(self,expected):
        actual=self.identity()
        if actual!=expected:raise LocalDataError('account_changed','账号已变更，本次操作没有作用于其他账号。')
        return actual
    def _resource(self,identity,name):return self.store.get(identity,'resource',name)
    def _resources(self,identity,names=None):
        selected=RESOURCES if names is None else tuple(name for name in RESOURCES if name in names)
        return {name:deepcopy(row['value']) for name in selected if (row:=self._resource(identity,name)) is not None}
    def _base_version(self,identity,name):
        value=self._resource(identity,name)
        return value.get('version') if value else None
    def _mark(self,identity,source='local'):
        meta=self.store.get(identity,'meta','sync',{})
        return {**self.store.status(identity),'source':source,'last_synced_at':meta.get('last_synced_at'),
            'last_error':meta.get('last_error'),'offline_ready':self.store.resources_present(identity,('profile','preferences','life_records','reminders','goals')),
            'personal_ai_allowed':self.ai_context_allowed(identity)}
    def status(self):return self._mark(self.identity())
    def clear_cache(self):return self.store.clear_cache(self.identity())
    def snapshot(self):
        identity=self.identity();value=self._project(identity);self._guard(identity)
        return {'actor_id':identity.actor_id,'resources':{key:item for key,item in value.items() if key in RESOURCES},'status':self._mark(identity)}
    def ai_context_allowed(self,identity=None):
        identity=identity or self.identity();settings=self._resource(identity,'memory_settings')
        if not settings or settings['value'].get('enabled') is not True:return False
        for row in self.store.operations(identity):
            if row['action'] in {'member.memory.forget','member.memory.controls.update'}:return False
            if row['action']=='preferences.update' and row['payload'].get('ai_personal_context_consent') is False:return False
        preferences=self._project(identity,{'preferences'}).get('preferences',{})
        return preferences.get('mobile_ai_personal_enabled',True) is True
    def ai_payload(self,child_operation_id,payload=None):
        identity=self.identity()
        try:
            if str(UUID(child_operation_id))!=child_operation_id:raise ValueError
        except (ValueError,TypeError,AttributeError):raise LocalDataError('validation','操作编号无效。') from None
        key='ai-payload:'+child_operation_id
        if payload is None:return self.store.get(identity,'meta',key)
        if not isinstance(payload,dict):raise LocalDataError('validation','AI操作资料无效。')
        value=self.store.put_if_absent(identity,'meta',key,deepcopy(payload));self._guard(identity);return value
    def draft(self,key,text=None):
        identity=self.identity();_text(key,120)
        if text is None:return self.store.get(identity,'draft',key,{'text':''})
        _text(text,12000,optional=True);self.store.put(identity,'draft',key,{'text':text,'updated_at':_now()})
        return {'saved':True}

    def draft_attachment(self,key,attachment=None,*,discard=False):
        identity=self.identity();_text(key,120);stored_key=['attachment',key]
        if discard:
            if attachment is not None:raise LocalDataError('validation','草稿操作无效。')
            self.store.delete(identity,'draft',stored_key);return {'discarded':True}
        if attachment is None:return self.store.get(identity,'draft',stored_key)
        if not isinstance(attachment,dict) or set(attachment)&_FORBIDDEN:raise LocalDataError('validation','附件草稿格式无效。')
        name=_text(attachment.get('name') or attachment.get('original_name'),200)
        media=attachment.get('media_type') or attachment.get('type');encoded=attachment.get('data_base64')
        if encoded is None and isinstance(attachment.get('src'),str) and attachment['src']:
            match=re.fullmatch(r'data:([^;,]+);base64,([A-Za-z0-9+/=\s]+)',attachment['src'])
            if not match:raise LocalDataError('validation','请重新选择本机图片或文件。')
            media=media or match[1];encoded=match[2]
        allowed={'image/jpeg','image/png','image/webp','application/pdf','text/plain','text/markdown','text/csv',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
        if encoded is None and (attachment.get('src')is None or attachment.get('src')=='') and type(attachment.get('file_id'))is int and 0<attachment['file_id']<2**53:
            if media not in allowed:raise LocalDataError('validation','附件草稿格式不受支持。')
            declared=attachment.get('size_bytes',attachment.get('size'))
            if declared is not None and (type(declared)is not int or not 0<=declared<=16*1024*1024):
                raise LocalDataError('validation','附件大小无效。')
            previous=self.store.get(identity,'draft',stored_key)
            identifier=attachment.get('upload_operation_id') or ((previous or {}).get('upload_operation_id') if (previous or {}).get('file_id')==attachment['file_id'] else None) or str(uuid4())
            try:
                if str(UUID(identifier))!=identifier:raise ValueError
            except (ValueError,TypeError):raise LocalDataError('validation','附件上传编号无效。') from None
            value={'name':name,'media_type':media,'file_id':attachment['file_id'],
                'kind':'image' if media.startswith('image/') else 'document','upload_operation_id':identifier,'updated_at':_now()}
            if declared is not None:value['size_bytes']=declared
            self._guard(identity);self.store.put(identity,'draft',stored_key,value);return deepcopy(value)
        if media not in allowed or not isinstance(encoded,str) or len(encoded)>24*1024*1024:
            raise LocalDataError('validation','附件草稿格式或大小不受支持。')
        try:raw=base64.b64decode(encoded,validate=True)
        except (ValueError,TypeError):raise LocalDataError('validation','附件内容不完整，请重新选择。') from None
        if not 0<len(raw)<=16*1024*1024:raise LocalDataError('local_limit','单个附件草稿不能超过16 MiB。')
        declared=attachment.get('size_bytes',attachment.get('size'))
        if declared is not None and (type(declared)is not int or declared!=len(raw)):
            raise LocalDataError('validation','附件大小校验失败，请重新选择。')
        checksum=hashlib.sha256(raw).hexdigest();previous=self.store.get(identity,'draft',stored_key)
        identifier=attachment.get('upload_operation_id') or ((previous or {}).get('upload_operation_id') if (previous or {}).get('sha256')==checksum else None) or str(uuid4())
        try:
            if str(UUID(identifier))!=identifier:raise ValueError
        except (ValueError,TypeError):raise LocalDataError('validation','附件上传编号无效。') from None
        value={'name':name,'media_type':media,'data_base64':encoded,'size_bytes':len(raw),
            'kind':'image' if media.startswith('image/') else 'document','sha256':checksum,
            'upload_operation_id':identifier,'updated_at':_now()}
        if type(attachment.get('file_id'))is int and attachment['file_id']>0:value['file_id']=attachment['file_id']
        self._guard(identity);self.store.put(identity,'draft',stored_key,value);return deepcopy(value)

    def handle(self,action,body,operation_id=None):
        identity=self.identity()
        if not isinstance(body,dict):raise LocalDataError('validation','请求内容无效。')
        actor=body.get('_fawnsee_actor_id',identity.actor_id)
        if type(actor) is not int or actor!=identity.actor_id:raise LocalDataError('account_changed','账号已变化，请重新打开页面。')
        clean={key:value for key,value in body.items() if key!='_fawnsee_actor_id'}
        if set(clean)&_FORBIDDEN:raise LocalDataError('permission','请求不能指定其他账号或本机路径。')
        if action=='member.memory.conversation.status':
            if clean:raise LocalDataError('validation','整理进度不接受额外参数。')
            # A progress GET must not enter cache/observe/_ask_sync: any of
            # those can schedule paid background work. Pending privacy edits
            # also must not reveal the older cloud's coverage counters.
            pending=self.store.operations(identity)
            privacy=any(row['action']in {'member.memory.forget','member.memory.controls.update'}
                or row['action']=='preferences.update'and row['payload'].get('ai_personal_context_consent')is False
                for row in pending)
            if privacy:
                values=self._project(identity,{'preferences','memory_settings'})
                settings=values.get('memory_settings',{});prefs=values.get('preferences',{})
                enabled=(settings.get('enabled')is True and prefs.get('mobile_ai_personal_enabled')is True
                    and any(row.get('domain')=='ai_records'and row.get('enabled_for_ai')is True for row in settings.get('controls',[])))
                self._guard(identity)
                return {'ok':True,'result':{'schema_version':1,'enabled':enabled,'state':'waiting'if enabled else 'disabled',
                    'reason':'privacy_sync_pending','messages':{key:None for key in('eligible_older','summarized','pending','recent_direct')},
                    'search':{key:None for key in('eligible_user','indexed_user','pending_user')},'updated_at':None,'retry_after_seconds':60}}
            result=self.online_handler(action,clean,operation_id);self._guard(identity);return result
        if action=='chat.greeting.ensure' and clean.get('boot_id') is not None:
            self._profile_before_boot_greeting(identity)
        if action=='onboarding.get' and not clean:
            completed=cached_onboarding11830(self,identity)
            if completed is not None:
                self._ask_sync()
                return {'ok':True,'result':completed,'_local149':self._mark(identity)}
        if action in WRITE_RESOURCES:
            result=self._write(identity,action,clean,operation_id)
            if action=='reading.social.set' and clean.get('kind')=='follow':self.store.invalidate_action_cache(identity,'reading.recommendations')
            if action in READING_SOCIAL_WRITES:
                social,found=read_reading_social(self,identity,'reading.social.get',{'article_id':clean['article_id']},self._project(identity,{'reading_social','reading'}))
                if found:result={**result,**social,**({'comment':result['comment']} if 'comment' in result else {})}
            self._ask_sync(force=True)
            return {'ok':True,'result':result,'_local149':self._mark(identity)}
        if action in READ_ACTIONS:
            resources=self._project(identity,_projection_resources(action))
            value,found=self._read(identity,action,clean,resources)
            if found:
                if action=='member.ai-profile.categories.get':
                    # The state endpoint intentionally only reads a snapshot.
                    # The archive route starts eligible summaries and polls
                    # their public status, so keep it in the normal worker.
                    self._queue_read_refresh(identity,action,clean)
                else:self._ask_sync()
                return {'ok':True,'result':_plain(value),'_local149':self._mark(identity)}
            key=[action,clean];cached=self.store.get(identity,'cache',key)
            if cached is not None and action=='chat.messages.list':
                repaired=_bounded_chat_page(cached,clean.get('limit',40))
                if repaired is None:self.store.delete(identity,'cache',key);cached=None
                elif repaired!=cached:self._guard(identity);self.store.put(identity,'cache',key,repaired);cached=repaired
            if cached is not None and action=='preferences.get':
                # Earlier versions could cache a valid GET before failing to
                # seed its newer schema. Repair that local read without a new
                # network dependency; malformed old cache is fetched afresh.
                try:self._seed_resource(identity,action,cached)
                except ValueError:self.store.delete(identity,'cache',key);cached=None
            if cached is not None:
                self._queue_read_refresh(identity,action,clean)
                return {'ok':True,'result':cached,'_local149':self._mark(identity)}
            result=self.online_handler(action,body,operation_id)
            self._guard(identity)
            if isinstance(result,dict) and result.get('ok'):
                # Validate/seed before caching. A rejected read must not become
                # a successful cache hit on the next attempt.
                self._seed_resource(identity,action,result.get('result'))
                self.store.put(identity,'cache',key,result.get('result'))
            return result
        result=self.online_handler(action,body,operation_id)
        self._guard(identity)
        self.observe(action,clean,result,identity=identity)
        return result

    def observe(self,action,body,response,*,identity=None):
        identity=identity or self.identity()
        if not isinstance(response,dict) or response.get('ok')is not True:return
        result=response.get('result')
        if action in ONBOARDING_ACTIONS11830:
            observe_onboarding11830(self,identity,action,result)
            return
        if action=='chat.greeting.ensure' and isinstance(result,dict):
            message=result.get('message')
            identifier=body.get('conversation_id')
            if isinstance(message,dict) and type(identifier)is int and identifier>0:
                key=['chat.messages.list',{'conversation_id':identifier,'limit':40}]
                page=self.store.get(identity,'cache',key)
                if isinstance(page,dict):
                    page=deepcopy(page);rows=page.get('messages',page.get('items',[]))
                    if not any(row.get('id')==message.get('id') for row in rows):
                        rows.append(deepcopy(message));rows.sort(key=lambda row:row.get('sequence_no',0))
                    # Greeting responses own the startup fold snapshot. Store
                    # it before exposing the appended opening to offline reads;
                    # stale groups must not overlap a newly extended interval.
                    from ollama_chat_app.services.first_meet_greeting import annotate_welcome_rows,welcome_groups_from_rows
                    groups=result.get('welcome_groups')
                    if not isinstance(groups,list)or not groups:
                        # Older servers did not return startup metadata. The
                        # known local rows still provide a lossless fallback.
                        groups=welcome_groups_from_rows(rows)
                    visible={row['id']for row in rows};scoped=[]
                    for group in groups:
                        if not isinstance(group,dict)or not isinstance(group.get('message_ids'),list):continue
                        ids=[item for item in group['message_ids']if item in visible]
                        if ids:scoped.append({**group,'message_ids':ids})
                    page['messages']=annotate_welcome_rows(rows,scoped);page['welcome_groups']=scoped
                    bounded=_bounded_chat_page(page)
                    if bounded is None:self.store.delete(identity,'cache',key)
                    else:self.store.put(identity,'cache',key,bounded)
            return
        if action=='chat.send' and isinstance(result,dict):
            conversation=_plain(result.get('conversation') or {})
            identifier=conversation.get('id')
            if type(identifier)is not int or identifier<=0:return
            key=['chat.messages.list',{'conversation_id':identifier,'limit':40}]
            page=self.store.get(identity,'cache',key,{'conversation':conversation,'messages':[],'has_more':False,'next_cursor':None})
            rows=page if isinstance(page,list) else page.get('messages',page.get('items',[]))
            rows=list(rows);seen={row.get('id') for row in rows}
            user=_plain(result.get('user_message') or {});assistant=_plain(result.get('assistant_message') or {})
            if type(user.get('sequence_no'))is int:assistant.setdefault('sequence_no',user['sequence_no']+1)
            for row in (user,assistant):
                if type(row.get('id'))is int and row['id']>0 and row['id']not in seen:
                    row.setdefault('created_at',_now());row.setdefault('conversation_id',identifier);rows.append(row)
            has_more=(not isinstance(page,list) and page.get('has_more')is True) or len(rows)>40
            rows=rows[-40:];cursor=rows[0].get('sequence_no') if has_more and rows else None
            # A cursor must be real sequence metadata, never an inferred row ID.
            if has_more and type(cursor)is not int:return
            from ollama_chat_app.services.first_meet_greeting import annotate_welcome_rows
            groups={group['group_id']:group for group in page.get('welcome_groups',[])if isinstance(group,dict)and isinstance(group.get('group_id'),str)} if isinstance(page,dict)else{}
            groups.update({group['group_id']:group for group in result.get('welcome_groups',[])if isinstance(group,dict)and isinstance(group.get('group_id'),str)})
            visible={row['id']for row in rows};scoped=[]
            for group in groups.values():
                ids=[item for item in group['message_ids']if item in visible]
                if ids:scoped.append({**group,'message_ids':ids})
            self.store.put(identity,'cache',key,{'conversation':conversation,'messages':annotate_welcome_rows(rows,scoped),'welcome_groups':scoped,'has_more':has_more,'next_cursor':cursor})
            list_key=['chat.conversations.list',{}]
            old=self.store.get(identity,'cache',list_key,[])
            values=old if isinstance(old,list) else old.get('items',[])
            values=[row for row in values if row.get('id')!=identifier]
            self.store.put(identity,'cache',list_key,[conversation,*values])
        elif action in {'chat.tools.confirm','chat.tools.cancel'}:
            if action=='chat.tools.confirm':self._adopt_confirmed_record(identity,result)
            self.store.invalidate_cache(identity,'chat.tools.')
            self._ask_sync(force=True)
        elif action.startswith(('chat.conversations.delete','chat.conversations.rename','chat.conversations.create')):
            self.store.invalidate_cache(identity,'chat.')
        elif action.startswith('forum.') and action not in READ_ACTIONS:self.store.invalidate_cache(identity,'forum.')
        elif action.startswith('shopping.') and action not in READ_ACTIONS:self.store.invalidate_cache(identity,'shopping.')
        elif action.startswith(('service.','member.appointments.')) and action not in READ_ACTIONS:self.store.invalidate_cache(identity,'service.')

    def _adopt_confirmed_record(self,identity,result):
        """Project an authoritative saved entity without inventing a resource CAS.

        Legacy receipts still trigger normal reconciliation. A verified inline
        record belongs only to its captured member; it is an overlay until a
        later complete snapshot, before the member's queued local edits.
        """
        if not isinstance(result,dict) or result.get('kind')!='life_record' or result.get('status')!='saved':return
        if result.get('persistence_verified')is not True:return
        entity=result.get('result_entity');identifier=(result.get('result')or{}).get('id') if isinstance(result.get('result'),dict) else None
        required={'id','user_id','category','occurred_at','content','details','source','created_at','updated_at','version'}
        if (type(result.get('actor_id'))is not int or result['actor_id']!=identity.actor_id
                or not isinstance(entity,dict) or not required.issubset(entity)
                or type(identifier)is not int or identifier<=0 or type(entity.get('id'))is not int or entity['id']!=identifier
                or type(entity.get('user_id'))is not int or entity['user_id']!=identity.actor_id):
            raise LocalDataError('invalid_response','已保存结果与当前账号不匹配，请重新查看记录。')
        HealthService._validate_category(entity['category'])
        HealthService._normalize_local_timestamp(entity['occurred_at'])
        HealthService._clean_required_text(entity['content'],'记录内容',maximum=10000)
        HealthService._validate_record_details(entity['category'],entity['details'])
        if (entity['source']not in {'manual','import','device','ai_confirmed'} or type(entity['version'])is not str
                or not 1<=len(entity['version'])<=256):raise LocalDataError('invalid_response','已保存记录的格式无效，请重新查看记录。')
        allowed=required|{'local_date','timezone_offset_minutes','linked_meal_id'}
        row={key:deepcopy(value)for key,value in entity.items()if key in allowed}
        token=digest({'proposal_id':result.get('proposal_id'),'id':identifier,'version':row['version']})
        with self._record_receipt_lock:
            self._guard(identity)
            seen=self.store.get(identity,'meta','confirmed-record-receipts',{})
            # A completed snapshot may prove that this previously saved entity
            # was later deleted. Replaying its older native receipt must not
            # resurrect that row in the local UI.
            if token in seen:return
            values=self.store.get(identity,'meta','confirmed-records',{})
            values[str(identifier)]={'token':token,'entity':row}
            self.store.put(identity,'meta','confirmed-records',values)
            seen[token]={'id':identifier,'version':row['version']}
            if len(seen)>4096:seen=dict(list(seen.items())[-4096:])
            self.store.put(identity,'meta','confirmed-record-receipts',seen)
            self._guard(identity)

    def _confirmed_records(self,identity):
        with self._record_receipt_lock:return self.store.get(identity,'meta','confirmed-records',{})

    def _reconcile_confirmed_records(self,identity,captured):
        with self._record_receipt_lock:
            self._guard(identity);current=self.store.get(identity,'meta','confirmed-records',{})
            remaining={key:value for key,value in current.items()if captured.get(key)!=value.get('token')}
            if remaining:self.store.put(identity,'meta','confirmed-records',remaining)
            else:self.store.delete(identity,'meta','confirmed-records')

    def _refresh_read_cache(self,identity):
        refreshed=0;failed=0
        if self.background_handler is None:return {'refreshed_reads':0,'read_refresh_failures':0}
        for row in self.store.list_values(identity,'refresh',8):
            value=row['value'];action=value.get('action');body=value.get('payload')
            if action not in READ_ACTIONS or action in NO_BACKGROUND_READS or not isinstance(body,dict):
                self.store.delete(identity,'refresh',row['key']);continue
            self._guard(identity)
            result=self.background_handler(action,body,str(uuid4()),_scope(identity));self._guard(identity)
            if isinstance(result,dict) and result.get('ok')is True:
                if action=='member.ai-profile.categories.get':
                    # Resource reads project the authoritative state rather
                    # than the generic cache. Fetch its version as well, so a
                    # completed summary becomes visible without losing CAS.
                    current=self._fetch_resource(identity,'ai_preferences')
                    self._guard(identity)
                    self.store.put(identity,'resource','ai_preferences',current)
                self.store.put(identity,'cache',row['key'],result.get('result'))
                if action=='reading.comments':self._seed_resource(identity,action,result.get('result'))
                self.store.delete(identity,'refresh',row['key']);refreshed+=1
            else:
                code=(result.get('error') or {}).get('code') if isinstance(result,dict) else 'protocol'
                if code in {'authentication','not_authenticated','permission'}:raise LocalDataError(code,'登录授权已失效，请重新登录。')
                if code in {'not_found','gone'}:
                    self.store.delete(identity,'cache',row['key']);self.store.delete(identity,'refresh',row['key'])
                failed+=1
        return {'refreshed_reads':refreshed,'read_refresh_failures':failed}

    def _seed_resource(self,identity,action,value):
        if action=='reading.comments' and isinstance(value,dict):
            summaries=self.store.get(identity,'meta','reading-comment-summaries',{})
            summaries.update({row['id']:row for row in value.get('items',[]) if isinstance(row,dict) and row.get('id')})
            summaries=bounded_comments(summaries,compact=True)
            self.store.put(identity,'meta','reading-comment-summaries',summaries)
        resource=RESOURCE_READS.get(action)
        # A filtered/paginated response is only a read cache, never a complete
        # authoritative resource. Initial full resources arrive through state.
        if resource not in {'profile','preferences','reminders','ai_preferences'}:return
        if self._resource(identity,resource) is not None:return
        if resource=='preferences':value=_preference_snapshot(value)
        self.store.put(identity,'resource',resource,{'value':value,'version':None,'complete':False})

    def write_record_assistant(self,identity,action,body,identifier,target,draft):
        resource=WRITE_RESOURCES[action]
        if target is not None:
            snapshot=self._resource(identity,resource)
            if snapshot is None or snapshot.get('complete') is not True:
                fresh=self._fetch_resource(identity,resource)
                self._guard(identity)
                if self._resource(identity,resource)==snapshot:
                    self.store.put(identity,'resource',resource,fresh)
        self._write(identity,action,body,identifier,record_draft=draft)
        self._ask_sync(force=True)

    @staticmethod
    def _preserve_record_assistant_fields(resources,resource,body,draft,actor):
        target=draft['target']
        int_id=target['id']
        row=next((item for item in _items(resources[resource]) if item.get('id')==int_id),None)
        if (row is None or row.get('user_id')!=actor or row.get('version')!=target['version']):
            raise LocalDataError('conflict','这条记录已变更或移除，请重新读取后再请助手整理。')
        if row.get('linked_meal_id'):
            raise LocalDataError('validation','这条记录关联餐食明细，请到餐食明细里修改。')
        supplement=draft['operation']=='supplement'
        if resource=='observations':
            if row.get('kind')!=draft['details']['kind']:
                raise LocalDataError('validation','修改时须保留原身体指标类型。')
            if supplement:
                for field in ('value','unit','context','notes','diastolic','metric_name'):
                    value=body.get(field)
                    if field in body and row.get(field) not in (None,'') and row[field]!=value:
                        raise LocalDataError('validation','补充资料须保留原有内容，请在对话中明确提出修改。')
            return body
        changes=body['changes']
        if row.get('category')!=changes['category']:
            raise LocalDataError('validation','修改时须保留原记录类别。')
        existing=row.get('details') or {}
        references=changes['details'].get('photo_file_ids')
        prior_photos=existing.get('photo_file_ids') or []
        if references is not None and references[:len(prior_photos)]!=prior_photos:
            raise LocalDataError('validation','请保留原记录的照片后再确认。')
        if supplement:
            for field,value in changes['details'].items():
                if field!='photo_file_ids' and existing.get(field) not in (None,'') and existing[field]!=value:
                    raise LocalDataError('validation','补充资料须保留原有内容，请在对话中明确提出修改。')
        return {**body,'changes':{**changes,'details':{**existing,**changes['details']}}}

    def _write(self,identity,action,body,operation_id,*,record_draft=None):
        identifier=str(UUID(operation_id or body.get('request_id') or str(uuid4())))
        resource=WRITE_RESOURCES[action]
        resources=self._project(identity,_projection_resources(action,write=True))
        if resource not in resources:
            # New members can start appending records without a first snapshot;
            # updates need the prior state for conflict-safe editing.
            if action not in APPENDS:raise LocalDataError('local_not_ready','请先联网读取此类资料，之后即可离线编辑。')
            resources[resource]=({} if resource=='reading_social' else {'items':[]} if resource in {'goals','observations'} else
                {'enabled':False,'revision':0,'controls':[]} if resource=='memory_settings' else [])
        if record_draft is not None and record_draft['target'] is not None:
            body=self._preserve_record_assistant_fields(resources,resource,body,record_draft,identity.actor_id)
        local_id=(str(uuid5(UUID(identifier),'record-assistant-entity')) if record_draft is not None
            else str(uuid4())) if action in CREATE_IDS else None
        operation={'operation_id':identifier,'action':action,'resource':resource,'payload':deepcopy(body),
            'base_version':None if action in APPENDS else self._base_version(identity,resource),
            'local_id':local_id,'local_number':_local_number(local_id) if local_id else None,
            'created_at':_now(),'request_digest':digest([action,body]),
            'base_fingerprint':digest((self._resource(identity,resource) or {}).get('value'))}
        if _library_only(operation):
            operation['library_intent']=_library_intent(resources['preferences'].get('diet_library_v1'),body['diet_library_v1'])
        if action in READING_SOCIAL_WRITES:
            check_reading_social(action,body)
            cached=self.store.get(identity,'cache',['reading.social.get',{'article_id':body['article_id']}],{})
            operation['reading_meta']={key:deepcopy(cached[key]) for key in ('publisher','title') if key in cached}
            for catalog_action in ('reading.get','reading.catalog'):
                if 'title' not in operation['reading_meta'] and catalog_action=='reading.get':
                    article=self.store.get(identity,'cache',[catalog_action,{'article_id':body['article_id']}],{})
                    if article.get('title'):operation['reading_meta']['title']=article['title']
            operation['reading_author']=str((resources.get('profile') or {}).get('display_name') or '我')
            operation['reading_avatar']=str((resources.get('preferences') or {}).get('avatar_data') or '')
            if action=='reading.comment.like':
                summaries=self.store.get(identity,'meta','reading-comment-summaries',{})
                confirmed=self.store.get(identity,'meta','reading-confirmed-comments',{})
                operation['reading_comment']=deepcopy(summaries.get(body['comment_id'],confirmed.get(body['comment_id'],{})))
        existing=self.store.operation(identity,identifier)
        if existing:
            if existing['request_digest']!=operation['request_digest']:raise LocalDataError('conflict','同一操作编号不能保存不同内容。')
            if resource=='goals' and action!='expansion.goals.delete':
                deleted=self._deleted_goal_receipt(identity,existing)
                if deleted is not None:return deleted
            if existing['status'] in {'applied','confirmed'}:return self._stable_goal_aliases(identity,resource,existing.get('result'))
            return self._stable_goal_aliases(identity,resource,existing.get('local_result'))
        # Validate and project before committing an intent. No success is
        # returned until the encrypted SQLite transaction is durable.
        pending=self.store.operations(identity)
        parent=next((row for row in reversed(pending) if row['resource']==resource),None)
        if parent:operation['depends_on']=parent['operation_id']
        if action=='member.memory.controls.update':
            old={row.get('domain'):row for row in (resources.get('memory_settings') or {}).get('controls',[])}
            operation['safety_shrink']=all(not any(patch.get(key) is True and old.get(patch.get('domain'),{}).get(key) is not True
                for key in ('enabled_for_ai','collect_events')) for patch in body.get('controls',[]))
            if operation['safety_shrink']:operation['base_version']=None
        if action in {'expansion.records.update','expansion.records.delete'}:
            record_id=body.get('record_id');mapping=(resources.get('_local149_mappings') or {}).get(str(record_id))
            ids={record_id,mapping['id'] if mapping else record_id}
            before=next((row for row in _items(resources.get('life_records')) if row.get('id')in ids),None)
            if before is not None:operation['local_before']=deepcopy(before)
        result=self._stable_goal_aliases(identity,resource,self._apply(resources,operation,identity.actor_id))
        operation['local_result']=_plain(result)
        self._guard(identity)
        cancelled={
            'health.records.delete':(body.get('record_ids'), 'health.records.add'),
            'expansion.records.delete':([body.get('record_id')], 'health.records.add'),
            'health.reminders.delete':(body.get('reminder_ids'), 'health.reminders.add'),
            'expansion.observations.delete':([body.get('observation_id')], 'expansion.observations.create'),
            'expansion.goals.delete':([body.get('goal_id')], 'expansion.goals.create'),
        }.get(action)
        if cancelled and self.store.cancel_unsent_creates(identity,operation,*cancelled):
            if action=='expansion.goals.delete':self._remember_goal_deletion(identity,result,operation)
            return result
        self.store.enqueue(identity,operation)
        if action=='expansion.goals.delete':
            self._remember_goal_deletion(identity,result,operation)
            self.store.invalidate_cache(identity,'expansion.goals.')
            self.store.invalidate_cache(identity,'expansion.growth.')
            self.store.invalidate_cache(identity,'expansion.points.')
        if action=='member.memory.forget':
            for prior in pending:
                if prior['resource'] in {'ai_preferences','ai_profile'} and not prior['attempted']:
                    self.store.change_operation(identity,prior['operation_id'],status='discarded')
            self.store.clear_cache(identity)
        return result

    def _deleted_goal_receipt(self,identity,operation):
        entity=operation.get('result_entity')or operation.get('result')or operation.get('local_result')
        if isinstance(entity,dict):entity=entity.get('goal',entity)
        candidates=[operation.get('local_number'),operation.get('payload',{}).get('goal_id')]
        if isinstance(entity,dict):candidates.extend((entity.get('id'),entity.get('goal_id'),entity.get('local_number')))
        for identifier in candidates:
            if type(identifier)is int:
                marker=self.store.get(identity,'meta',['deleted-goal',identifier])
                if isinstance(marker,dict):return deepcopy(marker)
        return None

    def _remember_goal_deletion(self,identity,result,operation):
        if not isinstance(result,dict)or result.get('deleted')is not True:return
        identifiers={identifier for identifier in (result.get('goal_id'),result.get('local_number'),operation.get('local_number'),operation.get('payload',{}).get('goal_id'))if type(identifier)is int}
        mappings=self.store.get(identity,'mapping','entity_ids',{})
        for local,mapped in mappings.items():
            if mapped.get('resource')=='goals'and (int(local)in identifiers or mapped.get('id')in identifiers):
                identifiers.update((int(local),mapped['id']))
        positive=next((value for value in identifiers if value>0),None)
        negative=next((value for value in identifiers if value<0),None)
        marker={'deleted':True,'goal_id':positive if positive is not None else negative,'deleted_count':0,'reminder_ids':[]}
        if negative is not None:marker['local_number']=negative
        for identifier in identifiers:self.store.put(identity,'meta',['deleted-goal',identifier],marker)

    def _stable_goal_aliases(self,identity,resource,result):
        """Return the same device key for lists, immediate acks and replayed acks."""
        result=deepcopy(result)
        if resource!='goals' or not isinstance(result,dict):return result
        goal=result.get('goal',result)
        if not isinstance(goal,dict):return result
        identifier=goal.get('id',goal.get('goal_id'))
        mappings=self.store.get(identity,'mapping','entity_ids',{})
        for local,mapped in mappings.items():
            if mapped.get('resource')=='goals' and mapped.get('id')==identifier:
                goal['local_number']=int(local);break
        return result

    def _project(self,identity,names=None):
        selected=None if names is None else frozenset(names)
        if selected is not None and not selected.issubset(RESOURCES):raise ValueError('Unknown local projection')
        values=self._resources(identity,selected)
        confirmed=self._confirmed_records(identity) if selected is None or 'life_records'in selected else {}
        if confirmed:
            records=values.setdefault('life_records',[]);rows=_items(records)
            incoming={value['entity']['id']:value['entity']for value in confirmed.values()}
            rows[:]=[row for row in rows if row.get('id')not in incoming]
            rows.extend(deepcopy(list(incoming.values())))
        values['_local149_mappings']=self.store.get(identity,'mapping','entity_ids',{})
        for operation in _library_projection_order(self.store.operations(identity)):
            affected={operation['resource']}
            if operation['resource']=='goals':affected.add('reminders')
            if operation['action']=='member.memory.forget':affected.update(('ai_preferences','ai_profile'))
            if selected is not None and not affected.intersection(selected):continue
            if operation['status'] in {'queued','uncertain','conflict','rejected','confirmed'}:
                if operation['resource'] not in values:
                    values[operation['resource']]=({} if operation['resource']=='reading_social' else {'items':[]} if operation['resource'] in {'goals','observations'} else
                        {'enabled':False,'revision':0,'controls':[]} if operation['resource']=='memory_settings' else [])
                try:
                    changed={operation['resource']}
                    if operation['resource']=='goals':changed.add('reminders')
                    if operation['action']=='member.memory.forget':changed.update(('ai_preferences','ai_profile'))
                    trial=dict(values)
                    for name in changed:
                        if name in values:trial[name]=deepcopy(values[name])
                    intent=_captured_library(operation)if _library_only(operation)else None
                    if intent:
                        projected={**operation,'payload':{'diet_library_v1':_merge_library(trial['preferences'].get('diet_library_v1'),intent)}}
                    else:projected=operation
                    self._apply(trial,projected,identity.actor_id)
                    entity=operation.get('result_entity')
                    if operation['status']=='confirmed' and isinstance(entity,dict) and type(entity.get('id'))is int and entity['id']>0:
                        rows=_items(trial.get(operation['resource']))
                        rows[:]=[row for row in rows if row.get('id')not in {entity['id'],operation.get('local_number')}]
                        rows.append(deepcopy(entity))
                    values=trial
                except (ValueError,LookupError,RuntimeError):
                    # A cloud conflict retains both bodies; it must not make all
                    # other locally stored records unreadable.
                    continue
        return values

    @staticmethod
    def _apply(resources,operation,actor):
        action=operation['action'];body=deepcopy(operation['payload']);name=operation['resource']
        if name=='reading_social':
            value=resources[name]
            if not isinstance(value,dict):raise ValueError('阅读缓存格式无效。')
            aid=body.get('article_id')
            if aid:
                own=value.setdefault('articles',{}).setdefault(aid,{})
                own.update(operation.get('reading_meta',{}))
                legacy=resources.get('reading',{})
                prior=(legacy.get(aid) if isinstance(legacy,dict) else next((row for row in legacy if row.get('content_id')==aid),None))
                if prior and str(prior.get('updated_at') or '')>=str(own.get('updated_at') or ''):
                    own.update(saved=bool(prior.get('saved')),read=prior.get('progress')==1)
                    own['saved_at']=(own.get('saved_at') or prior.get('updated_at')) if own['saved'] else None
            value['_author']=operation.get('reading_author','我');value['_avatar']=operation.get('reading_avatar','')
            return project_reading_social(value,operation,actor)
        value=resources[name];now=operation['created_at'];local_number=operation.get('local_number')
        def mark(row,*,changed=True):
            if changed:row['updated_at']=now
            row['sync_status']=operation.get('status','queued');row['local_only']=True
            if operation.get('local_id'):row['local_id']=operation['local_id']
            return row
        def find(rows,identifier):
            if type(identifier) is not int or identifier==0:raise ValueError('记录编号无效。')
            result=next((row for row in rows if row.get('id')==identifier),None)
            if result is None:
                mapped=(resources.get('_local149_mappings') or {}).get(str(identifier))
                if mapped:result=next((row for row in rows if row.get('id')==mapped['id']),None)
            if result is None:raise ValueError('此记录尚未在本机保存，请先联网读取。')
            return result
        def version(row):
            row['version']=digest({key:item for key,item in row.items() if key not in {'version','sync_status','local_only','local_id','entries','progress','reminder_status'}})
            return row
        if action=='health.profile.save':
            clean=HealthService.normalize_profile_patch(body)
            value.update(clean);value['updated_at']=now
            return None
        if action=='preferences.update':
            clean=_preference_patch(body);resources[name]={**value,**clean};return _preferences(resources[name])
        if name=='life_records':
            rows=_items(value)
            if action=='health.records.add':
                if set(body)-{'category','occurred_at','content','source','details'}:raise ValueError('记录中有不支持的字段。')
                category=HealthService._validate_category(body.get('category'))
                stamp,day,offset=HealthService._normalize_local_timestamp(body.get('occurred_at'))
                content=HealthService._clean_required_text(body.get('content'),'记录内容',maximum=10000)
                source=body.get('source','manual')
                if source not in {'manual','import','device','ai_confirmed'}:raise ValueError('记录来源无效。')
                details=HealthService._validate_record_details(category,body.get('details'))
                row=version(mark({'id':local_number,'user_id':actor,'category':category,'occurred_at':stamp,'local_date':day,
                    'timezone_offset_minutes':offset,'content':content,'source':source,'details':details,'created_at':now,'updated_at':now,'linked_meal_id':None}))
                rows.append(row);return deepcopy(row)
            identifiers=body.get('record_ids') if action=='health.records.delete' else [body.get('record_id')]
            if type(identifiers) is not list or not identifiers or len(identifiers)>200:raise ValueError('请选择要操作的记录。')
            targets=[find(rows,ident) for ident in identifiers]
            if action in {'health.records.delete','expansion.records.delete'}:
                for row in targets:rows.remove(row)
                return {'deleted':True,'deleted_count':len(targets),'record_ids':identifiers}
            row=targets[0];changes=body.get('changes')
            if not isinstance(changes,dict) or set(changes)-{'category','occurred_at','content','details'}:raise ValueError('记录修改内容无效。')
            if row.get('linked_meal_id'):raise ValueError('此记录关联餐食明细，请联网在原明细中修改。')
            category=HealthService._validate_category(changes.get('category',row['category']))
            stamp,day,offset=HealthService._normalize_local_timestamp(changes.get('occurred_at',row['occurred_at']))
            row.update(category=category,occurred_at=stamp,local_date=day,timezone_offset_minutes=offset,
                content=HealthService._clean_required_text(changes.get('content',row['content']),'记录内容',maximum=10000),
                details=HealthService._validate_record_details(category,changes.get('details',row.get('details'))))
            return deepcopy(version(mark(row)))
        if name=='reminders':
            rows=_items(value)
            if action=='health.reminders.add':
                if set(body)-{'title','scheduled_at','reminder_type','repeat_rule'}:raise ValueError('提醒内容无效。')
                title=HealthService._clean_required_text(body.get('title'),'提醒标题',maximum=200)
                kind=HealthService._validate_reminder(title,body.get('reminder_type','custom'))
                stamp=timestamp_to_db(HealthService._reminder_datetime(body.get('scheduled_at')))
                repeat=HealthService._validate_repeat_rule(body.get('repeat_rule'))
                row=mark({'id':local_number,'user_id':actor,'title':title,'reminder_type':kind,'scheduled_at':stamp,
                    'status':'active','enabled':True,'repeat_rule':repeat,'paused_until':None,'created_at':now,'updated_at':now})
                rows.append(row);return deepcopy(row)
            identifiers=body.get('reminder_ids') if action=='health.reminders.delete' else [body.get('reminder_id')]
            if type(identifiers) is not list or not identifiers or len(identifiers)>200:raise ValueError('请选择要操作的提醒。')
            targets=[find(rows,identifier) for identifier in identifiers]
            if action=='health.reminders.delete':
                for row in targets:rows.remove(row)
                return {'deleted_count':len(targets),'reminder_ids':identifiers}
            row=targets[0]
            if action=='health.reminders.toggle':
                enabled=body.get('enabled')
                if enabled is not None and type(enabled) is not bool:raise ValueError('提醒开关无效。')
                enabled=(row.get('status')!='active') if enabled is None else enabled
                row.update(status='active' if enabled else 'disabled',enabled=enabled)
            else:
                if set(body)-{'reminder_id','title','scheduled_at','reminder_type','repeat_rule'}:raise ValueError('提醒修改内容无效。')
                title=HealthService._clean_required_text(body.get('title',row['title']),'提醒标题',maximum=200)
                kind=HealthService._validate_reminder(title,body.get('reminder_type',row.get('reminder_type','custom')))
                row.update(title=title,reminder_type=kind)
                if 'scheduled_at' in body:row['scheduled_at']=timestamp_to_db(HealthService._reminder_datetime(body['scheduled_at']))
                if 'repeat_rule' in body:row['repeat_rule']=HealthService._validate_repeat_rule(body['repeat_rule'])
            return deepcopy(mark(row))
        if name in {'goals','observations'}:
            rows=_items(value)
            if action.endswith('.create'):
                row={'id':local_number,'user_id':actor,'created_at':now,'updated_at':now}
            else:row=find(rows,body.get('goal_id' if name=='goals' else 'observation_id'))
            if name=='observations':
                allowed={'observation_id','base_version','reason'} if action.endswith('.delete') else (
                    {'kind','value','unit','measured_at','context','notes','report_item_id','diastolic','metric_name'} if action.endswith('.create') else
                    {'observation_id','base_version','value','unit','measured_at','context','notes','reason','diastolic','metric_name'})
                required={'kind','value','unit','measured_at'} if action.endswith('.create') else {'observation_id','base_version','reason'}
                if set(body)-allowed or not required<=set(body):raise ValueError('身体指标字段无效。')
                if not action.endswith('.create'):_text(body.get('reason'),500)
                if action.endswith('.delete'):
                    rows.remove(row);return {'deleted':True,'observation_id':row['id']}
                source={**row,**body};kind=source.get('kind')
                if kind not in {'weight','glucose','blood_pressure','lipids','height','custom'}:raise ValueError('身体指标类型无效。')
                unit=_text(source.get('unit'),30,optional=kind=='custom') or ''
                units={'weight':{'kg'},'glucose':{'mmol/L','mg/dL'},'blood_pressure':{'mmHg'},'lipids':{'mmol/L','mg/dL'},'height':{'cm'}}
                if kind in units and unit not in units[kind]:raise ValueError('指标单位无效。')
                low,high={'weight':(.1,1000),'glucose':(.1,1000 if unit=='mg/dL' else 100),
                    'blood_pressure':(20,300),'lipids':(0,2000 if unit=='mg/dL' else 100),'height':(20,300),'custom':(-1000000,1000000)}[kind]
                measured_value=_number(source.get('value'),low,high)
                measured_date=_iso(source.get('measured_at'))
                if measured_date.year<1900:raise ValueError('身体指标日期无效。')
                measured=measured_date.isoformat()
                context=source.get('context','unspecified')
                if context not in {'fasting','before_meal','after_meal','random','unspecified'}:raise ValueError('测量情境无效。')
                if source.get('report_item_id') is not None:raise ValueError('关联报告的指标请联网确认后保存。')
                if kind=='blood_pressure':
                    diastolic=_number(source.get('diastolic'),10,200)
                    if diastolic>=measured_value:raise ValueError('请核对收缩压与舒张压。')
                    row['diastolic']=diastolic
                elif source.get('diastolic')is not None:raise ValueError('舒张压仅用于血压记录。')
                metric_name=_text(source.get('metric_name',''),100,optional=True)or ''
                if kind=='custom' and not metric_name:raise ValueError('请填写有效的指标名称。')
                if kind in {'custom','lipids'}or metric_name:row['metric_name']=metric_name
                row.update(kind=kind,value=measured_value,
                    unit=unit,measured_at=measured,context=context,notes=_text(source.get('notes',''),4000,optional=True) or '',report_item_id=None)
            else:
                if action.endswith('.delete'):
                    reminder_id=row.get('reminder_id');reminder_ids=[]
                    reminders=_items(resources.setdefault('reminders',[]))
                    if reminder_id is not None:
                        mappings=resources.get('_local149_mappings')or{}
                        mapped=mappings.get(str(reminder_id),{}).get('id')
                        aliases={reminder_id,mapped}
                        reminder_ids=[item['id']for item in reminders if item.get('id')in aliases]
                        reminders[:]=[item for item in reminders if item.get('id')not in aliases]
                    rows.remove(row)
                    if isinstance(value,dict):value['total']=len(rows)
                    return {'deleted':True,'goal_id':row['id'],'deleted_count':1,
                            'habit_entries_deleted':len(row.get('entries',[])),'reminder_ids':reminder_ids}
                if action.endswith(('.checkin','.uncheckin')):
                    day=body.get('day',_day())
                    try:parsed=datetime.strptime(day,'%Y-%m-%d').date()
                    except (ValueError,TypeError):raise ValueError('打卡日期无效。') from None
                    if day>_day():raise ValueError('不能提前打卡未来日期。')
                    if row.get('status','active')!='active':raise ValueError('目标已归档。')
                    entries=row.setdefault('entries',[])
                    if action.endswith('.uncheckin'):
                        entries[:]=[item for item in entries if item.get('day')!=day]
                    elif not any(item.get('day')==day for item in entries):
                        entries.append({'id':_local_number(operation['operation_id']),'day':day,'note':_text(body.get('note',''),1000,optional=True) or '', 'created_at':now})
                    row['progress']={'checkin_count':len(entries),'today_checked':any(item.get('day')==_day() for item in entries)}
                    mark(row,changed=False)
                    return {'goal':deepcopy(row),'points_awarded':0,'removed':action.endswith('.uncheckin')}
                if action.endswith('.close'):
                    row['status']='archived';LocalFirst149._goal_reminder(resources,row,operation,actor,paused=True)
                    return deepcopy(version(mark(row)))
                source={**row,**(body.get('changes') or body)}
                kind=source.get('kind')
                if kind not in {'water','diet','weight','glucose','reading','custom'}:raise ValueError('目标类型无效。')
                title=_text(source.get('title'),200);reminder=source.get('reminder_time')
                if reminder is not None and (not isinstance(reminder,str) or re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',reminder) is None):raise ValueError('提醒时间无效。')
                frequency=source.get('frequency','daily')
                if frequency not in {'daily','weekly'}:raise ValueError('目标频率无效。')
                row.update(title=title,kind=kind,target=_number(source.get('target'),.01,100000,optional=True),
                    unit=_text(source.get('unit'),30,optional=True),frequency=frequency,reminder_time=reminder,status='active')
                row.setdefault('entries',[]);row.setdefault('progress',{'checkin_count':0,'today_checked':False})
                LocalFirst149._goal_reminder(resources,row,operation,actor)
            if action.endswith('.create'):rows.append(row)
            if isinstance(value,dict):value['total']=len(rows)
            return deepcopy(version(mark(row)))
        if name=='reading':
            if set(body)-{'content_id','progress','saved'}:raise ValueError('阅读状态无效。')
            content=_text(body.get('content_id'),160);progress=_number(body.get('progress',0),0,1)
            if 'saved' in body and type(body['saved']) is not bool:raise ValueError('收藏状态无效。')
            row={'content_id':content,'progress':progress,'saved':body.get('saved',False),'updated_at':now,'sync_status':'pending'}
            if isinstance(value,list):
                old=next((item for item in value if item.get('content_id')==content),None)
                if old:old.update(row)
                else:value.append(row)
            else:value[content]={**value.get(content,{}),**row}
            return row
        if name=='ai_preferences':
            from mobile_memory import _category_request
            _category_request(action,body)
            rows=value.get('categories',[])
            if not isinstance(rows,list):raise ValueError('本机偏好档案格式无效，请先联网同步。')
            for patch in body['categories']:
                row=next((item for item in rows if item.get('domain')==patch['domain']),None)
                if row is None:raise ValueError('请先联网读取该类偏好。')
                if row.get('revision')!=patch['expected_revision']:raise ValueError('偏好已修改，请先重新读取。')
                row.update(text=patch['text'],revision=row['revision']+1,source='user',status='ready' if patch['text'] else 'empty',updated_at=now,sync_status='pending')
            return deepcopy(value)
        if name=='ai_profile':
            if set(body)!={'entries','expected_revision','request_id'} or type(body['expected_revision'])is not int or body['expected_revision']<0:
                raise ValueError('偏好保存版本无效。')
            if not isinstance(body['entries'],list) or len(body['entries'])>20:raise ValueError('偏好条目无效。')
            seen=set()
            for entry in body['entries']:
                if not isinstance(entry,dict) or set(entry)!={'id','text'} or str(UUID(entry['id']))!=entry['id'] or entry['id'] in seen:raise ValueError('偏好条目无效。')
                _text(entry['text'],2000);seen.add(entry['id'])
            value.update(entries=deepcopy(body['entries']),revision=body['expected_revision']+1,sync_status='pending')
            return deepcopy(value)
        if name=='memory_settings':
            from mobile_memory import CATEGORY_DOMAINS
            if action=='member.memory.forget':
                domains=body.get('domains')
                if set(body)!={'domains'} or not isinstance(domains,list) or not 1<=len(domains)<=11 or len(set(domains))!=len(domains) or any(domain not in CATEGORY_DOMAINS for domain in domains):raise ValueError('记忆类别无效。')
                for row in value.setdefault('controls',[]):
                    if row.get('domain') in domains:row['forget_before']=now
                for row in (resources.get('ai_preferences') or {}).get('categories',[]):
                    if row.get('domain') in domains:row.update(text='',status='empty',sync_status='pending')
                if 'preferences' in domains and isinstance(resources.get('ai_profile'),dict):resources['ai_profile']['entries']=[]
                value['revision']=digest([value,now])[:16]
                return {'business_records_deleted':False,'forgotten_domains':domains,'pending':True,'sync_status':'pending'}
            controls=body.get('controls')
            if set(body)!={'controls'} or not isinstance(controls,list) or not 1<=len(controls)<=11:raise ValueError('记忆设置无效。')
            seen=set();existing={row.get('domain'):row for row in value.setdefault('controls',[])}
            for patch in controls:
                if not isinstance(patch,dict) or set(patch)!={'domain','enabled_for_ai','collect_events'} or patch['domain']not in CATEGORY_DOMAINS or patch['domain']in seen or any(type(patch[key])is not bool for key in ('enabled_for_ai','collect_events')):raise ValueError('记忆设置无效。')
                seen.add(patch['domain']);existing[patch['domain']]={**existing.get(patch['domain'],{}),**patch}
            value.update(controls=list(existing.values()),revision=digest([value,controls])[:16],sync_status='pending')
            return deepcopy(value)
        raise ValueError('此操作暂不支持本机保存。')

    @staticmethod
    def _goal_reminder(resources,goal,operation,actor,paused=False):
        reminders=resources.setdefault('reminders',[]);rows=_items(reminders)
        row=next((item for item in rows if item.get('id')==goal.get('reminder_id')),None)
        if paused or not goal.get('reminder_time'):
            if row:row.update(status='disabled',enabled=False,sync_status='pending')
            goal['reminder_status']='disabled' if row else 'unset';return
        stamp=datetime.fromisoformat(_day()+'T'+goal['reminder_time']+':00+08:00')
        if stamp<=datetime.now(timezone(timedelta(hours=8))):stamp+=timedelta(days=1)
        if row is None:
            local=str(uuid5(NAMESPACE_URL,'fawnsee-goal-reminder:'+str(goal['id'])))
            row={'id':_local_number(local),'user_id':actor,'created_at':operation['created_at'],'local_id':local,'local_only':True}
            rows.append(row);goal['reminder_id']=row['id']
        title='目标：'+goal['title'][:197];HealthService._validate_reminder(title,'custom')
        row.update(title=title,reminder_type='custom',scheduled_at=stamp.isoformat(),repeat_rule=goal.get('frequency','daily'),
            status='active',enabled=True,updated_at=operation['created_at'],sync_status='pending')
        goal['reminder_status']='active'

    def _read(self,identity,action,body,resources):
        actor=identity.actor_id
        if action in READING_SOCIAL_READS:return read_reading_social(self,identity,action,body,resources)
        if action in {'health.dashboard','health.statistics'}:
            if 'life_records' not in resources:return None,False
            ram=_HealthRAM(actor,resources)
            try:
                health=HealthService(ram)
                result=health.get_dashboard(actor) if action=='health.dashboard' else health.get_record_statistics(actor,**body)
                return _plain(result),True
            finally:ram.close()
        name=RESOURCE_READS.get(action)
        if name is None or name not in resources:return None,False
        value=resources[name]
        if name in {'life_records','observations','goals','reminders'}:
            # A synchronized row keeps its real cloud id/version. Only this
            # device's read view carries the earlier local number so UI routes
            # and saved voice batches can retain their original stable keys.
            value=deepcopy(value)
            aliases={mapped['id']:int(local)for local,mapped in (resources.get('_local149_mappings')or {}).items()
                if mapped.get('resource')==name and type(mapped.get('id'))is int and mapped['id']>0}
            for row in _items(value):
                if row.get('id')in aliases:row['local_number']=aliases[row['id']]
            if name=='goals':
                reminder_aliases={mapped['id']:int(local)for local,mapped in (resources.get('_local149_mappings')or {}).items()
                    if mapped.get('resource')=='reminders'and type(mapped.get('id'))is int and mapped['id']>0}
                for row in _items(value):
                    if row.get('reminder_id')in reminder_aliases:row['reminder_local_number']=reminder_aliases[row['reminder_id']]
        if action=='health.profile.get':return value,True
        if action=='preferences.get':return _preferences(value),True
        if action=='health.reminders.list':return sorted(_items(value),key=lambda row:(row.get('status')!='active',row.get('scheduled_at',''),row.get('id',0))),True
        if action=='member.ai-profile.categories.get':return value,True
        if action in {'member.memory.settings.get','member.ai-profile.get'}:return value,True
        if action=='expansion.content.progress':
            content=body.get('content_id')
            rows=value if isinstance(value,dict) else {row.get('content_id'):row for row in value}
            return rows.get(content,{'content_id':content,'progress':0,'saved':False}),True
        if action in {'expansion.records.get','expansion.records.revisions','expansion.goals.get','expansion.observations.get'}:
            key='goal_id' if name=='goals' else 'observation_id' if name=='observations' else 'record_id'
            if set(body)-{key,'limit','offset'}:raise LocalDataError('validation','记录读取字段无效。')
            identifier=body.get(key)
            if type(identifier)is not int or identifier==0:raise LocalDataError('validation','记录编号无效。')
            mapping=(resources.get('_local149_mappings') or {}).get(str(identifier))
            identifiers={identifier,mapping.get('id') if mapping else identifier}
            selected=next((row for row in _items(value) if row.get('id')in identifiers),None)
            if selected is None:raise LocalDataError('not_found','此记录尚未在本机保存，请先联网读取。')
            if action=='expansion.records.revisions':return self._record_history(identity,body,selected,identifiers),True
            return selected,True
        rows=list(_items(value))
        if name=='life_records':
            if body.get('category') is not None:rows=[row for row in rows if row.get('category')==body['category']]
            if body.get('source') is not None:rows=[row for row in rows if row.get('source')==body['source']]
            time_field='occurred_at'
        elif name=='observations':
            if body.get('kind') is not None:rows=[row for row in rows if row.get('kind')==body['kind']]
            time_field='measured_at'
        elif name=='goals':
            if body.get('status') is not None:rows=[row for row in rows if row.get('status')==body['status']]
            time_field='created_at'
        else:return value,True
        for key in ('start_date','end_date'):
            if body.get(key) is not None:
                try:datetime.strptime(body[key],'%Y-%m-%d')
                except (ValueError,TypeError):raise ValueError('记录起止日期无效。') from None
                if key=='start_date':rows=[row for row in rows if _iso(row[time_field]).astimezone(timezone(timedelta(hours=8))).date().isoformat()>=body[key]]
                else:rows=[row for row in rows if _iso(row[time_field]).astimezone(timezone(timedelta(hours=8))).date().isoformat()<=body[key]]
        rows.sort(key=lambda row:(str(row.get(time_field) or ''),row.get('id',0)),reverse=True)
        default_limit=100 if action=='health.records.list' else 20
        limit=body.get('limit',default_limit);offset=body.get('offset',0)
        if type(limit) is not int or not 1<=limit<=1000 or type(offset) is not int or not 0<=offset<=1000000:
            raise ValueError('记录读取范围无效。')
        if action=='health.records.list':return rows[:limit],True
        return {'items':rows[offset:offset+limit],'total':len(rows),'limit':limit,'offset':offset},True

    def _record_history(self,identity,body,selected,identifiers):
        limit=body.get('limit',100);offset=body.get('offset',0)
        if type(limit)is not int or not 1<=limit<=100 or type(offset)is not int or not 0<=offset<=1000000:
            raise LocalDataError('validation','修改历史读取范围无效。')
        cloud_body={**body,'record_id':selected['id']}
        cached=self.store.get(identity,'cache',['expansion.records.revisions',cloud_body])
        cloud_items=deepcopy(_items(cached)) if cached is not None else []
        if selected['id']>0:self._queue_read_refresh(identity,'expansion.records.revisions',cloud_body)
        seen={(row.get('reason'),(row.get('after') or {}).get('version'),row.get('action','update')) for row in cloud_items}
        local=[]
        for operation in self.store.operations(identity,include_applied=True):
            if operation['status']=='discarded' or operation['action']not in {'expansion.records.update','expansion.records.delete'}:continue
            if operation['payload'].get('record_id')not in identifiers:continue
            after=operation.get('result_entity') if isinstance(operation.get('result_entity'),dict) else operation.get('local_result')
            if operation['action'].endswith('.delete'):after=None
            reason=operation['payload'].get('reason') or '本人修改'
            action='delete' if after is None else 'update'
            signature=(reason,(after or {}).get('version'),action)
            if signature in seen:continue
            seen.add(signature)
            local.append({'id':_local_number(operation['operation_id']),'entity_id':selected['id'],'entity_type':'life_record',
                'action':action,'reason':reason,'before':deepcopy(operation.get('local_before')),'after':deepcopy(after),
                'created_at':operation['created_at'],'local_only':True,'sync_status':operation['status'],'operation_id':operation['operation_id']})
        combined=local+cloud_items;combined.sort(key=lambda row:str(row.get('created_at') or ''),reverse=True)
        return {'items':combined[offset:offset+limit],'total':len(combined),'limit':limit,'offset':offset,
            'history_scope':{'cloud_history_downloaded':cached is not None,'cloud_history_complete':cached is not None and cached.get('offset',0)==0 and len(cloud_items)>=cached.get('total',len(cloud_items)),
                             'local_revision_count':len(local)}}

    def _fetch_resource(self,identity,name):
        expected=None;offset=0;assembled=[];first=None;count=0;used=0
        while True:
            self._guard(identity)
            params={'resource':name,'offset':offset}
            if identity.data_generation:params['data_generation']=identity.data_generation
            if expected is not None:params['version']=expected
            page=self.cloud_transport('GET',STATE_PATH,params=params,expected_identity=_scope(identity))
            self._guard(identity)
            if (not isinstance(page,dict) or page.get('schema_version')!=1 or page.get('actor_id')!=identity.actor_id
                    or page.get('resource')!=name or not isinstance(page.get('server_instance_id'),str)
                    or not re.fullmatch(r'[a-f0-9]{64}',str(page.get('version','')))):
                raise LocalDataError('sync_protocol','同步资料校验失败，原本机数据保持保存。')
            if identity.server_instance_id and page['server_instance_id']!=identity.server_instance_id:
                raise LocalDataError('account_changed','服务身份已变化，请联网重新登录。')
            if identity.data_generation and page.get('data_generation')!=identity.data_generation:
                raise LocalDataError('account_data_reset','云端账号资料已清空，请重新读取。')
            if expected is None:
                expected=page['version'];first=page;count=page.get('count',0)
            elif page['version']!=expected or page['server_instance_id']!=first['server_instance_id']:
                raise LocalDataError('sync_conflict','云端资料在同步期间发生变化，稍后会重新同步。')
            used+=len(json_text(page.get('value')).encode('utf8'))
            if used>128*1024*1024:raise LocalDataError('local_limit','此类资料较多，请选择分批下载。')
            if name in {'life_records','reminders','goals','observations'}:
                rows=_items(page.get('value'))
                if len(rows)>200 or not isinstance(page.get('complete'),bool):raise LocalDataError('sync_protocol')
                if any(not isinstance(row,dict) or type(row.get('id')) is not int or row['id']<=0 or ('user_id'in row and row['user_id']!=identity.actor_id) for row in rows):
                    raise LocalDataError('sync_protocol')
                assembled.extend(rows)
            if page.get('complete',True):break
            next_offset=page.get('next_offset')
            if type(next_offset) is not int or next_offset<=offset or next_offset>1000000 or not rows:
                raise LocalDataError('sync_protocol')
            offset=next_offset
        if name in {'life_records','reminders','goals','observations'}:
            if len({row['id'] for row in assembled})!=len(assembled) or type(count)is not int or len(assembled)!=count:
                raise LocalDataError('sync_protocol','同步页不完整，原本机数据保持保存。')
            value=assembled if name in {'life_records','reminders'} else {**first['value'],'items':assembled,'total':len(assembled),'limit':len(assembled),'offset':0}
        else:value=first['value']
        return {'value':value,'version':expected,'complete':True,'memory_revision':first.get('memory_revision'),
            'server_instance_id':first['server_instance_id'],'generated_at':first.get('generated_at')}

    def _wire_operation(self,identity,operation):
        if operation.get('wire_payload') is not None:
            return operation['wire_payload'],operation.get('wire_base_version')
        body=deepcopy(operation['payload']);base=operation.get('base_version')
        mappings=self.store.get(identity,'mapping','entity_ids',{})
        for key in _ID_FIELDS:
            if type(body.get(key)) is int and body[key]<0:
                mapped=mappings.get(str(body[key]))
                if not mapped:raise LocalDataError('dependency_pending','请先同步新建记录，再同步对此记录的修改。')
                body[key]=mapped['id']
        for key in ('record_ids','reminder_ids'):
            if key in body:
                mapped=[]
                for identifier in body[key]:
                    if type(identifier)is int and identifier<0:
                        row=mappings.get(str(identifier))
                        if not row:raise LocalDataError('dependency_pending','请先同步新建记录。')
                        identifier=row['id']
                    mapped.append(identifier)
                body[key]=mapped
        parent=self.store.operation(identity,operation['depends_on']) if operation.get('depends_on') else None
        if parent:
            if parent['status']=='discarded':parent=None
            elif parent['status'] not in {'applied','confirmed'}:raise LocalDataError('dependency_pending','前一项修改仍待处理，请先核对同步状态。')
        if parent:
            # Every link must prove that this device's preceding commit was
            # exactly the next operation's base. Appends may have no CAS base,
            # but their authoritative previous_version still proves the chain.
            # Any intervening device change breaks this proof and keeps the
            # original base, so the server retains a conflict instead of losing it.
            if base is not None:
                cursor=parent;seen=set()
                while cursor and cursor['operation_id']not in seen and len(seen)<10000:
                    seen.add(cursor['operation_id'])
                    if base==cursor.get('previous_version'):
                        base=parent.get('server_version',base);break
                    ancestor=self.store.operation(identity,cursor['depends_on'])if cursor.get('depends_on')else None
                    if (not ancestor or ancestor['status']not in {'applied','confirmed'}or ancestor['resource']!=operation['resource']
                            or cursor.get('previous_version')!=ancestor.get('server_version')):break
                    cursor=ancestor
            original=parent.get('local_result');remote=parent.get('result_entity') or parent.get('result')
            old_entity=original.get('goal',original) if isinstance(original,dict) else {}
            remote_entity=remote.get('goal',remote) if isinstance(remote,dict) else {}
            if (body.get('base_version') is not None and old_entity.get('version')==body['base_version'] and remote_entity.get('version')):
                body['base_version']=remote_entity['version']
            if operation['resource']=='ai_preferences' and isinstance(original,dict) and isinstance(remote,dict):
                for patch in body.get('categories',[]):
                    old=next((row for row in original.get('categories',[]) if row.get('domain')==patch['domain']),{})
                    new=next((row for row in remote.get('categories',[]) if row.get('domain')==patch['domain']),{})
                    if old.get('revision')==patch.get('expected_revision') and type(new.get('revision'))is int:patch['expected_revision']=new['revision']
            intent=_captured_library(operation)if _library_only(operation)else None
            if intent and isinstance(remote,dict)and'diet_library_v1'in remote:
                # Preserve this child's delta against its exact predecessor's
                # authoritative result. CAS still detects any other-device edit.
                body={'diet_library_v1':_merge_library(remote['diet_library_v1'],intent)}
                base=parent.get('server_version',base)
        # Child changes to a new record carry a local derived version. Map it
        # only to the precise preceding create result, not arbitrary latest data.
        if body.get('base_version') is not None:
            target=next((body[key] for key in _ID_FIELDS if key in body),None)
            if target is not None:
                versions=self.store.get(identity,'mapping','entity_versions',{})
                body['base_version']=versions.get(str(target)+':'+str(body['base_version']),body['base_version'])
        if base is None and operation['action'] not in APPENDS and not operation.get('safety_shrink'):
            if (parent and operation.get('base_fingerprint')==parent.get('base_fingerprint')
                    and parent.get('wire_base_version')==parent.get('previous_version')):
                base=parent.get('server_version')
            else:
                current=self._fetch_resource(identity,operation['resource'])
                if digest(current['value'])!=operation.get('base_fingerprint'):
                    error=LocalDataError('base_version_conflict','本机与云端资料均有修改，请选择保留方式。')
                    error.current=current;raise error
                base=current['version']
        body=wire_record_photos(self,identity,operation,body)
        return body,base

    def _ack_mapping(self,identity,operation,result):
        local=operation.get('local_result');local=local.get('goal',local) if isinstance(local,dict) else None
        remote=result.get('goal',result) if isinstance(result,dict) else result
        remote_id=remote.get('id') if isinstance(remote,dict) else remote if type(remote)is int else None
        if operation.get('local_number') and type(remote_id)is int and remote_id>0:
            mappings=self.store.get(identity,'mapping','entity_ids',{})
            mappings[str(operation['local_number'])]={'id':remote_id,'resource':operation['resource']}
            self.store.put(identity,'mapping','entity_ids',mappings)
        if operation['resource']=='goals'and isinstance(local,dict)and isinstance(remote,dict):
            local_reminder=local.get('reminder_id');remote_reminder=remote.get('reminder_id')
            if type(local_reminder)is int and local_reminder<0 and type(remote_reminder)is int and remote_reminder>0:
                mappings=self.store.get(identity,'mapping','entity_ids',{})
                mappings[str(local_reminder)]={'id':remote_reminder,'resource':'reminders'}
                self.store.put(identity,'mapping','entity_ids',mappings)
        if isinstance(local,dict) and local.get('version') and isinstance(remote,dict) and remote.get('version'):
            versions=self.store.get(identity,'mapping','entity_versions',{})
            versions[str(remote_id)+':'+str(local['version'])]=remote['version']
            self.store.put(identity,'mapping','entity_versions',versions)

    def _profile_before_boot_greeting(self,identity):
        """Confirm pre-existing local profile edits before a real boot opening.

        Reuse immutable outbox UUID/CAS handling; never pass a client nickname
        to the greeting endpoint. An unrelated sync or unresolved conflict is
        recoverable and must not replace the cached history with a stale name.
        """
        pending=[row for row in self.store.operations(identity) if row['resource']=='profile']
        if not pending:return
        if any(row['status']in {'conflict','rejected'}for row in pending):
            raise LocalDataError('profile_sync_pending','新的个人称呼尚未同步，请先核对个人信息的同步状态。')
        self._guard(identity)
        try:
            result=self.sync_once(max_operations=50,resources=('profile',),operation_resources=('profile',),
                                  refresh_reads=False,expected_identity=identity)
        except Exception as error:
            if getattr(error,'code',None) in {'network','timeout','offline','unavailable','service_unavailable','outcome_uncertain'}:
                raise LocalDataError('profile_sync_pending','新的个人称呼已保存在本机，联网同步后会更新欢迎语。') from None
            raise
        self._guard(identity)
        if result.get('busy') or any(row['resource']=='profile'for row in self.store.operations(identity)):
            raise LocalDataError('profile_sync_pending','新的个人称呼正在同步，联网后会更新欢迎语。')

    def sync_once(self,*,max_operations=50,resources=None,operation_resources=None,refresh_reads=True,expected_identity=None):
        if not self._sync_lock.acquire(blocking=False):return {'busy':True}
        identity=None
        try:
            identity=self.identity();selected=RESOURCES if resources is None else tuple(resources)
            if expected_identity is not None and identity!=expected_identity:
                raise LocalDataError('account_changed','账号已变更，本次同步没有作用于其他账号。')
            if any(name not in RESOURCES for name in selected):raise LocalDataError('validation')
            allowed_operations=None if operation_resources is None else frozenset(operation_resources)
            if (allowed_operations is not None and not allowed_operations.issubset(RESOURCES)) or type(refresh_reads)is not bool:
                raise LocalDataError('validation')
            confirmed_before={key:value['token']for key,value in self._confirmed_records(identity).items()}
            applied=0
            operations=self.store.operations(identity)
            operations.sort(key=lambda row:(row['action']not in {'member.memory.forget','member.memory.controls.update'},row['sequence_no']))
            for operation in operations:
                if allowed_operations is not None and operation['resource']not in allowed_operations:continue
                if applied>=max_operations:break
                if operation['status'] in {'conflict','rejected','confirmed'}:continue
                self._guard(identity)
                try:wire,base=self._wire_operation(identity,operation)
                except LocalDataError as error:
                    if error.code=='dependency_pending':continue
                    if error.code=='base_version_conflict':
                        self.store.change_operation(identity,operation['operation_id'],status='conflict',
                            patch={'conflict_current':error.current,'error_code':'conflict'})
                        continue
                    raise
                if self.store.claim_operation(identity,operation['operation_id'],wire,base) is None:continue
                request={'operation_id':operation['operation_id'],'action':operation['action'],'payload':wire,
                    'base_version':base,'local_id':operation.get('local_id')}
                if identity.data_generation:
                    if operation.get('data_generation')!=identity.data_generation:
                        raise LocalDataError('account_data_reset','这份旧账号修改已失效，不会再上传。')
                    request['data_generation']=operation['data_generation']
                try:response=self.cloud_transport('POST',OPERATIONS_PATH,json=request,expected_identity=_scope(identity))
                except Exception as error:
                    status=getattr(error,'status_code',None)
                    if status in {400,404,422} and getattr(error,'outcome_uncertain',True) is False:
                        self._guard(identity)
                        self.store.change_operation(identity,operation['operation_id'],status='rejected',
                            patch={'error_code':getattr(error,'code','validation'),'safe_to_retry':True})
                        continue
                    # The effect may have committed. Keep the frozen UUID/body;
                    # the next sync replays the same idempotent server operation.
                    raise
                self._guard(identity)
                if (not isinstance(response,dict) or response.get('operation_id')!=operation['operation_id']
                        or response.get('action')!=operation['action'] or response.get('resource')!=operation['resource']):
                    raise LocalDataError('sync_protocol')
                if response.get('status')=='conflict':
                    self.store.change_operation(identity,operation['operation_id'],status='conflict',
                        patch={'conflict_current':response.get('current'),'error_code':'conflict'})
                    continue
                if response.get('status')!='applied' or not re.fullmatch(r'[a-f0-9]{64}',str(response.get('server_version',''))):
                    raise LocalDataError('sync_protocol')
                if operation['resource']=='goals'and response.get('result_deleted')is True:
                    self._remember_goal_deletion(identity,response.get('result'),operation)
                    marker=self._deleted_goal_receipt(identity,operation)
                    if marker is None:raise LocalDataError('sync_protocol')
                    targets={value for value in (marker.get('goal_id'),marker.get('local_number'))if type(value)is int}
                    for pending in self.store.operations(identity):
                        target=pending.get('payload',{}).get('goal_id')
                        if pending['resource']=='goals'and (target in targets or pending.get('local_number')in targets):
                            self.store.change_operation(identity,pending['operation_id'],status='discarded',patch={'result':marker,'result_entity':None})
                    self.store.change_operation(identity,operation['operation_id'],status='discarded',patch={'result':marker,'result_entity':None})
                    applied+=1
                    continue
                entity=response.get('result_entity') or response.get('result')
                if operation.get('local_number') and type(entity)is int:
                    current=self._fetch_resource(identity,operation['resource'])
                    if current['version']==response['server_version']:
                        entity=next((row for row in _items(current['value']) if row.get('id')==entity),entity)
                self._ack_mapping(identity,operation,entity)
                if operation['action'] in READING_SOCIAL_WRITES and isinstance(response.get('result'),dict):
                    social=response['result'];aid=operation['payload']['article_id']
                    self.store.put(identity,'cache',['reading.social.get',{'article_id':aid}],{key:item for key,item in social.items() if key!='comment'})
                    if isinstance(social.get('comment'),dict) and operation['action']=='reading.comment.add':
                        remembered=self.store.get(identity,'meta','reading-confirmed-comments',{})
                        comment=deepcopy(social['comment'])
                        if comment.pop('avatar_omitted',False):
                            comment['avatar_data']=(operation.get('local_result') or {}).get('comment',{}).get('avatar_data','')
                        remembered[comment['id']]=comment
                        remembered=bounded_comments(remembered)
                        self.store.put(identity,'meta','reading-confirmed-comments',remembered)
                    self.store.invalidate_action_cache(identity,'reading.recommendations')
                    self._queue_read_refresh(identity,'reading.social.get',{'article_id':aid})
                    parent=operation['payload'].get('parent_id')
                    self._queue_read_refresh(identity,'reading.comments',{'article_id':aid,**({'parent_id':parent} if parent else {})})
                self.store.change_operation(identity,operation['operation_id'],status='confirmed',
                    patch={'result':response.get('result'),'result_entity':entity,'server_version':response['server_version'],
                           'previous_version':response.get('previous_version')})
                if operation['action']=='member.memory.forget':
                    for prior in self.store.operations(identity):
                        if prior['resource'] in {'ai_preferences','ai_profile'} and prior['sequence_no']<operation['sequence_no']:
                            self.store.change_operation(identity,prior['operation_id'],status='discarded')
                applied+=1
            fetched={name:self._fetch_resource(identity,name) for name in selected}
            self._guard(identity)
            revisions={row.get('memory_revision') for row in fetched.values()}
            instances={row['server_instance_id'] for row in fetched.values()}
            if len(revisions)>1 or len(instances)>1:raise LocalDataError('sync_conflict','资料权限在同步期间发生变化，请稍后重试。')
            previous=self.store.get(identity,'meta','sync',{})
            instance=next(iter(instances),'')
            if previous.get('server_instance_id') and instance and previous['server_instance_id']!=instance:
                raise LocalDataError('account_changed','服务身份已变化，请联网重新登录。')
            self.store.replace_resources(identity,fetched)
            if 'life_records'in fetched:self._reconcile_confirmed_records(identity,confirmed_before)
            revision=next(iter(revisions),None)
            if previous.get('memory_revision') is not None and previous['memory_revision']!=revision:
                self.store.clear_cache(identity)
            self.store.put(identity,'meta','sync',{'last_synced_at':_now(),'memory_revision':revision,
                'server_instance_id':instance,'last_error':None})
            refreshed=self._refresh_read_cache(identity) if refresh_reads else {}
            return {'applied':applied,**refreshed,**self._mark(identity)}
        except Exception as error:
            code=getattr(error,'code','network')
            if identity is not None:
                if code=='account_data_reset':
                    self.store.invalidate_account(identity);self.store.purge_account(identity)
                    self._blocked[identity.namespace]=identity.epoch
                    raise
                if code in {'authentication','not_authenticated','permission','identity_unavailable'}:
                    self._blocked[identity.namespace]=identity.epoch
                try:
                    previous=self.store.get(identity,'meta','sync',{})
                    self.store.put(identity,'meta','sync',{**previous,'last_error':code if isinstance(code,str) and len(code)<80 else 'network'})
                except Exception:pass
            raise
        finally:self._sync_lock.release()

    def pending(self):
        identity=self.identity()
        operations=self.store.operations(identity);result=[];by_id={row['operation_id']:row for row in operations};blocked=set()
        for row in operations:
            if not(row.get('attempted')or row['status']=='uncertain'or row['resource']!='preferences'):continue
            parent=row.get('depends_on')
            while parent in by_id and parent not in blocked:
                blocked.add(parent);parent=by_id[parent].get('depends_on')
        for row in operations:
            parent=by_id.get(row.get('depends_on'))
            if parent is not None and parent['status']!='confirmed':blocked.add(row['operation_id'])
        for row in operations:
            if row['status']=='confirmed':continue
            value={key:row.get(key)for key in('operation_id','action','resource','status','created_at','local_result','conflict_current','error_code')}
            value['safe_to_retry']=row.get('safe_to_retry')is True
            if _library_only(row):value['library_change']=self._library_change(identity,row,blocked)
            result.append(value)
        self._guard(identity);return result

    def _library_change(self,identity,row,blocked):
        intent=_captured_library(row)
        cloud=None
        try:
            resource=row.get('conflict_current')
            if resource is None:resource=self._resource(identity,'preferences')
            if (isinstance(resource,dict)and isinstance(resource.get('value'),dict)
                    and resource.get('actor_id',identity.actor_id)==identity.actor_id
                    and resource.get('resource','preferences')=='preferences'):
                cloud=_library_value(resource['value'].get('diet_library_v1'))
        except (ValueError,TypeError,KeyError):
            cloud=None
        local_valid=True
        try:local=_library_value(row['payload']['diet_library_v1'])
        except (ValueError,TypeError,KeyError):local=_library_value(None);local_valid=False
        comparison=intent or (_library_intent(cloud,local)if cloud is not None and local_valid else None)
        allowed=(row['operation_id']not in blocked and(row['status']=='conflict'or row['status']=='rejected'and row.get('safe_to_retry')is True))
        return {'schema_version':1,'basis':'captured'if intent else 'cloud_comparison'if comparison else 'unknown',
            'added':{kind:comparison['added'][kind]if comparison else []for kind in('foods','recipes')},
            'removed':{kind:comparison['removed'][kind]if comparison else []for kind in('foods','recipes')},
            'local':{kind:local[kind]for kind in('foods','recipes')},
            'cloud':{kind:cloud[kind]for kind in('foods','recipes')}if cloud is not None else None,
            'can_keep_cloud':allowed,'can_retry_local':allowed and local_valid}

    def _resolve_library(self,identity,operation,decision):
        if not self._sync_lock.acquire(blocking=False):raise LocalDataError('conflict','正在核对同步结果，请稍后重试。')
        try:
            self._guard(identity)
            replacement=None;current=self._resource(identity,'preferences')
            if decision=='retry_local':
                # Never label an old local snapshot as the latest cloud value.
                current=self._fetch_resource(identity,'preferences');self._guard(identity)
                intent=_captured_library(operation)
                body={'diet_library_v1':_merge_library(current['value'].get('diet_library_v1'),intent)
                    if intent else deepcopy(operation['payload']['diet_library_v1'])}
                clean=_preference_patch(body);identifier=str(uuid4())
                replacement={'operation_id':identifier,'action':'preferences.update','resource':'preferences','payload':body,
                    'base_version':current['version'],'local_id':None,'local_number':None,'created_at':_now(),
                    'request_digest':digest(['preferences.update',body]),'base_fingerprint':digest(current['value']),
                    'depends_on':operation['operation_id'],'library_retry':True,
                    'local_result':_preferences({**current['value'],**clean})}
                if intent:replacement['library_intent']=_library_intent(current['value'].get('diet_library_v1'),clean['diet_library_v1'])
            self._guard(identity)
            result=self.store.resolve_library_intent(identity,operation['operation_id'],replacement=replacement,current=current)
            self._guard(identity)
            result={**result,'decision':decision,**({'result':replacement['local_result']}if replacement else {})}
        finally:self._sync_lock.release()
        if replacement:self._ask_sync(force=True)
        return result

    def resolve(self,operation_id,decision):
        identity=self.identity();operation=self.store.operation(identity,operation_id)
        if operation is None:raise LocalDataError('not_found','待同步记录不存在。')
        if decision not in {'keep_cloud','retry_local'}:raise LocalDataError('validation')
        if operation['status'] not in {'conflict','rejected'}:
            raise LocalDataError('conflict','此记录尚未确认云端结果，暂不能丢弃或改写。')
        if operation['status']=='rejected' and operation.get('safe_to_retry') is not True:
            raise LocalDataError('conflict','此记录尚未确认云端结果，请先核对原操作。')
        if _library_only(operation):return self._resolve_library(identity,operation,decision)
        if decision=='keep_cloud':
            self.store.change_operation(identity,operation_id,status='discarded')
            return {'resolved':True,'decision':decision}
        # An explicit user choice makes a new intent at the freshly read cloud
        # version. Keep the rejected/conflicted original as an audit receipt.
        current=self._resource(identity,operation['resource'])
        if not current:raise LocalDataError('local_not_ready','请先同步最新资料。')
        body=deepcopy(operation['payload']);body.pop('request_id',None)
        if operation['action']=='member.ai-profile.categories.save':
            rows=current['value'].get('categories',[])
            for patch in body.get('categories',[]):
                match=next((row for row in rows if row.get('domain')==patch['domain']),None)
                if match:patch['expected_revision']=match['revision']
        target=next((body.get(key) for key in _ID_FIELDS if key in body),None)
        mappings=self.store.get(identity,'mapping','entity_ids',{})
        if target is not None and str(target)in mappings:
            mapped=mappings[str(target)]['id']
            for key in _ID_FIELDS:
                if body.get(key)==target:body[key]=mapped
            target=mapped
        if target is not None and 'base_version'in body:
            row=next((item for item in _items(current['value']) if item.get('id')==target),None)
            if row:body['base_version']=row.get('version')
        new_id=str(uuid4())
        if operation['action']=='member.ai-profile.categories.save':body['request_id']=new_id
        self.store.change_operation(identity,operation_id,status='discarded')
        try:result=self._write(identity,operation['action'],body,new_id)
        except Exception:
            self.store.change_operation(identity,operation_id,status=operation['status']);raise
        self._ask_sync(force=True);return {'resolved':True,'decision':decision,'operation_id':new_id,'result':result}
