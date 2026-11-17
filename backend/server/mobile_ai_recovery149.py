"""Owner-scoped, append-only durable AI attempts; no prompt or private reasoning.

The existing INSERT/SELECT-only rpc_requests table is the journal. One stable
claim prevents a duplicate paid request. Public results are bounded immutable
chunks so the existing 128 KiB table contract is preserved without new DDL.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ollama_chat_app.data.database import timestamp_from_db, timestamp_to_db
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc

NAMESPACE = UUID('de935e22-26d6-4ad0-99c7-0df8cd18335f')
CHUNK_BYTES = 48000
MAX_RESULT_BYTES = 32 * 1024 * 1024
HEARTBEAT_SECONDS = 30
LEASE_SECONDS = 90
TERMINAL = frozenset({'complete', 'failed', 'cancelled', 'expired'})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class RecoveryError(RuntimeError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def operation_uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RecoveryError('invalid_ai_operation', 422) from None
    return value


def operation_kind(value):
    if type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_.:-]{1,79}', value):
        raise RecoveryError('invalid_ai_operation', 422)
    return value


def collaboration_rows(response):
    """Summarize only fixed application-observed roles from a saved response."""
    from ollama_chat_app.services.chat_activity import COLLABORATION_ROLES
    steps=response.get('tool_steps',[])
    runs=response.get('agent_runs',[])
    malformed_steps=type(steps)is not list or any(type(row)is not dict for row in steps)
    team='agent_runs' in response or malformed_steps or (type(steps)is list and any(
        type(row)is dict and type(row.get('tool'))is str and row['tool'] in COLLABORATION_ROLES for row in steps))
    if not team:return [],False,False
    states={};incomplete=malformed_steps;main_complete=False;indexes=set()
    def add(tool,status):
        nonlocal incomplete
        if type(tool)is not str or tool not in COLLABORATION_ROLES:return
        if type(status)is not str or status not in {'done','failed','working'}:
            incomplete=True;return
        # A completed answer cannot persist an unfinished child as working.
        final='done' if status=='done' else 'failed'
        if tool not in states or final=='failed':states[tool]=final
    if type(steps)is not list:
        incomplete=True
    else:
        for row in steps:
            if type(row)is dict:add(row.get('tool'),row.get('status'))
    if type(runs)is not list:
        incomplete=True
    else:
        for row in runs:
            if type(row)is not dict:
                incomplete=True;continue
            usage=row.get('usage')
            if (set(row)!={'index','agent','model','status','usage'}
                    or type(row['index'])is not int or not 0<=row['index']<5 or row['index'] in indexes
                    or type(row['model'])is not str or not row['model'].strip() or len(row['model'])>100
                    or type(row['agent'])is not str or type(row['status'])is not str
                    or row['status'] not in {'started','complete','failed'}
                    or usage is not None and (type(usage)is not dict
                        or set(usage)-{'prompt_tokens','completion_tokens','total_tokens'}
                        or any(type(value)is not int or not 0<=value<=2000000 for value in usage.values()))):
                incomplete=True;continue
            indexes.add(row['index'])
            agent=row.get('agent')
            if agent=='main':
                if row['status']=='complete':main_complete=True
                else:incomplete=True
                continue
            if agent not in COLLABORATION_ROLES:
                incomplete=True;continue
            status={'complete':'done','failed':'failed','started':'working'}.get(row.get('status'))
            add(agent,status)
    if len(states)>2:return [],True,True
    # No observed child can mean main-only only after a real, well-formed
    # completed main call. Empty or partial metadata cannot prove that fact.
    if not states and not main_complete:incomplete=True
    return [{'tool':tool,'status':status}for tool,status in states.items()],incomplete,True


class AIRecovery149:
    def __init__(self, auth, *, chat_service=None, tool_service=None, memory_service=None, clock=time.time):
        self.auth, self.db, self.clock = auth, auth.database, clock
        self.chat, self.tools, self.memory = chat_service, tool_service, memory_service

    def _id(self, actor, op, purpose):
        return str(uuid5(NAMESPACE, f'{actor}:{op}:{purpose}'))

    def _digest(self, actor, op):
        return hashlib.sha256(f'fawnsee-ai149:{actor}:{op}'.encode()).hexdigest()

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def _authorize(self, principal, conn):
        actor = principal.user.id
        row = conn.execute('SELECT role_code,account_status FROM users WHERE id=?', (actor,)).fetchone()
        if row is None or row['role_code'] != 'member' or row['account_status'] != 'active':
            raise RecoveryError('ai_operation_permission', 403)
        session = conn.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',
            (principal.token_digest, actor, timestamp_to_db(self._now()))).fetchone()
        if session is None:
            raise RecoveryError('authentication_required', 401)
        return actor

    def _row(self, conn, actor, op, purpose):
        row = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_id=?',
            (actor, self._id(actor, op, purpose))).fetchone()
        if row is None:
            return None
        value = json.loads(row['response_json'])
        if type(value) is not dict or set(value) != {'ai149'}:
            raise RecoveryError('ai_operation_receipt_invalid', 503)
        return value['ai149']

    def _latest(self, conn, actor, op):
        row = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_digest=? ORDER BY created_at DESC LIMIT 1',
            (actor, self._digest(actor, op))).fetchone()
        if row is None:
            return None
        value = json.loads(row['response_json'])
        if type(value) is not dict or set(value) != {'ai149'}:
            raise RecoveryError('ai_operation_receipt_invalid', 503)
        return value['ai149']

    def _save(self, conn, actor, op, body, *, purpose=None):
        value = canonical({'ai149': body})
        if len(value.encode()) > 100000:
            raise RecoveryError('ai_operation_receipt_too_large', 503)
        previous = conn.execute('SELECT MAX(created_at) AS last_at FROM rpc_requests WHERE user_id=? AND request_digest=?',
            (actor, self._digest(actor, op))).fetchone()
        stamp = self._now()
        if previous and previous['last_at']:
            last = timestamp_from_db(previous['last_at'])
            if stamp <= last:
                stamp = last + timedelta(microseconds=1)
        conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
            (actor, self._id(actor, op, purpose) if purpose else str(uuid4()),
             self._digest(actor, op), value, timestamp_to_db(stamp)))

    def peek_claim(self, principal, metadata, payload):
        """Check an existing attempt before any new embedding/rerank work."""
        op, kind = operation_uuid(metadata['operation_id']), operation_kind(metadata['kind'])
        digest = hashlib.sha256(canonical({key: value for key, value in payload.items() if key != '_ai149'}).encode()).hexdigest()
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            prior = self._row(conn, actor, op, 'claim')
            if prior is None:
                return None
            if prior['kind'] != kind or prior['request_hash'] != digest:
                raise RecoveryError('ai_operation_payload_conflict')
            current = self._current(conn, actor, op, prior)
            if current['state'] == 'complete':
                self._check_memory(principal, prior)
                return {'replay': self._result(conn, actor, op, current), **prior}
            raise RecoveryError('ai_operation_' + current['state'])

    def claim(self, principal, metadata, payload, *, personal_retrieval=None):
        op, kind = operation_uuid(metadata['operation_id']), operation_kind(metadata['kind'])
        digest = hashlib.sha256(canonical({key: value for key, value in payload.items() if key != '_ai149'}).encode()).hexdigest()
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            prior = self._row(conn, actor, op, 'claim')
            if prior is not None:
                if prior['kind'] != kind or prior['request_hash'] != digest:
                    raise RecoveryError('ai_operation_payload_conflict')
                current = self._current(conn, actor, op, prior)
                if current['state'] == 'complete':
                    self._check_memory(principal, prior)
                    return {'replay': self._result(conn, actor, op, current), **prior}
                raise RecoveryError('ai_operation_' + current['state'])
            meta = payload.get('_ai139', {})
            body = {'operation_id': op, 'kind': kind, 'actor_id': actor, 'request_hash': digest,
                'state': 'running', 'started_at': self.clock(), 'heartbeat_at': self.clock(),
                'assistant_message_id': meta.get('assistant_message_id'), 'conversation_id': meta.get('conversation_id'),
                'personal_context': bool(meta.get('personal_context')), 'memory_revision': meta.get('memory_revision')}
            if personal_retrieval is not None:
                validator = getattr(self.memory, 'multimodal_validator', None)
                if not body['personal_context'] or validator is None:
                    raise RecoveryError('personal_context_unavailable', 503)
                try:
                    validator(actor, personal_retrieval)
                except (PermissionError, ValueError, RuntimeError):
                    raise RecoveryError('personal_context_changed') from None
                body['personal_retrieval'] = personal_retrieval
            self._save(conn, actor, op, body, purpose='claim')
            return body

    def _check_memory(self, principal, claim):
        if claim.get('personal_context'):
            if self.memory is None:
                raise RecoveryError('personal_context_unavailable', 503)
            with self.auth.identity(principal):
                current = self.memory.settings(principal.user.id)
            if not current['enabled'] or current['revision'] != claim.get('memory_revision'):
                raise RecoveryError('personal_context_changed')
            if claim.get('personal_retrieval') is not None:
                validator = getattr(self.memory, 'multimodal_validator', None)
                if validator is None:
                    raise RecoveryError('personal_context_unavailable', 503)
                try:
                    with self.auth.identity(principal):
                        validator(principal.user.id, claim['personal_retrieval'])
                except (PermissionError, ValueError, RuntimeError):
                    raise RecoveryError('personal_context_changed') from None
            with self.auth.identity(principal),self.db.connect()as conn:
                proofs=self._tool_proofs(conn,principal.user.id,claim['operation_id'])
            if proofs:
                validator=getattr(self.tools,'validate_context_proof',None)
                if not callable(validator):raise RecoveryError('personal_context_unavailable',503)
                try:
                    for proof in proofs:validator(principal,proof)
                except (PermissionError,ValueError,RuntimeError):
                    raise RecoveryError('personal_context_changed')from None

    def _tool_proof_digest(self, actor, op):
        return hashlib.sha256(f'fawnsee-ai149-tool-sources:{actor}:{op}'.encode()).hexdigest()

    def _tool_proofs(self, conn, actor, op):
        from .ai139_tools import tool_proof_budget
        rows=conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_digest=? ORDER BY created_at,request_id LIMIT 25',
            (actor,self._tool_proof_digest(actor,op))).fetchall()
        proofs=[]
        for row in rows:
            value=json.loads(row['response_json'])
            if type(value)is not dict or set(value)!={'ai149_tool_sources'}:raise RecoveryError('ai_operation_receipt_invalid',503)
            proofs.append(value['ai149_tool_sources'])
        try:tool_proof_budget(proofs)
        except (ValueError,TypeError,AttributeError):raise RecoveryError('ai_operation_receipt_invalid',503)from None
        return proofs

    def remember_tool_proof(self, principal, claim, proof):
        """Append source hashes separately; never change a request fingerprint."""
        from .ai139_tools import tool_proof_budget
        op=operation_uuid(claim['operation_id'])
        validator=getattr(self.tools,'validate_context_proof',None)
        if not claim.get('personal_context')or not callable(validator):raise RecoveryError('personal_context_unavailable',503)
        with self.auth.identity(principal),self.db.transaction()as conn:
            actor=self._authorize(principal,conn)
            original=self._row(conn,actor,op,'claim')
            if (original is None or original['request_hash']!=claim['request_hash']
                    or not original.get('personal_context')):raise RecoveryError('ai_operation_receipt_invalid',503)
            current=self._current(conn,actor,op,original)
            if current['state']!='running':raise RecoveryError('ai_operation_'+current['state'])
            self._check_memory(principal,original)
            try:
                validator(principal,proof)
                previous=self._tool_proofs(conn,actor,op)
                if proof in previous:return
                tool_proof_budget(previous+[proof])
            except (PermissionError,ValueError,RuntimeError):raise RecoveryError('personal_context_changed')from None
            encoded=canonical({'ai149_tool_sources':proof})
            rid=self._id(actor,op,'tool-sources:'+hashlib.sha256(encoded.encode()).hexdigest())
            conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
                (actor,rid,self._tool_proof_digest(actor,op),encoded,timestamp_to_db(self._now())))

    def _current(self, conn, actor, op, claim):
        terminal = self._row(conn, actor, op, 'terminal')
        if terminal is not None:
            return terminal
        current = self._latest(conn, actor, op) or claim
        if self.clock() - current.get('heartbeat_at', claim['heartbeat_at']) > LEASE_SECONDS:
            current = {**claim, 'state': 'expired', 'error_code': 'ai_operation_interrupted', 'finished_at': self.clock()}
            self._save(conn, actor, op, current, purpose='terminal')
            self._fail_pending(actor, claim, '上次回答已中断，请重新发起。')
        return current

    def remember_team_call(self, principal, claim, call):
        """Durable bounded call audit; never store prompts or reasoning here.

        A root claim is never retried automatically, including when an upstream
        charged call lost its response. A started receipt means usage unknown.
        """
        allowed = {'index', 'agent', 'model', 'status', 'usage'}
        if (type(call) is not dict or set(call) != allowed
                or type(call['index']) is not int or not 0 <= call['index'] < 5
                or call['agent'] not in {'main', 'organize_record_drafts', 'analyze_record_trends', 'plan_daily_life'}
                or call['status'] not in {'started', 'complete', 'failed'}
                or type(call['model']) is not str or len(call['model']) > 100):
            raise RecoveryError('invalid_team_receipt', 503)
        usage = call['usage']
        if usage is not None and (type(usage) is not dict or set(usage) - {'prompt_tokens', 'completion_tokens', 'total_tokens'}
                or any(type(v) is not int or not 0 <= v <= 2000000 for v in usage.values())):
            raise RecoveryError('invalid_team_receipt', 503)
        op = operation_uuid(claim['operation_id'])
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            original = self._row(conn, actor, op, 'claim')
            if original is None or original['request_hash'] != claim['request_hash']:
                raise RecoveryError('ai_operation_receipt_invalid', 503)
            current = self._current(conn, actor, op, original)
            if current['state'] != 'running':
                raise RecoveryError('ai_operation_' + current['state'])
            self._check_memory(principal, original)
            purpose = f"team:{call['index']}:{call['status']}"
            rid = self._id(actor, op, purpose)
            if conn.execute('SELECT request_id FROM rpc_requests WHERE user_id=? AND request_id=?', (actor, rid)).fetchone() is not None:
                raise RecoveryError('team_call_already_recorded', 409)
            # A separate digest keeps heartbeat/current-state selection limited
            # to root state records; audit rows cannot masquerade as root state.
            digest = hashlib.sha256(f'fawnsee-ai149-team:{actor}:{op}'.encode()).hexdigest()
            conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
                (actor, rid, digest, canonical({'ai149_team_call': call}), timestamp_to_db(self._now())))

    def heartbeat(self, principal, claim):
        op = claim['operation_id']
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            current = self._current(conn, actor, op, claim)
            if current['state'] != 'running':
                raise RecoveryError('ai_operation_' + current['state'])
            if self.clock() - current.get('heartbeat_at', 0) >= HEARTBEAT_SECONDS:
                self._save(conn, actor, op, {**claim, 'state': 'running', 'heartbeat_at': self.clock()})

    def finish(self, principal, claim, response, *, finalize=None):
        raw = canonical(response).encode()
        if len(raw) > MAX_RESULT_BYTES:
            raise RecoveryError('ai_operation_result_too_large', 502)
        op = claim['operation_id']
        # UTF-8 is kept as a JSON string per chunk. Slicing by characters with
        # a conservative four-byte bound avoids splitting an encoded codepoint.
        text = raw.decode(); pieces = [text[i:i + CHUNK_BYTES // 4] for i in range(0, len(text), CHUNK_BYTES // 4)]
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            self._check_memory(principal, claim)
            current = self._current(conn, actor, op, claim)
            if current['state'] == 'complete':
                return self._result(conn, actor, op, current)
            if current['state'] != 'running':
                raise RecoveryError('ai_operation_' + current['state'])
            for index, piece in enumerate(pieces):
                self._save(conn, actor, op, {'piece': piece, 'index': index}, purpose='result:' + str(index))
            if finalize is not None:
                finalize()
            done = {**claim, 'state': 'complete', 'finished_at': self.clock(), 'chunks': len(pieces),
                'result_bytes': len(raw), 'result_sha256': hashlib.sha256(raw).hexdigest()}
            self._save(conn, actor, op, done, purpose='terminal')
        return response

    def _result(self, conn, actor, op, terminal):
        count = terminal.get('chunks')
        if type(count) is not int or not 1 <= count <= 3000:
            raise RecoveryError('ai_operation_receipt_invalid', 503)
        pieces = []
        for index in range(count):
            row = self._row(conn, actor, op, 'result:' + str(index))
            if row is None or row.get('index') != index or type(row.get('piece')) is not str:
                raise RecoveryError('ai_operation_receipt_invalid', 503)
            pieces.append(row['piece'])
        raw = ''.join(pieces).encode()
        if len(raw) != terminal['result_bytes'] or hashlib.sha256(raw).hexdigest() != terminal['result_sha256']:
            raise RecoveryError('ai_operation_receipt_invalid', 503)
        value = json.loads(raw)
        if type(value) is not dict:
            raise RecoveryError('ai_operation_receipt_invalid', 503)
        return value

    def _fail_pending(self, actor, claim, message):
        mid = claim.get('assistant_message_id')
        if self.chat is not None and type(mid) is int:
            existing = self.chat.get_message(actor, mid)
            if existing is not None and existing.status == 'pending':
                self.chat.fail_message(actor, mid, message, provider='deepseek_cloud', model='deepseek-flash')

    def fail(self, principal, claim, code='default_ai_incomplete', *, cancelled=False):
        op = claim['operation_id']
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = principal.user.id
            # A disconnect may coincide with session revocation. Terminal state
            # still needs saving under the captured actor, but grants no reads.
            if self._row(conn, actor, op, 'terminal') is not None:
                return
            state = 'cancelled' if cancelled else 'failed'
            self._save(conn, actor, op, {**claim, 'state': state, 'error_code': code, 'finished_at': self.clock()}, purpose='terminal')
            self._fail_pending(actor, claim, '已停止回答。' if cancelled else '本次回答未完整生成，请重新发起。')

    def _legacy(self, conn, actor, op, kind):
        if kind != 'chat.send' or self.chat is None:
            return None
        rid = str(uuid5(UUID(op), 'chat.begin_message:0'))
        row = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_id=?', (actor, rid)).fetchone()
        if row is None:
            return None
        try:
            exchange = decode_rpc(json.loads(row['response_json'])['result'])
            assistant = self.chat.get_message(actor, exchange.assistant_message.id)
            if assistant is None:
                return None
            value = {'operation_id': op, 'kind': kind, 'conversation_id': exchange.conversation.id,
                'assistant_message_id': assistant.id, 'requires_new_operation': True}
            if assistant.status == 'complete':
                value.update(state='complete', retry_safe=False,
                    response={'content': assistant.content, 'model': assistant.model, 'sources': [], 'proposals': []})
            elif assistant.status == 'error':
                value.update(state='failed', retry_safe=True, error_code='ai_generation_failed')
            else:
                value.update(state='running', retry_safe=False, error_code='legacy_operation_unconfirmed')
            return value
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RecoveryError('ai_operation_receipt_invalid', 503) from None

    def view(self, principal, op, kind):
        operation_uuid(op); operation_kind(kind)
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            claim = self._row(conn, actor, op, 'claim')
            if claim is None:
                return self._legacy(conn, actor, op, kind) or {'operation_id': op, 'kind': kind,
                    'state': 'not_started', 'retry_safe': True, 'requires_new_operation': False}
            if claim['kind'] != kind:
                raise RecoveryError('ai_operation_not_found', 404)
            current = self._current(conn, actor, op, claim)
            value = {'operation_id': op, 'kind': kind, 'state': current['state'],
                'retry_safe': current['state'] in {'failed', 'cancelled', 'expired'},
                'requires_new_operation': current['state'] != 'running',
                'conversation_id': claim.get('conversation_id'), 'assistant_message_id': claim.get('assistant_message_id')}
            if current['state'] == 'complete':
                self._check_memory(principal, claim)
                value['response'] = self._result(conn, actor, op, current)
            if current.get('error_code'):
                value['error_code'] = current['error_code']
            return value

    def cancel(self, principal, op, kind):
        operation_uuid(op); operation_kind(kind)
        with self.auth.identity(principal), self.db.transaction() as conn:
            actor = self._authorize(principal, conn)
            claim = self._row(conn, actor, op, 'claim')
            if claim is None:
                legacy = self._legacy(conn, actor, op, kind)
                if legacy and legacy['state'] == 'running':
                    self._fail_pending(actor, legacy, '已停止此前未确认的回答。')
                elif legacy is None:
                    claim={'operation_id':op,'kind':kind,'actor_id':actor,'request_hash':'0'*64,
                        'state':'cancelled','heartbeat_at':self.clock(),'started_at':self.clock(),
                        'error_code':'cancelled','finished_at':self.clock()}
                    self._save(conn,actor,op,claim,purpose='claim')
                    self._save(conn,actor,op,claim,purpose='terminal')
                return self.view(principal, op, kind)
            if claim['kind'] != kind:
                raise RecoveryError('ai_operation_not_found', 404)
            self.fail(principal, claim, 'cancelled', cancelled=True)
            return self.view(principal, op, kind)

    def restore_chat(self,principal,op,kind):
        return self._restore_chat_with_status(principal,op,kind)[0]

    def _restore_chat_with_status(self,principal,op,kind):
        """Complete only this paid attempt's owned placeholder, never new text."""
        operation_uuid(op);operation_kind(kind)
        if kind!='chat.send' or self.chat is None:
            raise RecoveryError('ai_chat_restore_not_available',422)
        from ollama_chat_app.services.knowledge_chat import _public_answer
        from ollama_chat_app.providers.platform_deepseek import _sources
        from ollama_chat_app.services.chat_activity import validate_activity,activity_json
        with self.auth.identity(principal),self.db.transaction() as conn:
            actor=self._authorize(principal,conn)
            claim=self._row(conn,actor,op,'claim')
            if claim is None or claim['kind']!=kind:
                raise RecoveryError('ai_operation_not_found',404)
            current=self._current(conn,actor,op,claim)
            if current['state']!='complete':raise RecoveryError('ai_operation_'+current['state'])
            self._check_memory(principal,claim)
            response=self._result(conn,actor,op,current)
            mid,cid=claim.get('assistant_message_id'),claim.get('conversation_id')
            if type(mid) is not int or type(cid) is not int:
                raise RecoveryError('ai_chat_restore_not_available',422)
            message=self.chat.get_message(actor,mid)
            if message is None or message.role!='assistant' or message.conversation_id!=cid:
                raise RecoveryError('ai_operation_not_found',404)
            sources=_sources(response.get('sources',[]))
            changed=message.status!='complete'
            if changed:
                if message.status not in {'pending','error'}:
                    raise RecoveryError('ai_chat_restore_not_available')
                answer=_public_answer(response['content'])
                missing=[row for row in sources if row['url'] not in answer]
                if missing:
                    lines=['\n\n参考来源：']
                    for row in missing:
                        title=row['title'].replace('[','（').replace(']','）').replace('\n',' ')
                        url=row['url'].replace(' ','%20').replace('(','%28').replace(')','%29')
                        lines.append('- ['+title+']('+url+')'+(' · '+row['date'] if row.get('date') else ''))
                    answer+='\n'.join(lines)
                if len(answer)>2_000_000:raise RecoveryError('ai_chat_restore_too_large',413)
                if message.status=='error':
                    # The immutable paid completion is the sole authority for
                    # reopening this exact placeholder. No client text is used.
                    conn.execute("UPDATE messages SET status='pending',error_message=NULL WHERE id=? AND conversation_id=? AND role='assistant' AND status='error'",(mid,cid))
                message=self.chat.complete_message(actor,mid,answer,provider='deepseek_cloud',model=response['model'])
                finished=datetime.fromtimestamp(current['finished_at'],timezone.utc)
                started=datetime.fromtimestamp(claim['started_at'],timezone.utc)
                if finished-started>timedelta(minutes=130):started=finished
                steps=[]
                if sources:steps.append({'kind':'web_search','status':'done','label':'搜索了公开网页','detail':'搜索了公开网页。'})
                raw_steps=response.get('tool_steps',[])
                application_steps=[row for row in raw_steps if type(row)is dict
                    and type(row.get('tool'))is str and row['tool'] not in {'web_search','fetch_webpage'}] if type(raw_steps)is list else []
                collaboration,incomplete,team=collaboration_rows(response)
                if application_steps or response.get('proposals') or team:
                    succeeded=bool(response.get('proposals')) or any(row.get('status')=='done' for row in application_steps)
                    status='done' if succeeded else 'failed'
                    detail='已生成待确认操作，尚未保存。' if response.get('proposals') else '查询了本人授权的资料。' if succeeded else '应用工具未完成。'
                    if team:
                        from ollama_chat_app.services.chat_activity import collaboration_detail
                        detail=collaboration_detail(collaboration,incomplete=incomplete)
                        status=('failed' if incomplete or any(row['status']=='failed' for row in collaboration)
                                else 'done' if collaboration or succeeded else 'skipped')
                    steps.append({'kind':'controlled_tools','status':status,
                        'label':{'done':'调用了应用工具','failed':'应用工具未完成','skipped':'未调用应用工具'}[status],
                        'detail':detail})
                steps.append({'kind':'answer','status':'done','label':'生成了回答','detail':'回答已整理并保存。'})
                public=validate_activity({'version':1,'status':'complete',
                    'started_at':started.isoformat(),'finished_at':finished.isoformat(),'steps':steps})
                conn.execute("INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,'ai.activity_recovered','message',?,?,?)",
                    (actor,mid,activity_json(public),timestamp_to_db(self._now())))
            rows=self.chat.list_activity(actor,cid,[mid])
            activity=next((r['activity'] for r in rows if r['assistant_message_id']==mid),None)
            user=conn.execute("SELECT id FROM messages WHERE conversation_id=? AND sequence_no=? AND role='user'",(cid,message.sequence_no-1)).fetchone()
            if user is None:raise RecoveryError('ai_chat_restore_not_available')
            conversation=self.chat.get_conversation(actor,cid)
            assistant=jsonable_encoder(message)
            assistant.update(sources=sources,proposals=response.get('proposals',[]),tool_steps=response.get('tool_steps',[]))
            if activity is not None:assistant['activity']=activity
            result={'conversation':jsonable_encoder(conversation),'user_message':jsonable_encoder(self.chat.get_message(actor,user['id'])),
                'assistant_message':assistant,'recovered':True,'operation_id':op}
        return result,changed


