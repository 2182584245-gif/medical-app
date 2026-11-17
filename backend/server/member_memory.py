"""Member-only cloud memory controls, minimal events and dated evidence APIs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import UUID
from pathlib import Path
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.routing import APIRoute
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .platform_auth import authentication_dependency
from .member_memory_core import (MemberMemoryCore, MemoryCoreError,
                                 explicit_preference_entries, stored_preference_entries)

DOMAINS = ('profile', 'life_records', 'reminders', 'goals_habits',
           'services_appointments', 'reading', 'shopping', 'forum',
           'operations', 'preferences', 'ai_records')
PAGES = {'today', 'records', 'ai', 'community', 'mine', 'voice', 'trends',
         'record-form', 'record-detail', 'record-edit', 'today-reading',
         'today-goals', 'today-reminders', 'today-services', 'today-shopping',
         'community-post', 'community-search', 'community-compose',
         'mine-profile', 'mine-settings', 'mine-help', 'ai-settings',
         'ai-history', 'ai-photo', 'ai-voice', 'other'}
_CONTRACT = json.loads(Path(__file__).with_name('member_memory_event_contract.json').read_text(encoding='utf8'))
PAGES = frozenset(_CONTRACT['operations']['page_resource_ids'])
FEATURES = frozenset(_CONTRACT['operations']['feature_resource_ids'])


class MemoryRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                response = await original(request)
                response.headers['Cache-Control'] = 'no-store'
                return response
            except MemoryCoreError as error:
                status = 403 if str(error)=='permission_denied' else 409 if str(error)=='personal_context_changed' else 422
                raise failure(status,'个人记忆请求无法处理，请核对日期和账户。') from None
        return handle


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def stamp(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parsed(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return result.astimezone(timezone.utc)


def failure(status, message):
    return HTTPException(status, message, headers={'Cache-Control': 'no-store'})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class Event(StrictModel):
    event_id: str = Field(min_length=36, max_length=36)
    occurred_at: str = Field(min_length=20, max_length=40)
    domain: str = Field(max_length=30)
    action: str = Field(max_length=30)
    resource_type: str = Field(max_length=30)
    resource_id: str = Field(min_length=1, max_length=120)
    duration_ms: int = Field(default=0, ge=0, le=86400000)


class Events(StrictModel):
    events: list[Event] = Field(min_length=1, max_length=20)


class ControlChange(StrictModel):
    domain: str
    enabled_for_ai: bool
    collect_events: bool


class ControlsChange(StrictModel):
    controls: list[ControlChange] = Field(min_length=1, max_length=11)


class Forget(StrictModel):
    domains: list[str] = Field(min_length=1, max_length=11)


class ContextQuery(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    bucket: str = 'all'


class PreferenceFeedback(StrictModel):
    action: str
    expected_revision: int = Field(ge=1)


class Empty(StrictModel):
    pass


class ExplicitPreference(StrictModel):
    id: str = Field(min_length=36, max_length=36)
    text: str = Field(min_length=1, max_length=2000)


class AIProfileChange(StrictModel):
    expected_revision: int = Field(ge=0, le=9007199254740990)
    request_id: str = Field(min_length=36, max_length=36)
    entries: list[ExplicitPreference] = Field(max_length=20)


class MemberMemoryService:
    def __init__(self, database, preferences, *, clock=None, core=None):
        self.db, self.preferences = database, preferences
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.core = core or MemberMemoryCore(database, clock=self.clock)

    def member(self, actor):
        identity = self.db._identity.get()
        if type(actor) is not int or actor <= 0 or identity.user_id != actor or identity.role_code != 'member':
            raise failure(403, '无权读取该账户的记忆。')

    def control_rows(self, actor):
        self.member(actor)
        with self.db.connect() as conn:
            return {row['domain']: dict(row) for row in conn.execute(
                'SELECT domain,enabled_for_ai,collect_events,forget_before,revision,updated_at '
                'FROM member_memory_controls WHERE user_id=?', (actor,)).fetchall()}

    def settings(self, actor):
        self.member(actor)
        pref = self.preferences.get(actor)
        enabled = pref.get('mobile_ai_personal_enabled', True) is True
        rows = self.control_rows(actor)
        controls = [{
            'domain': domain, 'enabled_for_ai': bool(rows.get(domain, {}).get('enabled_for_ai', True)),
            'collect_events': bool(rows.get(domain, {}).get('collect_events', True)),
            'forget_before': rows.get(domain, {}).get('forget_before'),
            'revision': int(rows.get(domain, {}).get('revision', 0)),
        } for domain in DOMAINS]
        with self.db.connect() as conn:
            first = conn.execute('SELECT MIN(received_at) AS first_at FROM member_memory_events WHERE user_id=?', (actor,)).fetchone()
            profile = conn.execute('SELECT revision FROM member_ai_preferences WHERE user_id=?', (actor,)).fetchone()
            introduction=conn.execute('SELECT personal_introduction,personal_introduction_updated_at,updated_at FROM member_profiles WHERE user_id=?',(actor,)).fetchone()
        category_reader = getattr(self, 'category_revision_reader', None)
        category_revision = category_reader(actor) if category_reader else None
        return {'enabled': enabled, 'disabled_domains': [r['domain'] for r in controls if not r['enabled_for_ai'] or not r['collect_events']],
                'revision': digest([enabled, controls, int(profile['revision']) if profile else 0, category_revision,
                    digest(dict(introduction)) if introduction else None])[:16], 'collection_started_at': first['first_at'] if first else None,
                'controls': controls, 'timezone': 'Asia/Shanghai'}

    def ai_profile(self, actor):
        self.member(actor)
        config = self.settings(actor)
        control = next(item for item in config['controls'] if item['domain'] == 'preferences')
        enabled = config['enabled'] and control['enabled_for_ai']
        cutoff = parsed(control['forget_before']) if control['forget_before'] else None
        with self.db.connect() as conn:
            row = conn.execute('SELECT entries_json,revision,updated_at FROM member_ai_preferences WHERE user_id=?', (actor,)).fetchone()
        entries = stored_preference_entries(row['entries_json']) if row else []
        entries = [{**item, 'source': 'user_confirmed',
                    'used_for_ai': bool(enabled and (cutoff is None or parsed(item['updated_at']) > cutoff))}
                   for item in entries]
        inferred = [item for item in self.list_preferences(actor) if item['status'] in {'inferred', 'confirmed'}]
        return {'revision': int(row['revision']) if row else 0,
                'entries': entries, 'inferred': inferred,
                'enabled_for_ai': bool(enabled), 'updated_at': row['updated_at'] if row else None}

    def save_ai_profile(self, actor, expected_revision, request_id, entries):
        self.member(actor)
        try:
            if type(request_id) is not str or str(UUID(request_id)) != request_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise failure(422, '保存编号无效，请重新打开生活档案。') from None
        if type(expected_revision) is not int or not 0 <= expected_revision <= 9007199254740990:
            raise failure(422, '档案版本无效，请重新读取。')
        clean = explicit_preference_entries(entries)
        request_hash = digest([expected_revision, clean])
        now = stamp(self.clock())
        with self.db.transaction() as conn:
            row = conn.execute('SELECT entries_json,revision,last_request_id,last_request_hash FROM member_ai_preferences WHERE user_id=?', (actor,)).fetchone()
            if row and row['last_request_id'] == request_id:
                if row['last_request_hash'] != request_hash:
                    raise failure(409, '同一次保存的内容不能更改，请重新读取。')
            else:
                if (int(row['revision']) if row else 0) != expected_revision:
                    raise failure(409, '生活档案已在其他位置更新，请重新读取后保存。')
                previous = {item['id']: item for item in stored_preference_entries(row['entries_json'])} if row else {}
                saved = []
                for item in clean:
                    old = previous.get(item['id'])
                    saved.append(old if old and old['text'] == item['text'] else
                                 {**item, 'created_at': old['created_at'] if old else now, 'updated_at': now})
                if row:
                    changed = conn.execute('UPDATE member_ai_preferences SET entries_json=?,revision=revision+1,'
                        'last_request_id=?,last_request_hash=?,updated_at=? WHERE user_id=? AND revision=?',
                        (encoded(saved),request_id,request_hash,now,actor,expected_revision))
                else:
                    changed = conn.execute('INSERT INTO member_ai_preferences '
                        '(user_id,entries_json,revision,last_request_id,last_request_hash,created_at,updated_at) '
                        'VALUES (?,?,1,?,?,?,?) ON CONFLICT(user_id) DO NOTHING',
                        (actor,encoded(saved),request_id,request_hash,now,now))
                if changed.rowcount != 1:
                    winner = conn.execute('SELECT last_request_id,last_request_hash FROM member_ai_preferences WHERE user_id=?', (actor,)).fetchone()
                    if not winner or winner['last_request_id'] != request_id or winner['last_request_hash'] != request_hash:
                        raise failure(409, '生活档案已更新，请重新读取后保存。')
        return self.ai_profile(actor)

    def change_controls(self, actor, values):
        self.member(actor)
        if len({r['domain'] for r in values}) != len(values) or any(r['domain'] not in DOMAINS for r in values):
            raise failure(422, '记忆分类无效。')
        now = stamp(self.clock())
        with self.db.transaction() as conn:
            locker = getattr(self, 'multimodal_lock_owner', None)
            if locker: locker(conn, actor)
            for row in values:
                conn.execute('INSERT INTO member_memory_controls '
                    '(user_id,domain,enabled_for_ai,collect_events,forget_before,revision,updated_at) VALUES (?,?,?,?,NULL,1,?) '
                    'ON CONFLICT(user_id,domain) DO UPDATE SET enabled_for_ai=excluded.enabled_for_ai, '
                    'collect_events=excluded.collect_events,revision=member_memory_controls.revision+1,updated_at=excluded.updated_at',
                    (actor,row['domain'],row['enabled_for_ai'],row['collect_events'],now))
                if row['domain']=='profile' and (not row['enabled_for_ai'] or not row['collect_events']):
                    invalidator=getattr(self,'profile_vector_invalidator',None)
                    if invalidator:invalidator(actor,connection=conn)
                if not row['enabled_for_ai'] or not row['collect_events']:
                    invalidator = getattr(self, 'multimodal_invalidator', None)
                    if invalidator: invalidator(actor, domains=[row['domain']], connection=conn)
                    if row['domain'] == 'ai_records':
                        invalidator = getattr(self, 'conversation_invalidator', None)
                        if invalidator: invalidator(actor, connection=conn)
        return self.settings(actor)

    def forget(self, actor, domains):
        self.member(actor)
        if len(set(domains)) != len(domains) or any(domain not in DOMAINS for domain in domains):
            raise failure(422, '记忆分类无效。')
        now = stamp(self.clock())
        with self.db.transaction() as conn:
            locker = getattr(self, 'multimodal_lock_owner', None)
            if locker: locker(conn, actor)
            for domain in domains:
                conn.execute('INSERT INTO member_memory_controls '
                    '(user_id,domain,enabled_for_ai,collect_events,forget_before,revision,updated_at) VALUES (?,?,TRUE,TRUE,?,1,?) '
                    'ON CONFLICT(user_id,domain) DO UPDATE SET forget_before=excluded.forget_before, '
                    'revision=member_memory_controls.revision+1,updated_at=excluded.updated_at', (actor,domain,now,now))
                conn.execute('DELETE FROM member_memory_daily WHERE user_id=? AND domain=?', (actor,domain))
                conn.execute('DELETE FROM member_memory_events WHERE user_id=? AND domain=? AND received_at<=?', (actor,domain,now))
                invalidator = getattr(self, 'multimodal_invalidator', None)
                if invalidator: invalidator(actor, domains=[domain], connection=conn)
                if domain == 'ai_records':
                    invalidator = getattr(self, 'conversation_invalidator', None)
                    if invalidator: invalidator(actor, connection=conn)
                if domain=='profile':
                    # Move the public profile CAS anchor so a queued pre-forget
                    # offline edit conflicts instead of resurrecting evidence.
                    # Content is retained; only confirmation/metadata changes.
                    conn.execute('UPDATE member_profiles SET personal_introduction_updated_at=NULL,updated_at=? WHERE user_id=?',(now,actor))
                    invalidator=getattr(self,'profile_vector_invalidator',None)
                    if invalidator:invalidator(actor,connection=conn)
            # Preferences may be supported by any domain. Revoke them together;
            # their old source evidence cannot bypass the per-domain tombstones.
            conn.execute("UPDATE member_memory_preferences SET status='revoked',revision=revision+1,updated_at=? WHERE user_id=?", (now,actor))
        return {'forgotten_domains': domains, 'forget_before': now, 'business_records_deleted': False}

    def append_events(self, actor, values):
        self.member(actor)
        now = self.clock().astimezone(timezone.utc)
        prepared = []
        for row in values:
            try:
                identifier = str(UUID(row['event_id']))
                at = parsed(row['occurred_at'])
                if identifier != row['event_id'] or not now-timedelta(days=31) <= at <= now+timedelta(minutes=5):
                    raise ValueError
                domain, action, kind, resource = row['domain'],row['action'],row['resource_type'],row['resource_id']
                combos = {'operations': {'page_view': {'page'}, 'button_click': {'feature'}},
                          'reading': {'content_view': {'content','library_document'}},
                          'forum': {'post_view': {'post'}}}
                if kind not in combos.get(domain, {}).get(action, set()):
                    raise ValueError
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', resource):
                    raise ValueError
                if kind == 'page' and resource not in PAGES:
                    raise ValueError
                if kind == 'feature' and resource not in FEATURES:
                    raise ValueError
                if action in {'content_view','post_view'} and row['duration_ms'] < 2000:
                    raise ValueError
                if kind == 'post' and (not resource.isdigit() or not 0 < int(resource) <= 2147483647):
                    raise ValueError
                prepared.append((identifier,domain,action,stamp(at),kind,resource,encoded({'duration_ms':row['duration_ms']})))
            except (ValueError, TypeError, KeyError):
                raise failure(422, '操作事件格式无效。') from None
        if len({r[0] for r in prepared}) != len(prepared):
            raise failure(422, '操作事件编号重复。')
        accepted = duplicates = ignored = 0
        with self.db.transaction() as conn:
            config = self.settings(actor)
            control_map = {r['domain']:r for r in config['controls']}
            for event in prepared:
                event_id,domain,action,occurred,kind,resource,payload = event
                if not config['enabled'] or domain in config['disabled_domains']:
                    ignored += 1; continue
                cutoff = control_map[domain].get('forget_before')
                if cutoff and parsed(occurred) < parsed(cutoff):
                    ignored += 1; continue
                if kind == 'post' and conn.execute("SELECT id FROM forum_posts WHERE id=? AND status='active'", (int(resource),)).fetchone() is None:
                    ignored += 1; continue
                old = conn.execute('SELECT domain,event_type,occurred_at,entity_type,entity_id,payload_json '
                                   'FROM member_memory_events WHERE user_id=? AND event_id=?', (actor,event_id)).fetchone()
                if old:
                    if tuple(old[k] for k in ('domain','event_type','occurred_at','entity_type','entity_id','payload_json')) != event[1:]:
                        raise failure(409, '同一事件编号的内容不能更改。')
                    duplicates += 1; continue
                conn.execute('INSERT INTO member_memory_events '
                    '(user_id,event_id,domain,event_type,occurred_at,received_at,entity_type,entity_id,payload_json,schema_version) '
                    'VALUES (?,?,?,?,?,?,?,?,?,1)', (actor,event_id,domain,action,occurred,stamp(now),kind,resource,payload))
                accepted += 1
        return {'accepted': accepted, 'duplicates': duplicates, 'ignored': ignored}

    def list_preferences(self, actor):
        self.member(actor)
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute('SELECT preference_key,statement,status,confidence,evidence_json,'
                'support_start,support_end,expires_at,model,source_digest,revision,created_at,updated_at '
                'FROM member_memory_preferences WHERE user_id=? ORDER BY updated_at DESC LIMIT 100', (actor,)).fetchall()]
            states = self.core.preference_support_status(actor, rows)
        # Internal references are used for validation, not displayed as prose or
        # returned to the preference-management panel.
        return [{**{key:value for key,value in row.items() if key not in {'evidence_json','source_digest','model'}},
                 'status': states[row['preference_key']]} for row in rows]

    def feedback(self, actor, key, action, expected_revision):
        self.member(actor)
        if action not in {'confirm','reject'} or not re.fullmatch(r'[a-z][a-z0-9_]{1,63}',key):
            raise failure(422, '偏好反馈无效。')
        with self.db.transaction() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM member_memory_preferences WHERE user_id=? AND preference_key=?', (actor,key)).fetchall()]
            state = self.core.preference_support_status(actor,rows).get(key)
            if state not in {'inferred','confirmed'}:
                raise failure(409,'这项偏好的依据已变化，请重新读取。')
            result = conn.execute('UPDATE member_memory_preferences SET status=?,revision=revision+1,updated_at=? '
                'WHERE user_id=? AND preference_key=? AND revision=? AND status IN (?,?)',
                ('confirmed' if action=='confirm' else 'rejected',stamp(self.clock()),actor,key,expected_revision,'inferred','confirmed'))
            if result.rowcount != 1:
                raise failure(409, '偏好已变更，请重新读取。')
        return {'updated': True}


def create_memory_router(auth, database, preferences, *, service=None, summarizer=None, projector=None, category_summarizer=None):
    router = APIRouter(prefix='/v1/member/memory', tags=['member'],route_class=MemoryRoute)
    dependency = Depends(authentication_dependency(auth))
    memory = service or MemberMemoryService(database, preferences)

    def actor(identity):
        if identity.user.role_code != 'member':
            raise failure(403, '请使用会员账号。')
        return identity.user.id

    @router.get('/settings')
    def settings(identity=dependency):
        with auth.identity(identity):
            return memory.settings(actor(identity))

    @router.patch('/controls')
    def controls(value: ControlsChange, identity=dependency):
        with auth.identity(identity):
            return memory.change_controls(actor(identity), [r.model_dump() for r in value.controls])

    @router.post('/forget')
    def forget(value: Forget, identity=dependency):
        with auth.identity(identity):
            return memory.forget(actor(identity), value.domains)

    @router.post('/events')
    def events(value: Events, background: BackgroundTasks, request: Request, identity=dependency):
        with auth.identity(identity):
            result = memory.append_events(actor(identity), [r.model_dump() for r in value.events])
        if projector and result['accepted']:
            projector.schedule(background,identity,{r.domain for r in value.events})
        if category_summarizer and result['accepted']:
            background.add_task(category_summarizer.start,request,identity)
        return result

    @router.post('/context')
    async def context(value: ContextQuery, request: Request, identity=dependency):
        if value.bucket not in {'all','recent','mid','remote'}:
            raise failure(422, '日期范围无效。')
        with auth.identity(identity):
            current = actor(identity)
            config = await asyncio.to_thread(memory.settings,current)
            if not config['enabled']:
                return {'member_user_id':current,'memory_enabled':False,'generated_at':stamp(), 'coverage':{'state':'disabled'}}
            result = await asyncio.to_thread(memory.core.context,current,value.question,bucket=value.bucket,max_chars=18000,
                initial_retrieval=True)
            after = await asyncio.to_thread(memory.settings,current)
            if after['revision'] != config['revision'] or not after['enabled']:
                raise failure(409,'个人记忆设置已变化，请重新提问。')
            result['memory_enabled'] = True
            result['memory_revision'] = config['revision']
            result['memory_controls'] = {r['domain']:r for r in config['controls']}
        if category_summarizer:
            category_summarizer.schedule(request,identity)
        elif summarizer:
            summarizer.schedule(request,identity)
        conversation = getattr(request.app.state, 'conversation_memory', None)
        if conversation is not None:
            conversation.schedule(request, identity)
        return result

    @router.get('/overview')
    def overview(identity=dependency):
        with auth.identity(identity):
            return memory.core.overview(actor(identity))

    @router.get('/conversations/status')
    def conversation_status(request: Request, identity=dependency):
        """Coverage only: no scheduling, mutations, embeddings or generation."""
        with auth.identity(identity):
            current = actor(identity)
            progress = getattr(request.app.state, 'conversation_history', None)
            if progress is None:
                return {'schema_version': 1, 'enabled': memory.settings(current)['enabled'], 'state': 'unavailable',
                    'messages': {key: None for key in ('eligible_older','summarized','pending','recent_direct')},
                    'search': {key: None for key in ('eligible_user','indexed_user','pending_user')},
                    'updated_at': None, 'retry_after_seconds': 60}
            return progress.status(current)

    @router.get('/days')
    def days(domain: str, start_day: str, end_day: str, identity=dependency):
        if domain not in DOMAINS:
            raise failure(422, '记忆分类无效。')
        with auth.identity(identity):
            return memory.core.days(actor(identity),start_day,end_day,domains=[domain])

    @router.get('/preferences')
    def preferences_list(identity=dependency):
        with auth.identity(identity):
            return {'items':memory.list_preferences(actor(identity))}

    @router.get('/ai-profile')
    def ai_profile(identity=dependency):
        with auth.identity(identity):
            return memory.ai_profile(actor(identity))

    @router.put('/ai-profile')
    def save_ai_profile(value: AIProfileChange, identity=dependency):
        with auth.identity(identity):
            return memory.save_ai_profile(actor(identity), value.expected_revision,
                                          value.request_id, [item.model_dump() for item in value.entries])

    @router.post('/preferences/{key}/feedback')
    def feedback(key: str, value: PreferenceFeedback, identity=dependency):
        with auth.identity(identity):
            return memory.feedback(actor(identity),key,value.action,value.expected_revision)

    @router.post('/preferences/summarize')
    async def summarize(value: Empty, request: Request, identity=dependency):
        actor(identity)
        if summarizer is None:
            raise failure(503,'偏好整理暂不可用。')
        return await summarizer.run(request,identity)

    return router
