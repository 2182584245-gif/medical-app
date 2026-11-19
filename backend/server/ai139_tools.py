"""Server-owned member tools and confirmed-write receipts, over existing RLS tables.

No user SQL, model-supplied actor, arbitrary HTTP endpoint, or unconfirmed write
is executable. Public sources and signed proposals live in owner-scoped RPC
receipts so history can recover their actual status without another model call.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import math
import re
import time
from uuid import UUID, uuid4
from ollama_chat_app.data.database import timestamp_to_db, timestamp_from_db


class ToolError(ValueError):
    def __init__(self, code='invalid_tool_request', status=422):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def text(value, maximum, optional=False):
    if optional and value is None:return None
    if type(value) is not str or not value.strip() or len(value)>maximum:raise ToolError()
    return value.strip()


def aware(value):
    result=datetime.fromisoformat(text(value,80).replace('Z','+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:raise ToolError()
    return result.isoformat()


def object_args(value, allowed, required=()):
    if type(value) is not dict or set(value)-set(allowed) or not set(required)<=set(value):raise ToolError()
    # Reject NaN, byte strings and other non-JSON leaves before any service work.
    if len(canonical(value).encode())>12000:raise ToolError('tool_arguments_too_large')
    return value


def lifestyle_title(value):
    # Tool writes have the narrower scope of general daily-life reminders.
    # Existing health validation is also retained, including its translations.
    if re.search(r'药|服用|胰岛素|注射|处方|剂量|medicin|medicat|prescript|\bpills?\b|\bdrug\b|\binsulin\b',value,re.I):
        raise ToolError('medical_reminder_not_supported')
    return value


READ_DOMAINS={
 'read_profile':('profile','个人资料'), 'read_life_records':('life_records','生活记录'),
 'record_statistics':('life_records','生活记录统计'), 'read_reminders':('reminders','提醒'),
 'read_goals':('goals_habits','目标与习惯'), 'read_reading':('reading','阅读记录'),
 'read_ai_history':('ai_records','AI对话记录'), 'read_preferences':('preferences','用户偏好'),
}
WRITE_TOOLS={'prepare_record':'life_record','prepare_reminder':'reminder','prepare_goal':'goal'}
LABELS={**{name:label for name,(_,label) in READ_DOMAINS.items()},
 'web_search':'联网查询','fetch_webpage':'读取网页','prepare_record':'准备生活记录',
 'prepare_reminder':'准备生活提醒','prepare_goal':'准备目标与习惯'}
DOMAIN_NAMES=('profile','life_records','reminders','goals_habits','services_appointments','reading','shopping','forum','operations','preferences','ai_records')


def tool_proof_budget(proofs):
    if (type(proofs)is not list or len(proofs)>24 or len(canonical(proofs).encode('utf-8'))>60000
            or sum(len(p.get('references',[]))+len((p.get('multimodal')or{}).get('references',[]))
                   +(p.get('profile')is not None)for p in proofs)>128):
        raise ToolError('personal_context_budget_exceeded',413)


def schema(name, description, properties, required=()):
    return {'type':'function','function':{'name':name,'description':description,
      'parameters':{'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}


def tool_schemas(*, personal=False, writes=False, web=False):
    result=[]
    common={'start_date':{'type':'string','description':'可选开始日期 YYYY-MM-DD'},'end_date':{'type':'string','description':'可选结束日期 YYYY-MM-DD'},
      'question':{'type':'string','description':'本人正在询问的具体问题，最多300字'}}
    if personal:
        for name,(_,label) in READ_DOMAINS.items():
            current=name in {'read_profile','read_preferences'}
            properties={'question':common['question']} if current else common
            description=('读取当前登录用户授权且未忘记的当前'+label+'，用于用户眼下的问题或未来计划；当前设置不按计划日期筛选。'
                         if current else '只查询当前登录用户授权且未忘记的'+label+'。保留来源性质、日期及范围；未记录不能解释为未发生。')
            result.append(schema(name,description,properties))
    if writes:
        result += [schema('prepare_record','只为用户明确说已经发生的事实创建待确认记录，不会保存。时间或数量不清楚先询问。',
          {'category':{'type':'string','enum':['diet','water','activity','sleep','environment','medical','metric']},'occurred_at':{'type':'string','description':'用户确认的发生时间，ISO8601含时区'},
           'content':{'type':'string','description':'忠实于用户原话的简短内容'},'details':{'type':'object','description':'对应记录字段；不推测缺失数值'}},('category','occurred_at','content','details')),
          schema('prepare_reminder','准备一般生活提醒，等待用户确认，不自行保存；不能设置用药或治疗提醒。',
           {'title':{'type':'string'},'scheduled_at':{'type':'string','description':'ISO8601含时区'},'reminder_type':{'type':'string'},'repeat_rule':{'type':['string','null'],'enum':['daily','weekly',None]}},('title','scheduled_at')),
          schema('prepare_goal','准备一个待用户确认的目标与习惯，不能自动建立。',
           {'title':{'type':'string'},'kind':{'type':'string','enum':['water','diet','weight','glucose','reading','custom']},'target':{'type':['number','null']},'unit':{'type':['string','null']},'frequency':{'type':'string','enum':['daily','weekly']},'reminder_time':{'type':['string','null'],'description':'HH:MM或null'}},('title','kind'))]
    if web:
        result += [schema('web_search','仅检索公共问题。只在用户明确要求联网或最新事实时使用；不得把个人身份、联系方式、档案原文或系统资料发到搜索。',
             {'query':{'type':'string','description':'公共检索词，最多128字，不含任何个人身份或档案内容'}},('query',)),
           schema('fetch_webpage','读取本次联网搜索已返回的公开网页，不能读取其他URL。',{'url':{'type':'string'}},('url',))]
    return result


class AI139Tools:
    def __init__(self, auth, services, *, memory_service=None, clock=time.time):
        self.auth,self.db,self.services,self.memory,self.clock=auth,auth.database,services,memory_service,clock
        self.signing_key=hmac.new(bytes.fromhex(auth.settings.token_pepper.get_secret_value()),b'ai139-confirmation-v1',hashlib.sha256).digest()

    def _member(self, principal):
        if principal.user.role_code!='member' or principal.user.account_status!='active':raise ToolError('tool_permission_denied',403)
        return principal.user.id

    def bind_message(self, principal, assistant_message_id, conversation_id):
        actor=self._member(principal)
        if type(assistant_message_id) is not int or assistant_message_id<=0 or type(conversation_id) is not int or conversation_id<=0:raise ToolError('tool_message_required')
        with self.auth.identity(principal),self.db.connect() as conn:
            row=conn.execute('SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND m.role=? AND c.id=? AND c.user_id=?',
                             (assistant_message_id,'assistant',conversation_id,actor)).fetchone()
        if row is None:raise ToolError('tool_message_unavailable',403)
        return {'actor':actor,'assistant_message_id':assistant_message_id,'conversation_id':conversation_id}

    def _receipt_digest(self, actor, message):
        return hashlib.sha256(f'fawnsee-ai139-run:{actor}:{message}'.encode()).hexdigest()

    def _read(self, conn, actor, message):
        row=conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_digest=? ORDER BY created_at DESC LIMIT 1',
                         (actor,self._receipt_digest(actor,message))).fetchone()
        if row is None:return None
        value=json.loads(row['response_json'])
        if type(value) is not dict or set(value)!={'ai139_run'}:raise ToolError('tool_receipt_conflict',409)
        result=value['ai139_run']
        if result.get('assistant_message_id')!=message or result.get('actor')!=actor:raise ToolError('tool_receipt_conflict',409)
        return result

    def _save(self, conn, actor, run):
        value=canonical({'ai139_run':run})
        if len(value.encode())>100000:raise ToolError('tool_receipt_too_large')
        # rpc_requests deliberately grants runtime only INSERT/SELECT. Append a
        # new immutable state instead of broadening its existing RLS privileges.
        fingerprint=self._receipt_digest(actor,run['assistant_message_id'])
        previous=conn.execute('SELECT MAX(created_at) AS last_at FROM rpc_requests WHERE user_id=? AND request_digest=?',
                              (actor,fingerprint)).fetchone()
        stamp=datetime.fromtimestamp(self.clock(),timezone.utc)
        if previous and previous['last_at']:
            last=timestamp_from_db(previous['last_at'])
            if stamp<=last:stamp=last+timedelta(microseconds=1)
        conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
                     (actor,str(uuid4()),fingerprint,value,timestamp_to_db(stamp)))

    def begin(self, principal, meta):
        identity=self.bind_message(principal,meta.get('assistant_message_id'),meta.get('conversation_id'))
        with self.auth.identity(principal),self.db.transaction() as conn:
            current=self._read(conn,identity['actor'],identity['assistant_message_id'])
            message=conn.execute('SELECT status FROM messages WHERE id=?',(identity['assistant_message_id'],)).fetchone()
            if message is None or message['status']!='pending':raise ToolError('tool_message_not_pending',409)
            if current is None:self._save(conn,identity['actor'],{**identity,'status':'running','sources':[],'proposals':[]})
            elif current.get('status')=='complete':raise ToolError('tool_message_not_pending',409)
        return identity

    def finish(self, principal, meta):
        identity=self.bind_message(principal,meta['assistant_message_id'],meta['conversation_id'])
        with self.auth.identity(principal),self.db.transaction() as conn:
            current=self._read(conn,identity['actor'],identity['assistant_message_id'])
            if current is None:raise ToolError('proposal_unavailable',404)
            current['status']='complete';self._save(conn,identity['actor'],current)

    def set_record_offer(self, principal, meta):
        """Mark an offer on this owned assistant message without creating a draft."""
        from .record_offer11862 import reports_health_fact
        identity = self.bind_message(principal, meta['assistant_message_id'], meta['conversation_id'])
        actor, message = identity['actor'], identity['assistant_message_id']
        with self.auth.identity(principal), self.db.transaction() as conn:
            prior = conn.execute(
                """SELECT source.id, substr(source.content,1,1201) AS content
                   FROM messages current_message JOIN conversations c ON c.id=current_message.conversation_id
                   JOIN messages source ON source.conversation_id=c.id
                       AND source.sequence_no=current_message.sequence_no-1
                   WHERE c.user_id=? AND c.id=? AND current_message.id=? AND current_message.role='assistant'
                     AND source.role='user'""",
                (actor, identity['conversation_id'], message),
            ).fetchone()
            run = self._read(conn, actor, message)
            if (not prior or not reports_health_fact(prior['content']) or not run
                    or run.get('status') != 'running' or run.get('record_drafts')):
                return False
            run['record_offer'] = prior['id']
            self._save(conn, actor, run)
            return True

    def prior_record_offer(self, principal, meta):
        """Accept or decline only the offer in the immediately preceding owned exchange."""
        from .record_offer11862 import accepts_offer, declines_offer, offer_details, only_declines_offer, reports_health_fact
        identity = self.bind_message(principal, meta['assistant_message_id'], meta['conversation_id'])
        actor, message = identity['actor'], identity['assistant_message_id']
        with self.auth.identity(principal), self.db.connect() as conn:
            prior = conn.execute(
                """SELECT offered.id AS offer_id, source.id AS source_id,
                          source.created_at AS source_at, substr(source.content,1,1201) AS source_text,
                          substr(reply.content,1,241) AS reply_text
                   FROM messages current_message JOIN conversations c ON c.id=current_message.conversation_id
                   JOIN messages reply ON reply.conversation_id=c.id AND reply.sequence_no=current_message.sequence_no-1
                   JOIN messages offered ON offered.conversation_id=c.id AND offered.sequence_no=current_message.sequence_no-2
                   JOIN messages source ON source.conversation_id=c.id AND source.sequence_no=current_message.sequence_no-3
                   WHERE c.user_id=? AND c.id=? AND current_message.id=? AND current_message.role='assistant'
                     AND reply.role='user' AND offered.role='assistant' AND source.role='user'""",
                (actor, identity['conversation_id'], message),
            ).fetchone()
            if not prior:
                return None
            run = self._read(conn, actor, prior['offer_id'])
        if (not run or run.get('status') != 'complete' or run.get('record_offer') != prior['source_id']
                or not reports_health_fact(prior['source_text'])):
            return None
        if accepts_offer(prior['reply_text']):
            return {'accepted': True, 'text': prior['source_text'],
                    'created_at': timestamp_from_db(prior['source_at']).isoformat(),
                    'assistant_message_id': prior['offer_id'],
                    'detail': offer_details(prior['reply_text'])}
        if declines_offer(prior['reply_text']):
            return {'accepted': False, 'only_decline': only_declines_offer(prior['reply_text'])}
        return None

    def list_run(self, principal, *, assistant_message_id=None, conversation_id=None,assistant_message_ids=None,
                 photo_enabled=False, dialogue_enabled=False, time_hints_enabled=False, extended_photos=False):
        actor=self._member(principal)
        projection={'photo_enabled':photo_enabled,'dialogue_enabled':dialogue_enabled,
                    'time_hints_enabled':time_hints_enabled,'extended_photos':extended_photos}
        selected=None
        if assistant_message_ids is not None:
            if assistant_message_id is not None or type(assistant_message_ids) is not str or len(assistant_message_ids)>4096:raise ToolError()
            parts=assistant_message_ids.split(',')
            if not 1<=len(parts)<=200 or any(re.fullmatch('[1-9][0-9]{0,18}',part) is None for part in parts):raise ToolError()
            selected={int(part) for part in parts}
            if len(selected)!=len(parts) or any(ident>9223372036854775807 for ident in selected):raise ToolError()
        with self.auth.identity(principal),self.db.connect() as conn:
            if assistant_message_id is not None:
                message=conn.execute('SELECT m.id,c.id AS conversation_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND m.role=? AND c.user_id=?',
                   (assistant_message_id,'assistant',actor)).fetchone()
                if message is None:raise ToolError('tool_message_unavailable',404)
                result=self._read(conn,actor,assistant_message_id) or {'assistant_message_id':assistant_message_id,'sources':[],'proposals':[]}
                return self._public_run(result, **projection)
            if type(conversation_id) is not int or conversation_id<=0:raise ToolError()
            own=conn.execute('SELECT id FROM conversations WHERE id=? AND user_id=?',(conversation_id,actor)).fetchone()
            if own is None:raise ToolError('tool_message_unavailable',404)
            if selected is None:
                messages=conn.execute('SELECT id FROM messages WHERE conversation_id=? AND role=? ORDER BY sequence_no DESC LIMIT 200',(conversation_id,'assistant')).fetchall()
            else:
                placeholders=','.join('?' for _ in selected)
                messages=conn.execute('SELECT id FROM messages WHERE conversation_id=? AND role=? AND id IN ('+placeholders+') ORDER BY sequence_no DESC',
                                     (conversation_id,'assistant',*sorted(selected))).fetchall()
                if {row['id'] for row in messages}!=selected:raise ToolError('tool_message_unavailable',404)
            return {'messages':[self._public_run(current, **projection)
                                for row in messages
                                if (current:=self._read(conn,actor,row['id'])) is not None]}

    def _public_run(self, run, *, photo_enabled=False, dialogue_enabled=False, time_hints_enabled=False,
                    extended_photos=False):
        proposals=[]
        for original in run['proposals']:
            proposal=dict(original)
            if proposal['status']=='pending' and proposal['expires_at']<=self.clock():proposal['status']='expired'
            proposals.append(proposal)
        public={'assistant_message_id':run['assistant_message_id'],'sources':run['sources'],'proposals':proposals}
        if run.get('record_receipts'):
            public['record_receipts'] = run['record_receipts']
        if 'record_drafts' in run:
            from .record_assistant_try12 import project_record_photo_drafts
            public['record_drafts']=project_record_photo_drafts(run['record_drafts'], photo_enabled=photo_enabled,
                                                               dialogue_enabled=dialogue_enabled,
                                                               time_hints_enabled=time_hints_enabled,
                                                               extended_photos=extended_photos)
            if run.get('record_assistant_try12'):
                public['record_drafts_ready']=run.get('status')=='complete'
        return public

    def set_record_drafts(self, principal, meta, drafts):
        """Persist only model suggestions in existing owned message metadata.

        This is not a health record, a draft outbox operation or a source fact.
        The client still imports it into its encrypted local draft explicitly.
        """
        from .record_assistant_try12 import CAPABILITY, DIALOGUE_CAPABILITIES, PHOTO_CAPABILITY
        capability=meta.get('assistant_team')
        structured=capability in {CAPABILITY,PHOTO_CAPABILITY,*DIALOGUE_CAPABILITIES}
        if (not structured and capability!='try1.1-v1' or type(drafts) is not list
                or len(drafts)>3 or len(canonical(drafts).encode())>(45000 if structured else 24000)):raise ToolError()
        identity=self.bind_message(principal,meta['assistant_message_id'],meta['conversation_id'])
        with self.auth.identity(principal),self.db.transaction() as conn:
            current=self._read(conn,identity['actor'],identity['assistant_message_id'])
            if current is None or current.get('status')!='running':raise ToolError('tool_message_not_pending',409)
            from .record_tasks11864 import CAPABILITY as TASKS_CAPABILITY, RecordTasks
            tasks_enabled = meta.get('record_tasks') == TASKS_CAPABILITY
            task_source = meta.get('_record_task_prior') if tasks_enabled else None
            if capability in DIALOGUE_CAPABILITIES:
                self._replace_record_drafts(conn, identity, drafts, task_source=task_source)
            if tasks_enabled:
                store = RecordTasks(self)
                if drafts:
                    store.apply(conn, identity, drafts, task_source)
                elif focus := meta.get('_record_task_focus'):
                    store.finish_focus(conn, identity, focus)
            current['record_drafts']=drafts
            if structured:
                current['record_assistant_try12']=True
            self._save(conn,identity['actor'],current)

    def _replace_record_drafts(self, conn, identity, drafts, *, task_source=None):
        from .record_assistant_try12 import _has_saved_draft, draft_replacement_marker
        replaced={draft['supersedes_draft_id']:draft['id'] for draft in drafts if 'supersedes_draft_id' in draft}
        if not replaced:
            return
        actor=identity['actor']
        if task_source:
            from .record_tasks11864 import RecordTasks
            selected = RecordTasks(self)._get(conn, actor, identity['conversation_id'], task_source['id'])
            if (not selected or selected['draft_id'] != task_source['draft_id']
                    or set(replaced) != {selected['draft_id']}):
                raise ToolError('record_draft_changed',409)
            previous = {'id': selected['assistant_message_id']}
        else:
            previous=conn.execute(
                """SELECT prior.id FROM messages current_message
               JOIN conversations c ON c.id=current_message.conversation_id
               JOIN messages prior ON prior.conversation_id=c.id AND prior.sequence_no<current_message.sequence_no
               WHERE c.user_id=? AND c.id=? AND current_message.id=? AND prior.role='assistant'
               ORDER BY prior.sequence_no DESC LIMIT 1""",
                (actor,identity['conversation_id'],identity['assistant_message_id']),
            ).fetchone()
        prior=self._read(conn,actor,previous['id']) if previous else None
        if not prior or prior.get('status') != 'complete':
            raise ToolError('record_draft_changed',409)
        existing={draft['id']:draft for draft in prior.get('record_drafts',[]) if not draft.get('blocked')}
        if not set(replaced).issubset(existing) or any(_has_saved_draft(conn,actor,value) for value in replaced):
            raise ToolError('record_draft_changed',409)
        stamp=timestamp_to_db(datetime.fromtimestamp(self.clock(),timezone.utc))
        for old,new in replaced.items():
            marker=draft_replacement_marker(actor,old)
            fingerprint=hashlib.sha256(f'record-dialogue:{actor}:{old}'.encode()).hexdigest()
            conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
                         (actor,marker,fingerprint,canonical({'superseded_record_draft':new}),stamp))
            existing[old].update(blocked=True,superseded=True)
        self._save(conn,actor,prior)

    def set_sources(self, principal, meta, sources):
        identity=self.bind_message(principal,meta['assistant_message_id'],meta['conversation_id'])
        with self.auth.identity(principal),self.db.transaction() as conn:
            current=self._read(conn,identity['actor'],identity['assistant_message_id'])
            if current is None:current={**identity,'status':'running','sources':[],'proposals':[]}
            current['sources']=sources[:8]
            self._save(conn,identity['actor'],current)

    def read_tool(self, principal, name, arguments, *, personal_context):
        return self.read_tool_with_proof(principal,name,arguments,personal_context=personal_context)[0]

    def read_tool_with_proof(self, principal, name, arguments, *, personal_context):
        actor=self._member(principal)
        if not personal_context or name not in READ_DOMAINS or self.memory is None:raise ToolError('personal_context_disabled',403)
        object_args(arguments,('start_date','end_date','question'))
        start,end=arguments.get('start_date'),arguments.get('end_date')
        for value in (start,end):
            if value is not None:
                if type(value) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) is None:raise ToolError()
                datetime.fromisoformat(value)
        if start and end and not 0<=(datetime.fromisoformat(end)-datetime.fromisoformat(start)).days<=365:raise ToolError('tool_date_range_too_large')
        domain,label=READ_DOMAINS[name]
        with self.auth.identity(principal):
            config=self.memory.settings(actor)
            control=next((r for r in config['controls'] if r['domain']==domain),None)
            if not config['enabled'] or not control or not control['enabled_for_ai']:raise ToolError('personal_context_disabled',403)
            question=label+' '+text(arguments.get('question',label),300)
            if start:question+=' 从'+start
            if end:question+=' 至'+end
            data=self.memory.core.context(actor,question,max_chars=14000,controls={d:d==domain for d in DOMAIN_NAMES},
                current_state=name in {'read_profile','read_preferences'})
            # Settings are re-read after retrieval; a changed permission cannot be delivered.
            if self.memory.settings(actor)['revision']!=config['revision']:raise ToolError('personal_context_changed',409)
            proof=self._context_proof(data,config['revision'])
            self.validate_context_proof(principal,proof)
            return data,proof

    @staticmethod
    def _context_proof(data, settings_revision):
        """Only source identities/hashes leave the live context for recovery."""
        from .member_memory_core import fingerprint
        from .member_multimodal11842 import PROOF_FIELDS
        references={}
        def add(item):
            if type(item)is not dict or 'ref'not in item:return
            ref={k:item.get(k)for k in ('ref','source_hash','kind')}
            previous=references.get(ref['ref'])
            if previous is not None and previous!=ref:raise ToolError('personal_context_changed',409)
            references[ref['ref']]=ref
        for item in data.get('evidence',[]):add(item)
        # A context budget may omit the separate evidence list while retaining
        # its actual raw record. Guard every original actually handed to a tool.
        for name in ('raw_records','confirmed_facts','inferred_preferences','ai_history'):
            for item in data.get(name,[]):
                add(item)
                for support in item.get('evidence',[]):add(support)
        if len(references)>128:raise ToolError('personal_context_budget_exceeded',413)
        mm=data.get('multimodal_references',[])
        multimodal=None
        if mm:
            multimodal={'settings_revision':settings_revision,
                'model_revision':data.get('capability_coverage',{}).get('multimodal_retrieval',{}).get('model_revision'),
                'references':[{k:item.get(k)for k in PROOF_FIELDS}for item in mm]}
        profile=data.get('profile',{})
        return {'schema':'ai139-tool-sources-v1','settings_revision':settings_revision,
            'references':list(references.values()),'multimodal':multimodal,
            'profile':{'fields':sorted(profile),'sha256':fingerprint(profile)}if profile else None}

    def validate_context_proof(self, principal, proof):
        """No model calls; recheck consumed tool originals before every round."""
        if type(proof) is dict and proof.get('schema') == 'record-assistant-target-try12-v1':
            from .record_assistant_try12 import validate_target_proof
            return validate_target_proof(self, principal, proof)
        if type(proof) is dict and proof.get('schema') == 'assistant-team-statistics-try11-v1':
            from .assistant_team_try11 import validate_statistics_proof
            return validate_statistics_proof(self, principal, proof)
        from .member_memory_core import PROFILE_FIELDS,_row_hash,fingerprint,iso
        actor=self._member(principal)
        if (type(proof)is not dict or set(proof)!={'schema','settings_revision','references','multimodal','profile'}
                or proof['schema']!='ai139-tool-sources-v1' or type(proof['references'])is not list
                or len(proof['references'])>128):raise ToolError('personal_context_changed',409)
        with self.auth.identity(principal):
            self.memory.core._actor(actor)
            config=self.memory.settings(actor)
            if not config['enabled']or config['revision']!=proof['settings_revision']:raise ToolError('personal_context_changed',409)
            with self.db.connect()as conn:
                session=conn.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',
                    (principal.token_digest,actor,timestamp_to_db(datetime.now(timezone.utc)))).fetchone()
                if session is None:raise ToolError('personal_context_changed',409)
            ordinary=[];assistant=[]
            for item in proof['references']:
                if (type(item)is not dict or set(item)!={'ref','source_hash','kind'}or type(item['ref'])is not str
                        or len(item['ref'])>240 or re.fullmatch('[a-f0-9]{64}',str(item['source_hash']))is None):
                    raise ToolError('personal_context_changed',409)
                table,_,identifier=item['ref'].partition(':')
                if table=='messages'and item['kind']=='model_inferred':
                    if not identifier.isdigit()or not 0<int(identifier)<2**53:raise ToolError('personal_context_changed',409)
                    assistant.append((item,int(identifier)))
                else:ordinary.append(item)
            found=self.memory.core.resolve_references(actor,ordinary)if ordinary else {}
            if any(item['ref']not in found or found[item['ref']]['source_hash']!=item['source_hash']for item in ordinary):
                raise ToolError('personal_context_changed',409)
            if assistant:
                with self.db.connect()as conn:
                    for item,identifier in assistant:
                        row=conn.execute("SELECT m.id,m.conversation_id,m.sequence_no,m.role,substr(m.content,1,1600) AS content,m.status,m.created_at,m.updated_at,c.title FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.id=? AND m.role='assistant' AND (m.provider IS NULL OR m.provider<>'fawnsee-welcome')",(actor,identifier)).fetchone()
                        if row is None or _row_hash(dict(row))!=item['source_hash']:raise ToolError('personal_context_changed',409)
            profile=proof['profile']
            if profile is not None:
                allowed=set(PROFILE_FIELDS)|{'updated_at'}
                if (type(profile)is not dict or set(profile)!={'fields','sha256'}or type(profile['fields'])is not list
                        or len(profile['fields'])>len(allowed)or any(type(k)is not str or k not in allowed for k in profile['fields'])
                        or re.fullmatch('[a-f0-9]{64}',str(profile['sha256']))is None):raise ToolError('personal_context_changed',409)
                control=next((r for r in config['controls']if r['domain']=='profile'),None)
                if not control or not control['enabled_for_ai']:raise ToolError('personal_context_changed',409)
                with self.db.connect()as conn:
                    row=conn.execute('SELECT '+','.join(PROFILE_FIELDS)+',updated_at FROM member_profiles WHERE user_id=?',(actor,)).fetchone()
                if row is None:raise ToolError('personal_context_changed',409)
                current=dict(row);current['updated_at']=iso(current['updated_at'])
                if fingerprint({k:current[k]for k in profile['fields']})!=profile['sha256']:raise ToolError('personal_context_changed',409)
            if proof['multimodal']is not None:
                validator=getattr(self.memory,'multimodal_validator',None)
                if not callable(validator):raise ToolError('personal_context_unavailable',503)
                try:
                    if validator(actor,proof['multimodal'])is not True:raise ToolError('personal_context_changed',409)
                except (PermissionError,RuntimeError):raise ToolError('personal_context_changed',409)from None
        return True

    def clean_proposal(self, name, arguments):
        health=self.services['health']
        if name=='prepare_record':
            body=object_args(arguments,('category','occurred_at','content','details'),('category','occurred_at','content','details'))
            category=body['category'];stamp=aware(body['occurred_at']);content=text(body['content'],2000)
            if category=='metric':
                details=object_args(body['details'],('kind','value','unit','context','notes'),('kind','value','unit'))
                validator=self.services['expansion']._observation_data
                clean=validator(details)
                return 'observation',{'kind':details['kind'],'value':clean['value'],'unit':clean['unit'],'context':clean.get('context','unspecified'),'notes':clean.get('notes'),'measured_at':stamp},'记录身体指标'
            category=health._validate_category(category)
            details=health._validate_record_details(category,body['details'])
            health._normalize_local_timestamp(stamp)
            return 'life_record',{'category':category,'occurred_at':stamp,'content':content,'details':details},'添加生活记录'
        if name=='prepare_reminder':
            body=object_args(arguments,('title','scheduled_at','reminder_type','repeat_rule'),('title','scheduled_at'))
            title=lifestyle_title(text(body['title'],200));stamp=aware(body['scheduled_at']);kind=health._validate_reminder(title,body.get('reminder_type','custom'))
            health._reminder_datetime(stamp);repeat=health._validate_repeat_rule(body.get('repeat_rule'))
            return 'reminder',{'title':title,'scheduled_at':stamp,'reminder_type':kind,'repeat_rule':repeat},'添加生活提醒'
        if name=='prepare_goal':
            body=object_args(arguments,('title','kind','target','unit','frequency','reminder_time'),('title','kind'))
            if body['kind'] not in {'water','diet','weight','glucose','reading','custom'}:raise ToolError()
            clean=self.services['expansion']._goal_data(body)
            lifestyle_title(clean['title'])
            # Reuse medication reminder rejection for scheduled goals as well.
            if clean.get('reminder_time'):health._validate_reminder(clean['title'],'custom')
            return 'goal',{'kind':body['kind'],**clean},'添加目标与习惯'
        raise ToolError('unsupported_tool')

    @staticmethod
    def proposal_display(kind, body):
        """Complete, human-readable review fields; execution uses signed body."""
        categories={'diet':'饮食','water':'饮水','activity':'运动','sleep':'睡眠','environment':'居住环境','medical':'医疗检查','metric':'身体指标'}
        kinds={'water':'饮水','diet':'饮食','weight':'体重','glucose':'血糖','reading':'阅读','custom':'自定义'}
        labels={'category':'类别','occurred_at':'发生时间','content':'记录内容','scheduled_at':'提醒时间','title':'名称',
          'reminder_type':'提醒类型','repeat_rule':'重复方式','kind':'指标或目标','value':'数值','unit':'单位',
          'context':'测量情境','notes':'备注','measured_at':'测量时间','target':'目标值','frequency':'频率','reminder_time':'提醒时刻',
          'amount_ml':'饮水量（毫升）','volume_ml':'饮水量（毫升）','duration_minutes':'时长（分钟）','steps':'步数',
          'calories':'热量（千卡）','meal_type':'餐次','food':'食物','sleep_hours':'睡眠时长（小时）','quality':'感受',
          'temperature_c':'温度（℃）','humidity_percent':'湿度（%）','ventilation_minutes':'通风时长（分钟）','comfort_score':'居住感受分数',
          'duration_hours':'睡眠时长（小时）','calories_kcal':'热量（千卡）','amount_g':'食物重量（克）','energy_kcal':'运动耗能（千卡）',
          'quality_score':'环境质量评分','event_type':'医疗事项类型','name':'事项名称','description':'说明','location':'地点','activity_type':'运动类型'}
        values={'daily':'每天','weekly':'每周','custom':'一般生活提醒','water':'饮水','diet':'饮食','weight':'体重','glucose':'血糖','reading':'阅读',
          'unspecified':'未注明','fasting':'空腹','before_meal':'餐前','after_meal':'餐后','random':'随机','breakfast':'早餐','lunch':'午餐','dinner':'晚餐','snack':'加餐',
          'activity':'运动','sleep':'睡眠','environment':'居住环境','followup':'生活回访','membership':'会员事项',
          'consultation':'看病就诊','medication':'已发生的用药事实','other':'其他'}
        fields=[]
        def add(key,value):
            if value is None:return
            if isinstance(value,dict):
                for nested,item in value.items():add(nested,item)
                return
            if isinstance(value,list):shown='、'.join(str(v) for v in value)
            elif key=='category':shown=categories.get(value,str(value))
            elif key=='kind':shown=kinds.get(value,str(value))
            elif type(value) is bool:shown='是' if value else '否'
            else:shown=values.get(str(value),str(value))
            fields.append({'label':labels.get(key,'补充信息'), 'value':shown})
        for key,value in body.items():add(key,value)
        summary=(f"{categories.get(body.get('category'), '生活')}：{body['content']}" if kind=='life_record' else
          f"{kinds.get(body.get('kind'), '身体指标')}：{body['value']} {body['unit']}" if kind=='observation' else body['title'])
        return summary,fields

    def propose(self, principal, meta, name, arguments):
        identity=self.bind_message(principal,meta['assistant_message_id'],meta['conversation_id'])
        kind,body,title=self.clean_proposal(name,arguments)
        ident=str(uuid4());expiry=int(self.clock())+1200
        signed={**identity,'id':ident,'kind':kind,'body':body,'expires_at':expiry,
                'memory_revision':meta.get('memory_revision') if meta.get('personal_context') else None}
        raw=canonical(signed).encode();encoded=base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
        signature=hmac.new(self.signing_key,encoded.encode(),hashlib.sha256).hexdigest()
        summary,fields=self.proposal_display(kind,body)
        body_digest=hashlib.sha256(canonical(body).encode()).hexdigest()
        proposal={'id':ident,'kind':kind,'title':title,'summary':summary,'fields':fields,'body_digest':body_digest,
                  'token':encoded+'.'+signature,'expires_at':expiry,'status':'pending'}
        with self.auth.identity(principal),self.db.transaction() as conn:
            current=self._read(conn,identity['actor'],identity['assistant_message_id']) or {**identity,'status':'running','sources':[],'proposals':[]}
            # Identical tool retries in one response do not generate duplicate cards.
            for existing in current['proposals']:
                if existing['kind']==kind and existing.get('body_digest')==body_digest:return existing
            if len(current['proposals'])>=3:raise ToolError('proposal_limit_reached')
            current['proposals'].append(proposal);self._save(conn,identity['actor'],current)
        return proposal

    def _unsign(self, token):
        if type(token) is not str or len(token)>20000:raise ToolError('invalid_confirmation')
        try:
            encoded,signature=token.split('.')
            if not hmac.compare_digest(signature,hmac.new(self.signing_key,encoded.encode(),hashlib.sha256).hexdigest()):raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
            if set(value)!={'actor','assistant_message_id','conversation_id','id','kind','body','expires_at','memory_revision'}:raise ValueError()
            UUID(value['id'])
            return value
        except (TypeError,ValueError,KeyError,UnicodeError):raise ToolError('invalid_confirmation') from None

    def _verified_entity(self, conn, actor, kind, result):
        """A service return value is not evidence that an owned row exists."""
        identifier=result.get('id') if type(result) is dict else None
        table={'life_record':'life_records','observation':'expansion_observations',
               'reminder':'reminders','goal':'expansion_goals'}.get(kind)
        if table is None or type(identifier) is not int or identifier<=0:
            raise ToolError('proposal_persistence_unverified',503)
        row=conn.execute('SELECT id,user_id FROM '+table+' WHERE id=? AND user_id=?',
                         (identifier,actor)).fetchone()
        if row is None or row['id']!=identifier or row['user_id']!=actor:
            raise ToolError('proposal_persistence_unverified',503)
        return True

    def _confirmed_receipt(self, conn, actor, proposal):
        saved=proposal['status']=='saved'
        entity=None
        if saved:
            self._verified_entity(conn,actor,proposal['kind'],proposal.get('result'))
            if proposal['kind']=='life_record':
                # Return the ordinary owner-scoped readback, not model fields.
                # This lets the captured native account update its local list
                # immediately, without waiting for every resource to resync.
                row=self.services['expansion'].get_record(actor,{'record_id':proposal['result']['id']})
                if type(row) is not dict or row.get('id')!=proposal['result']['id'] or row.get('user_id')!=actor:
                    raise ToolError('proposal_persistence_unverified',503)
                allowed={'id','user_id','category','occurred_at','local_date','timezone_offset_minutes',
                         'content','details','source','created_at','updated_at','version','linked_meal_id'}
                entity={key:value for key,value in row.items() if key in allowed}
        return {'proposal_id':proposal['id'],'kind':proposal['kind'],'status':proposal['status'],
                'result':proposal.get('result'),'persistence_verified':saved,
                'actor_id':actor,'result_entity':entity}

    def act(self, principal, proposal_token, request_id, *, cancel=False):
        actor=self._member(principal)
        try:
            if type(request_id) is not str or str(UUID(request_id))!=request_id:raise ValueError()
        except (TypeError,ValueError,AttributeError):raise ToolError('invalid_confirmation') from None
        signed=self._unsign(proposal_token)
        if signed['actor']!=actor:raise ToolError('tool_permission_denied',403)
        self.bind_message(principal,signed['assistant_message_id'],signed['conversation_id'])
        with self.auth.identity(principal),self.db.transaction() as conn:
            current_user=conn.execute('SELECT role_code,account_status FROM users WHERE id=?',(actor,)).fetchone()
            if current_user is None or current_user['role_code']!='member' or current_user['account_status']!='active':raise ToolError('tool_permission_denied',403)
            session=conn.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',
               (principal.token_digest,actor,timestamp_to_db(datetime.fromtimestamp(self.clock(),timezone.utc)))).fetchone()
            if session is None:raise ToolError('tool_permission_denied',403)
            current=self._read(conn,actor,signed['assistant_message_id'])
            proposal=next((p for p in current['proposals'] if p['id']==signed['id']),None) if current else None
            if proposal is None or not hmac.compare_digest(proposal['token'],proposal_token):raise ToolError('proposal_unavailable',404)
            # Same proposal is idempotent even if the network retry uses a new operation ID.
            if proposal['status'] in {'saved','cancelled'}:
                return self._confirmed_receipt(conn,actor,proposal)
            message=conn.execute('SELECT status FROM messages WHERE id=?',(signed['assistant_message_id'],)).fetchone()
            if not cancel and (current.get('status')!='complete' or message is None or message['status']!='complete'):
                raise ToolError('proposal_answer_incomplete',409)
            if signed['expires_at']<=self.clock():raise ToolError('proposal_expired',409)
            if signed.get('memory_revision') and not cancel:
                if self.memory is None or self.memory.settings(actor)['revision']!=signed['memory_revision']:raise ToolError('personal_context_changed',409)
            if cancel:result=None;status='cancelled'
            else:
                body=signed['body'];kind=signed['kind']
                if kind=='life_record':result={'id':self.services['health'].add_life_record(actor,source='ai_confirmed',**body)}
                elif kind=='observation':result=self.services['expansion'].create_observation(actor,body)
                elif kind=='reminder':result={'id':self.services['health'].add_reminder(actor,**body)}
                elif kind=='goal':result=self.services['expansion'].create_goal(actor,body)
                else:raise ToolError('unsupported_tool')
                self._verified_entity(conn,actor,kind,result)
                status='saved'
            proposal.update({'status':status,'result':json.loads(canonical(result)) if result is not None else None})
            receipt=self._confirmed_receipt(conn,actor,proposal)
            self._save(conn,actor,current)
        return receipt