class CancelOperation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    kind: str = Field(min_length=2, max_length=80)


def create_ai_recovery149_router(proxy):
    router = APIRouter()
    async def identity(request):
        _token, principal = await proxy.authorize(request)
        if proxy.operation_ledger is None:
            raise RecoveryError('ai_recovery_unavailable', 503)
        return principal
    def failed(error):
        return JSONResponse({'detail': error.code}, status_code=error.status)

    def project_drafts(value, record_dialogue=None):
        response = value.get('response')
        if not isinstance(response, dict) or not isinstance(response.get('record_drafts'), list):
            return value
        from .record_assistant_try12 import DIALOGUE_PHOTOS_CAPABILITY, DIALOGUE_TIME_CAPABILITIES, project_record_photo_drafts
        drafts = project_record_photo_drafts(
            response['record_drafts'], photo_enabled=True, dialogue_enabled=True,
            time_hints_enabled=record_dialogue in DIALOGUE_TIME_CAPABILITIES,
            extended_photos=record_dialogue == DIALOGUE_PHOTOS_CAPABILITY,
        )
        return {**value, 'response': {**response, 'record_drafts': drafts}}

    @router.get('/v1/ai/operations/{operation_id}')
    async def get(operation_id: str, kind: str, request: Request, record_dialogue: str | None = None):
        import asyncio
        try:
            principal = await identity(request)
            result = await asyncio.to_thread(proxy.operation_ledger.view, principal, operation_id, kind)
            return project_drafts(result, record_dialogue)
        except RecoveryError as error:
            return failed(error)
        except Exception as error:
            return JSONResponse({'detail': getattr(error,'code','ai_recovery_unavailable')},status_code=getattr(error,'status',503))
    @router.post('/v1/ai/operations/{operation_id}/cancel')
    async def cancel(operation_id: str, value: CancelOperation, request: Request):
        import asyncio
        try:
            principal = await identity(request)
            return project_drafts(await asyncio.to_thread(proxy.operation_ledger.cancel, principal, operation_id, value.kind))
        except RecoveryError as error:
            return failed(error)
        except Exception as error:
            return JSONResponse({'detail': getattr(error,'code','ai_recovery_unavailable')},status_code=getattr(error,'status',503))
    @router.post('/v1/ai/operations/{operation_id}/restore-chat')
    async def restore(operation_id:str,value:CancelOperation,request:Request,background:BackgroundTasks):
        import asyncio
        try:
            principal=await identity(request)
            result,changed=await asyncio.to_thread(proxy.operation_ledger._restore_chat_with_status,principal,operation_id,value.kind)
            if changed:
                from .mobile_sync149 import schedule_projection149
                schedule_projection149(request,background,principal,('ai_records',))
            return result
        except RecoveryError as error:return failed(error)
        except Exception as error:
            return JSONResponse({'detail':getattr(error,'code','ai_recovery_unavailable')},status_code=getattr(error,'status',503))
    return router
