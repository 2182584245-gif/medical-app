"""Bounded, owner-authorized Qwen retrieval over retained original evidence.

The cache is derived data, never an archive or a replacement for exact daily
statistics. No public URL, OCR model, whole-account export or background sweep.
"""
from __future__ import annotations

from contextlib import nullcontext, contextmanager
from datetime import datetime, timezone, timedelta
import base64
import hashlib
import io
import json
import math
import re
import threading
import time

from .member_memory_core import DOMAINS, canonical, fingerprint, timestamp, _evidence, parsed_json

TABLE = 'member_multimodal_vectors11842'
EXT = 'medical_app_extensions'
MAX_NEW_TEXT = 6
MAX_NEW_IMAGES = 2
MAX_CANDIDATES = 8
MAX_REFERENCES = 3
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_INDEX_ROWS = 8192
MAX_CONTENT = 2000
PIPELINE = 'owner-evidence-v1:excerpt2000:original-sha256:exif-rgb-jpeg85-edge1024'
SOURCE_TABLES = frozenset({'life_records', 'expansion_observations', 'expansion_meals', 'medical_reports', 'reminders',
    'expansion_goals', 'expansion_habit_entries', 'visit_records', 'visit_tasks',
    'expansion_content_progress', 'orders', 'forum_posts', 'forum_comments',
    'forum_likes', 'member_memory_events', 'messages', 'chat_attachments'})
PICTURE_TYPES = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}
PROOF_FIELDS = ('ref', 'domain', 'source_hash', 'occurred_at', 'kind', 'modality',
    'chunk_no', 'content_hash', 'image_source_ref', 'image_sha256')


class MultimodalUnavailable(RuntimeError):
    pass


class StalePersonalSources(PermissionError):
    def __init__(self):
        super().__init__('personal_context_changed')


def vector_literal(values):
    if (type(values) is not list or len(values) != 512 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
        raise MultimodalUnavailable('invalid_semantic_vector')
    norm = math.sqrt(sum(float(v) ** 2 for v in values))
    if not 0.999 <= norm <= 1.001:
        raise MultimodalUnavailable('invalid_semantic_vector_norm')
    return '[' + ','.join(format(float(v), '.9g') for v in values) + ']'


def parsed_ref(value, allowed=SOURCE_TABLES):
    if type(value) is not str or len(value) > 240:
        raise StalePersonalSources()
    table, separator, key = value.partition(':')
    if not separator or table not in allowed or not key or len(key) > 200:
        raise StalePersonalSources()
    from .vector_coverage11847 import is_target, source_ref
    if is_target(value):
        try:
            source_ref(value)
        except ValueError:
            raise StalePersonalSources() from None
        return table, key
    if table not in {'expansion_content_progress', 'member_memory_events'}:
        if not key.isdigit() or not 0 < int(key) < 2**53:
            raise StalePersonalSources()
    return table, key


def document_hash(item):
    from .vector_coverage11847 import is_target, PIPELINE as FULL_PIPELINE
    full = is_target(item.get('ref')) and item.get('modality') == 'text'
    return fingerprint({key: item.get(key) for key in ('ref', 'source_hash',
        'modality', 'chunk_no', 'content', 'image_source_ref', 'image_sha256')}
        | {'pipeline': FULL_PIPELINE if full else PIPELINE})


def _safe_data(value):
    # Opaque storage handles never improve the provider's semantic features.
    if isinstance(value, dict):
        return {k: _safe_data(v) for k, v in value.items()
                if type(k) is str and not k.endswith(('_id', '_ids'))
                and k not in {'id', 'sha256', 'source_hash'}}
    if isinstance(value, list):
        return [_safe_data(v) for v in value[:40]]
    return value


class PostgresSemanticStore:
    """Only fixed SQL over a private owner/model partition."""
    def __init__(self, database):
        self.db = database

    def available(self):
        return getattr(self.db, 'dialect', None) == 'postgresql'

    def ledger(self, actor, model):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                'SELECT source_ref,source_hash,modality,chunk_no,content_hash,image_source_ref,image_sha256 '
                'FROM ' + TABLE + ' WHERE user_id=? AND model_revision=? ORDER BY updated_at DESC LIMIT ?',
                (actor, model, MAX_INDEX_ROWS)).fetchall()]

    def ledger_for(self, actor, model, refs):
        """Exact bounded source lookup, including vectors beyond ledger's page."""
        if not refs:
            return []
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                'SELECT source_ref,source_hash,modality,chunk_no,content_hash,image_source_ref,image_sha256 '
                'FROM ' + TABLE + ' WHERE user_id=? AND model_revision=? AND source_ref IN ('
                + ','.join('?' for _ in refs) + ')', (actor, model, *refs)).fetchall()]

    def upsert(self, actor, model, documents, vectors, conn):
        at = datetime.now(timezone.utc).isoformat()
        for item, values in zip(documents, vectors):
            conn.execute('DELETE FROM ' + TABLE + ' WHERE user_id=? AND source_ref=? '
                'AND model_revision=? AND source_hash<>?',
                (actor, item['ref'], model, item['source_hash']))
            conn.execute('INSERT INTO ' + TABLE + '(user_id,source_ref,domain,source_hash,model_revision,'
                'chunk_no,modality,content,content_hash,image_source_ref,image_sha256,occurred_at,updated_at,embedding) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CAST(? AS ' + EXT + '.vector)) '
                'ON CONFLICT(user_id,source_ref,model_revision,modality,chunk_no) DO UPDATE SET '
                'domain=excluded.domain,source_hash=excluded.source_hash,content=excluded.content,'
                'content_hash=excluded.content_hash,image_source_ref=excluded.image_source_ref,'
                'image_sha256=excluded.image_sha256,occurred_at=excluded.occurred_at,'
                'updated_at=excluded.updated_at,embedding=excluded.embedding',
                (actor, item['ref'], item['domain'], item['source_hash'], model,
                 item['chunk_no'], item['modality'], item['content'], item['content_hash'],
                 item.get('image_source_ref'), item.get('image_sha256'), item['occurred_at'], at,
                 vector_literal(values)))

    def nearest(self, actor, model, values, state, start, end):
        clauses, params = [], []
        for domain in sorted(state['domains']):
            part = '(domain=?'
            params.append(domain)
            cutoff = state['controls'][domain].get('forget_before')
            if cutoff:
                part += ' AND occurred_at>?'
                params.append(cutoff)
            clauses.append(part + ')')
        if not clauses:
            return []
        sql = ('SELECT source_ref AS ref,domain,source_hash,modality,chunk_no,content,content_hash,'
            'image_source_ref,image_sha256,occurred_at,embedding OPERATOR(' + EXT + '.<=>) '
            'CAST(? AS ' + EXT + '.vector) AS cosine_distance FROM ' + TABLE +
            ' WHERE user_id=? AND model_revision=? AND (' + ' OR '.join(clauses) + ') '
            'AND occurred_at>=? AND occurred_at<? ORDER BY cosine_distance,source_ref,modality,chunk_no LIMIT ?')
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql,
                (vector_literal(values), actor, model, *params, start, end, MAX_CANDIDATES)).fetchall()]

    def invalidate(self, actor, domains, conn):
        if domains is None:
            conn.execute('DELETE FROM ' + TABLE + ' WHERE user_id=?', (actor,))
        elif domains:
            conn.execute('DELETE FROM ' + TABLE + ' WHERE user_id=? AND domain IN (' +
                ','.join('?' for _ in domains) + ')', (actor, *sorted(domains)))

    def invalidate_sources(self, actor, refs, conn):
        if refs:
            conn.execute('DELETE FROM ' + TABLE + ' WHERE user_id=? AND source_ref IN (' +
                ','.join('?' for _ in refs) + ')', (actor, *refs))
            from .vector_coverage11847 import is_target, source_ref
            for ref in refs:
                if is_target(ref):
                    root = source_ref(ref)[3]
                    conn.execute('DELETE FROM ' + TABLE + ' WHERE user_id=? AND (source_ref=? OR source_ref LIKE ?)',
                                 (actor, root, root + ':part:%'))

    def remove(self, actor, items, conn):
        for item in items:
            conn.execute('DELETE FROM ' + TABLE + ' WHERE user_id=? AND source_ref=? '
                'AND model_revision=? AND modality=? AND chunk_no=? AND content_hash=?',
                (actor, item['ref'], item['model_revision'], item['modality'],
                 item['chunk_no'], item['content_hash']))

    def invalidate_conversations(self, actor, identifiers, conn, *, attachments=True):
        """Run before source deletion, in its transaction; no transcript load."""
        marks=','.join('?' for _ in identifiers)
        message_ref="'messages:'||CAST(m.id AS TEXT)"
        conn.execute('DELETE FROM '+TABLE+' WHERE user_id=? AND EXISTS('
            'SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id '
            'WHERE c.user_id=? AND c.id IN('+marks+') AND ('+TABLE+'.source_ref='+message_ref+
            ' OR '+TABLE+'.source_ref LIKE '+message_ref+"||':part:%'))",(actor,actor,*identifiers))
        if attachments:
            conn.execute('DELETE FROM '+TABLE+' WHERE user_id=? AND EXISTS('
                'SELECT 1 FROM chat_attachments a JOIN messages m ON m.id=a.message_id '
                'JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND c.id IN('+marks+') '
                "AND "+TABLE+".source_ref='chat_attachments:'||CAST(a.id AS TEXT))",(actor,actor,*identifiers))


class MemberMultimodalRetrieval:
    def __init__(self, database, *, core, settings_reader, client=None, store=None, monotonic=None):
        self.db, self.core, self.settings_reader = database, core, settings_reader
        self.client = client
        self.store = store or PostgresSemanticStore(database)
        self._lock = threading.RLock()
        self.monotonic = monotonic or time.monotonic

    def _budget(self, deadline):
        if self.monotonic() >= deadline:
            raise MultimodalUnavailable('personal_semantic_deadline')

    @contextmanager
    def _locked(self, deadline):
        self._budget(deadline)
        if not self._lock.acquire(timeout=min(5.0, max(0.0, deadline-self.monotonic()))):
            raise MultimodalUnavailable('personal_semantic_busy')
        try:
            self._budget(deadline)
            yield
        finally:
            self._lock.release()

    def _actor(self, actor):
        self.core._actor(actor)
        scope = getattr(self.db, '_identity', None)
        identity = scope.get() if scope is not None else None
        token_digest = getattr(identity, 'token_digest', None)
        if token_digest:
            from ollama_chat_app.data.database import timestamp_to_db
            with self.db.connect() as conn:
                valid = conn.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? '
                    'AND user_id=? AND revoked_at IS NULL AND expires_at>?',
                    (token_digest, actor, timestamp_to_db(datetime.now(timezone.utc)))).fetchone()
            if valid is None:
                raise StalePersonalSources()

    def lock_owner(self, connection, actor):
        self._actor(actor)
        if getattr(self.db, 'dialect', None) == 'postgresql':
            key = int.from_bytes(hashlib.sha256(('fawnsee:mm11842:' + str(actor)).encode()).digest()[:8], 'big', signed=True)
            connection.execute('SELECT pg_catalog.pg_advisory_xact_lock(CAST(? AS BIGINT))', (key,))

    def invalidate(self, actor, domains=None, connection=None):
        self._actor(actor)
        if domains is not None:
            domains = set(domains)
            if any(d not in DOMAINS for d in domains):
                raise ValueError('invalid_memory_domain')
        if not self.store.available():
            return
        with (nullcontext(connection) if connection is not None else self.db.transaction()) as conn:
            self.lock_owner(conn, actor)
            self.store.invalidate(actor, domains, conn)

    def _provider(self):
        if self.client is None:
            from .qwen_multimodal11842 import QwenMultimodalClient
            self.client = QwenMultimodalClient.from_env()
        model = getattr(self.client, 'model_revision', None)
        if type(model) is not str or re.fullmatch('[a-f0-9]{40}', model) is None:
            raise MultimodalUnavailable('invalid_semantic_model_contract')
        return self.client, model

    def invalidate_sources(self, actor, source_refs, connection=None):
        self._actor(actor)
        if type(source_refs) not in (list, tuple, set) or len(source_refs) > 128:
            raise ValueError('invalid_memory_references')
        refs = sorted(set(source_refs))
        for ref in refs:
            parsed_ref(ref)
        if not self.store.available():
            return
        with (nullcontext(connection) if connection is not None else self.db.transaction()) as conn:
            self.lock_owner(conn, actor)
            self.store.invalidate_sources(actor, refs, conn)

    def invalidate_target_mutation(self, actor, service, method, arguments, connection):
        """Narrow normal source changes before the owning transaction writes.

        Returning True means this source scope has been handled. Consent,
        forgetting and unrelated legacy domains keep their existing policy.
        No attempt epoch or paid retry budget is changed here.
        """
        arguments=arguments or {}
        payload=arguments.get('payload',{})
        if type(payload)is not dict:payload={}
        refs=[];life_ids=[];conversations=None;delete_chat=False;file_id=None
        if service=='preferences'and method in {'update','update_preferences'}:
            from ollama_chat_app.services.preferences import DEFAULT_PREFERENCES,PreferencesService
            from ollama_chat_app.data.database import timestamp_to_db
            patch=arguments.get('patch',{})
            # Use the owning validator before recording an authorization change.
            # An invalid/CAS-failed/replayed save must not advance a generation.
            patch=PreferencesService.validate(patch)
            consent={'mobile_ai_personal_enabled','mobile_ai_library_enabled','ai_context_consent',
                     'ai_library_enabled','ai_context_consent_decided'}
            if not consent.intersection(patch):return True
            self._actor(actor)
            self.lock_owner(connection,actor)
            row=connection.execute('SELECT preferences_json FROM user_preferences WHERE user_id=?',(actor,)).fetchone()
            saved=parsed_json(row['preferences_json'],{})if row else{}
            changed=any(patch[key]!=saved.get(key,DEFAULT_PREFERENCES[key])for key in consent.intersection(patch))
            if not changed:return True
            # This is the real authority revision for a changed global consent,
            # not a cache-loss epoch. Existing per-domain permissions/cutoffs
            # remain untouched, and every old attempt receipt remains retained.
            at=timestamp_to_db(self.core.clock())
            for domain in DOMAINS:
                connection.execute('INSERT INTO member_memory_controls(user_id,domain,enabled_for_ai,collect_events,'
                    'forget_before,revision,updated_at)VALUES(?,?,TRUE,TRUE,NULL,1,?) '
                    'ON CONFLICT(user_id,domain)DO UPDATE SET revision=member_memory_controls.revision+1,'
                    'updated_at=excluded.updated_at',(actor,domain,at))
            return False  # Original broad privacy cleanup still runs atomically.
        if service=='health'and method in {'delete_life_record','delete_life_records'}:
            life_ids=([arguments.get('record_id')]if method=='delete_life_record'else arguments.get('record_ids',[]))
        elif service=='expansion'and method in {'update_record','delete_record'}:
            life_ids=[payload.get('record_id')]
        elif service=='expansion'and method in {'update_observation','delete_observation'}:
            identifier=payload.get('observation_id')
            if type(identifier)is int and identifier>0:refs=['expansion_observations:'+str(identifier)]
        elif service=='expansion'and method in {'update_meal','delete_meal'}:
            identifier=payload.get('meal_id')
            if type(identifier)is int and identifier>0:
                row=connection.execute('SELECT id,life_record_id FROM expansion_meals WHERE user_id=? AND id=?',
                                       (actor,identifier)).fetchone()
                if row is not None:life_ids=[row['life_record_id']];refs=['expansion_meals:'+str(row['id'])]
        elif service=='chat'and method in {'delete_conversations','rename_conversation'}:
            delete_chat=method=='delete_conversations'
            conversations=arguments.get('conversation_ids',[])if delete_chat else[arguments.get('conversation_id')]
            if not delete_chat and len(conversations)==1 and type(conversations[0])is int:
                row=connection.execute('SELECT title FROM conversations WHERE user_id=? AND id=?',
                                       (actor,conversations[0])).fetchone()
                # The service trims titles. A same-title rename is not a source
                # change and must not exhaust an unchanged chunk's cache budget.
                title=arguments.get('title')
                if row is not None and type(title)is str and row['title']==title.strip():return True
        elif service=='files'and method=='delete_file':
            file_id=arguments.get('file_id')
        else:
            return False
        # Invalid arguments are still rejected by the owning service. Do not
        # widen their deletion scope or alter its public validation contract.
        life_ids=[x for x in life_ids if type(x)is int and x>0]if type(life_ids)is list else[]
        if len(life_ids)>100:return True
        self._actor(actor)
        if not self.store.available():return True
        self.lock_owner(connection,actor)
        if life_ids:
            refs.extend('life_records:'+str(x)for x in life_ids)
            meals=connection.execute('SELECT id FROM expansion_meals WHERE user_id=? AND life_record_id IN('
                +','.join('?'for _ in life_ids)+')',(actor,*life_ids)).fetchall()
            refs.extend('expansion_meals:'+str(row['id'])for row in meals)
        if refs:
            # Root page deletion is done by the reviewed store, not by a
            # bounded/recent ledger snapshot that could miss older page rows.
            self.store.invalidate_sources(actor,sorted(set(refs)),connection)
        if conversations is not None:
            if (type(conversations)is not list or not 1<=len(conversations)<=100
                    or any(type(x)is not int or x<=0 for x in conversations)):return True
            self.store.invalidate_conversations(actor,conversations,connection,attachments=delete_chat)
            if delete_chat:
                # The real FK also cascades. Explicit owner scope keeps the
                # same guarantee for the precommit invalidation boundary.
                connection.execute('DELETE FROM member_conversation_memory11844 WHERE user_id=? AND conversation_id IN('
                    +','.join('?'for _ in conversations)+')',(actor,*conversations))
        if type(file_id)is int and file_id>0:
            # Removing a picture does not change the parent's text. Retain
            # those unchanged text chunks and unrelated images/conversations.
            connection.execute('DELETE FROM '+TABLE+' WHERE user_id=? AND image_source_ref=?',
                               (actor,'user_files:'+str(file_id)))
        return True

    def _state(self, actor, controls=None, domains=None):
        self._actor(actor)
        config = self.settings_reader(actor)
        if config.get('enabled') is not True:
            raise StalePersonalSources()
        rows = {row['domain']: dict(row) for row in config.get('controls', [])}
        selected = set(DOMAINS if domains is None else domains) - {'profile', 'preferences'}
        if any(d not in DOMAINS for d in selected):
            raise StalePersonalSources()
        selected = {d for d in selected if d in rows and rows[d].get('enabled_for_ai') is True
                    and rows[d].get('collect_events') is True and not (controls and controls.get(d) is False)}
        return {'revision': config['revision'], 'domains': selected, 'controls': rows}

    def _same_state(self, actor, state):
        current = self._state(actor, domains=state['domains'])
        if current['revision'] != state['revision'] or current['domains'] != state['domains']:
            raise StalePersonalSources()

    def _resolve(self, actor, documents, state):
        self._same_state(actor, state)
        from .vector_coverage11847 import is_target, documents as full_documents, identity
        full = [item for item in documents if is_target(item.get('ref')) and item.get('modality') == 'text']
        refs = [{'ref': item['ref']} for item in documents if item not in full]
        resolved = self.core.resolve_references(actor, refs)
        # Assistant originals have a separate, role-bound conversation path.
        # The core fact/preference resolver remains strictly user-only.
        if full:
            expected = {identity(item): item for item in full_documents(self.db, actor,
                        [item['ref'] for item in full], state, document_hash,
                        selected={(item['ref'], item['chunk_no']) for item in full})}
            for item in full:
                current = expected.get(identity(item))
                if current is None or ('content' in item and item['content'] != current['content']):
                    raise StalePersonalSources()
                if (item.get('domain') != current['domain']
                        or timestamp(item.get('occurred_at')) != timestamp(current['occurred_at'])):
                    raise StalePersonalSources()
                if item.get('kind') not in {None, 'raw_record', current['kind']}:
                    raise StalePersonalSources()
                # A supplied proof may never relabel an AI reply as a user fact.
                if 'content' not in item and item.get('kind') != current['kind']:
                    raise StalePersonalSources()
                resolved[item['ref']] = current
        for item in documents:
            parsed_ref(item['ref'])
            current = resolved.get(item['ref'])
            at = timestamp(item.get('occurred_at'))
            cutoff = timestamp(state['controls'].get(item['domain'], {}).get('forget_before'))
            if (not current or item['domain'] not in state['domains'] or current['domain'] != item['domain']
                    or current['source_hash'] != item['source_hash'] or not at or (cutoff and at <= cutoff)
                    or current.get('kind') == 'model_inferred'):
                raise StalePersonalSources()
            if item['modality'] == 'image':
                self._image_link(actor, item)
                self._image(actor, item)
        return resolved

    def fulltext_documents(self, actor, refs, state=None, *, selected=None):
        from .vector_coverage11847 import documents
        self._actor(actor)
        return documents(self.db, actor, refs, state or self._state(actor), document_hash, selected=selected)

    def chunk_ledger(self, actor, model, documents):
        refs = list(dict.fromkeys(item['ref'] for item in documents))
        return self.store.ledger_for(actor, model, refs)

    def _chunk_budget(self):
        budget = getattr(self, 'chunk_budget', None)
        if budget is None:
            # Standalone owners (including maintenance/integration callers)
            # receive the same durable claim policy as the composed app.
            from types import SimpleNamespace
            from .conversation_progress11846 import ConversationProgress11846
            budget = ConversationProgress11846(SimpleNamespace(db=self.db,
                clock=getattr(self.core, 'clock', lambda: datetime.now(timezone.utc))), self)
        return budget

    def index_original_chunks(self, actor, chunks, *, expected=None):
        """One bounded exact chunk batch; caller owns durable attempt claims."""
        from .vector_coverage11847 import identity, ledger_identity
        self._actor(actor)
        if (type(chunks) is not list or len(chunks) > MAX_NEW_TEXT or any(type(x) is not dict
                or set(x) != {'ref', 'chunk_no'} or type(x['chunk_no']) is not int
                or not 0 <= x['chunk_no'] <= 31 for x in chunks)):
            raise ValueError('invalid_memory_chunks')
        state = self._state(actor)
        available = {(d['ref'], d['chunk_no']): d for d in self.fulltext_documents(actor,
                     [x['ref'] for x in chunks], state,
                     selected={(x['ref'], x['chunk_no']) for x in chunks})}
        selected = [available.get((x['ref'], x['chunk_no'])) for x in chunks]
        if any(x is None for x in selected) or len({identity(x) for x in selected}) != len(selected):
            raise StalePersonalSources()
        if expected is not None and {identity(x) for x in selected} != set(expected):
            raise StalePersonalSources()
        if sum(len(x['content'].encode('utf-8')) for x in selected) > 24000:
            raise ValueError('memory_chunk_budget_exceeded')
        client, model = self._provider()
        existing = {ledger_identity(x) for x in self.chunk_ledger(actor, model, selected) if x['modality'] == 'text'}
        fresh = [x for x in selected if identity(x) not in existing]
        if not fresh:
            return {'state': 'already_indexed', 'indexed_originals': 0, 'already_indexed': len(selected)}
        deadline = self.monotonic() + 40
        with self._locked(deadline):
            guard = lambda: self._guard_sources(actor, fresh, state, deadline)
            guard()
            values = client.embed_texts([x['content'] for x in fresh], pre_send=guard, deadline=deadline)
            if len(values) != len(fresh):
                raise MultimodalUnavailable('invalid_semantic_batch')
            for vector in values:
                vector_literal(vector)
            with self.db.transaction() as conn:
                self.lock_owner(conn, actor)
                guard()
                self.store.upsert(actor, model, fresh, values, conn)
        return {'state': 'ready', 'indexed_originals': len(fresh), 'already_indexed': len(selected)-len(fresh)}

    def _guard_sources(self, actor, documents, state, deadline):
        self._budget(deadline)
        self._resolve(actor, documents, state)
        self._budget(deadline)

    def _guard_state(self, actor, state, deadline):
        self._budget(deadline)
        self._same_state(actor, state)
        self._budget(deadline)

    def _original_message_evidence(self, actor, refs, state):
        """Build the same canonical evidence as collect(), never summary text."""
        identifiers = [int(parsed_ref(ref, {'messages'})[1]) for ref in refs]
        if not identifiers:
            return []
        with self.db.connect() as conn:
            rows = conn.execute('SELECT m.id,m.conversation_id,m.sequence_no,m.role,m.content,'
                'm.status,m.created_at,m.updated_at,c.title FROM messages m '
                'JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? '
                "AND m.role='user' AND m.status='complete' "
                "AND (m.provider IS NULL OR m.provider<>'fawnsee-welcome') "
                'AND length(m.content)<=24000 AND m.id IN ('
                + ','.join('?' for _ in identifiers) + ')', (actor, *identifiers)).fetchall()
        available = {'messages:' + str(row['id']): dict(row) for row in rows}
        canonical_sources = self.core.resolve_references(actor,
            [{'ref': ref} for ref in refs if ref in available])
        evidence = []
        for ref in refs:
            row, canonical_source = available.get(ref), canonical_sources.get(ref)
            if row is None or canonical_source is None:
                continue
            source = _evidence('ai_records', 'messages', row,
                data={key: row[key] for key in ('conversation_id', 'sequence_no', 'status', 'title')},
                kind='raw_record')
            if source is None or source['source_hash'] != canonical_source['source_hash']:
                raise StalePersonalSources()
            source['speaker'] = 'user'
            evidence.append(source)
        return evidence

    def index_original_messages(self, actor, refs, *, expected=None):
        """Compatibility wrapper; selected roots now retain all original chunks.

        The old dict contract is accepted only when it describes exactly one
        planned chunk per root. Internal workers use the unambiguous chunk API.
        """
        from .vector_coverage11847 import identity
        self._actor(actor)
        if (type(refs) is not list or len(refs)>40 or any(type(x) is not dict
                or set(x)!={'ref'} for x in refs)):
            raise ValueError('invalid_memory_references')
        for item in refs:
            parsed_ref(item['ref'], {'messages'})
        state = self._state(actor, domains=['ai_records'])
        if not state['domains']:
            return {'state':'disabled','indexed_originals':0,'deferred_items':0}
        documents = self.fulltext_documents(actor, [x['ref'] for x in refs], state)
        if expected is not None:
            if (type(expected) is not dict or len(documents)!=len(refs)
                    or {d['ref']:(d['source_hash'],d['content_hash']) for d in documents}!=expected):
                raise StalePersonalSources()
        return self._chunk_budget().warm_selected(actor, refs)

    def _image_link(self, actor, item):
        parent, key = parsed_ref(item['ref'])
        table, file_key = parsed_ref(item.get('image_source_ref'), {'user_files', 'chat_attachments'})
        if parent == 'chat_attachments':
            if table != 'chat_attachments' or file_key != key:
                raise StalePersonalSources()
            return
        if table != 'user_files' or parent not in {'life_records', 'expansion_observations', 'expansion_meals', 'medical_reports'}:
            raise StalePersonalSources()
        with self.db.connect() as conn:
            if parent in {'life_records', 'expansion_observations'}:
                column = 'details_json' if parent == 'life_records' else 'payload_json'
                row = conn.execute('SELECT ' + column + ' AS details FROM ' + parent +
                    ' WHERE user_id=? AND id=?', (actor, int(key))).fetchone()
                details = parsed_json(row['details'], {}) if row else {}
                ids = details.get('photo_file_ids', []) if isinstance(details, dict) else []
                linked = (type(ids) is list and len(ids) <= 4 and
                    all(type(v) is int and 0 < v < 2**53 for v in ids) and int(file_key) in ids)
            elif parent == 'expansion_meals':
                row = conn.execute('SELECT m.payload_json,l.details_json AS parent_details FROM expansion_meals m '
                    'JOIN life_records l ON l.id=m.life_record_id AND l.user_id=m.user_id '
                    'WHERE m.user_id=? AND m.id=?', (actor, int(key))).fetchone()
                data = parsed_json(row['payload_json'], {}) if row else {}
                parent_data = parsed_json(row['parent_details'], {}) if row else {}
                linked = (row is not None and isinstance(data, dict) and isinstance(parent_data, dict)
                    and 'photo_file_ids' not in parent_data and type(data.get('photo_file_id')) is int
                    and data['photo_file_id'] == int(file_key))
            else:
                row = conn.execute("SELECT file_id FROM medical_reports WHERE user_id=? AND id=? AND status='confirmed'",
                    (actor, int(key))).fetchone()
                linked = row is not None and type(row['file_id']) is int and row['file_id'] == int(file_key)
            if not linked:
                raise StalePersonalSources()

    def _image(self, actor, item):
        self._actor(actor)
        table, key = parsed_ref(item.get('image_source_ref'), {'user_files', 'chat_attachments'})
        with self.db.connect() as conn:
            if table == 'user_files':
                row = conn.execute('SELECT f.media_type,f.sha256,f.size_bytes,c.content,c.mime_type,'
                    'c.sha256 AS content_sha256 FROM user_files f JOIN user_file_contents c ON c.file_id=f.id '
                    'WHERE f.user_id=? AND f.id=?', (actor, int(key))).fetchone()
                if row is None:
                    raise StalePersonalSources()
                if row['size_bytes'] > MAX_IMAGE_BYTES or row['sha256'] != row['content_sha256'] or row['media_type'] != row['mime_type']:
                    raise StalePersonalSources()
            else:
                row = conn.execute('SELECT a.media_type,a.sha256,a.content FROM chat_attachments a '
                    'JOIN messages m ON m.id=a.message_id JOIN conversations c ON c.id=m.conversation_id '
                    "WHERE c.user_id=? AND a.id=? AND m.role='user'", (actor, int(key))).fetchone()
                if row is None:
                    raise StalePersonalSources()
            media = row['media_type']
            raw = bytes(row['content'])
            digest = hashlib.sha256(raw).hexdigest()
            if (media not in PICTURE_TYPES or not 0 < len(raw) <= MAX_IMAGE_BYTES or
                    digest != row['sha256'] or item.get('image_sha256') not in (None, digest)):
                raise StalePersonalSources()
            if table == 'user_files' and row['size_bytes'] != len(raw):
                raise StalePersonalSources()
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format != PICTURE_TYPES[media] or max(picture.size) > 2048:
                    raise ValueError()
                picture.verify()
            from .platform_uploads import validate_image
            validate_image(raw, media)
        except Exception:
            raise StalePersonalSources() from None
        return raw, media, digest

    def _extra_picture_sources(self, actor, snapshot, state):
        """Formal legacy parents only; orphan files and report drafts stay out."""
        if 'life_records' not in state['domains']:
            return []
        scope = snapshot.get('selected_range', {})
        start, end = timestamp(scope.get('start')), timestamp(scope.get('end'))
        if not start or not end:
            raise StalePersonalSources()
        low, high = start.astimezone(timezone.utc).isoformat(), (end + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        result = []
        with self.db.connect() as conn:
            meals = conn.execute('SELECT m.id,m.user_id,m.life_record_id,m.occurred_at,m.meal_type,m.payload_json,'
                'm.created_at,m.updated_at,l.details_json AS parent_details_json FROM expansion_meals m JOIN life_records l '
                'ON l.id=m.life_record_id AND l.user_id=m.user_id WHERE m.user_id=? AND m.occurred_at>=? '
                'AND m.occurred_at<? ORDER BY m.occurred_at DESC,m.id DESC LIMIT 128', (actor, low, high)).fetchall()
            for raw in meals:
                row = dict(raw)
                parent_details = parsed_json(row.pop('parent_details_json'), {})
                if 'photo_file_ids' in parent_details:
                    continue  # An explicit modern photo list, including [], wins.
                data = parsed_json(row['payload_json'], {})
                file_id = data.get('photo_file_id') if isinstance(data, dict) else None
                if type(file_id) is not int or not 0 < file_id < 2**53:
                    continue
                note = data.get('notes', '') if type(data.get('notes', '')) is str else ''
                item = _evidence('life_records', 'expansion_meals', row, when='occurred_at',
                    data={'category': 'diet', 'meal_type': row['meal_type'], 'details': {'photo_file_ids': [file_id]}},
                    statement=note or '用户正式保存的餐食图片；图内内容尚未自动确认')
                if item: result.append(item)
            reports = conn.execute('SELECT id,user_id,file_id,report_type,report_date,institution,summary,status,created_at,updated_at '
                "FROM medical_reports WHERE user_id=? AND status='confirmed' AND ((report_date IS NOT NULL "
                'AND report_date>=? AND report_date<=?) OR (report_date IS NULL AND created_at>=? AND created_at<?)) '
                'ORDER BY created_at DESC,id DESC LIMIT 128',
                (actor, scope['start'], scope['end'], low, high)).fetchall()
            for raw in reports:
                row = dict(raw); file_id = row['file_id']
                if type(file_id) is not int or not 0 < file_id < 2**53:
                    continue
                item = _evidence('life_records', 'medical_reports', row,
                    when='report_date' if row['report_date'] is not None else 'created_at',
                    data={'report_type': row['report_type'], 'report_date': str(row['report_date']) if row['report_date'] else None,
                        'institution': row['institution'], 'details': {'photo_file_ids': [file_id]}, 'confirmed_report': True},
                    statement=row['summary'] or '用户已确认报告的原始图片；图内数值须查看原图或已有文字，不由嵌入模型识别')
                if item: result.append(item)
        return result

    def _documents(self, actor, snapshot, state, ledger):
        previous_images = {(r['source_ref'], r['source_hash'], r['chunk_no'], r.get('image_source_ref')): r
                           for r in ledger if r['modality'] == 'image'}
        results = []
        sources = [*snapshot.get('evidence', []), *self._extra_picture_sources(actor, snapshot, state)]
        from .vector_coverage11847 import is_target
        # Bound materialization independently of the core's 500-row display
        # page. Background keysets cover every remaining source and chunk.
        roots = list(dict.fromkeys(s['ref'] for s in sources if is_target(s.get('ref'))))
        for root in roots:
            results.extend(self.fulltext_documents(actor, [root], state))
            if sum(len(d['content']) for d in results) >= 2_000_000:
                break
        for source in sources:
            if source.get('source_table') == 'messages':
                continue  # Full text and role came from the owning row above.
            if (source.get('domain') not in state['domains'] or source.get('source_table') not in SOURCE_TABLES
                    or source.get('not_user_fact') or source.get('speaker') == 'assistant'
                    or source.get('kind') == 'model_inferred'):
                continue
            parsed_ref(source.get('ref'))
            base = {key: source[key] for key in ('ref', 'domain', 'source_hash', 'occurred_at', 'kind')}
            at = timestamp(base['occurred_at'])
            cutoff = timestamp(state['controls'][base['domain']].get('forget_before'))
            if not at or cutoff and at <= cutoff:
                continue
            description = source.get('statement') or ''
            data = _safe_data(source.get('data', {}))
            content = (description + ('\n' + canonical(data) if data else '')).strip()[:MAX_CONTENT]
            if content and not is_target(source.get('ref')):
                text = {**base, 'content': content, 'modality': 'text', 'chunk_no': 0,
                        'image_source_ref': None, 'image_sha256': None}
                text['content_hash'] = document_hash(text)
                results.append(text)
            photos = []
            if source['source_table'] in {'life_records', 'expansion_observations', 'expansion_meals', 'medical_reports'}:
                details = source.get('data', {}).get('details', {})
                ids = details.get('photo_file_ids', []) if isinstance(details, dict) else []
                if type(ids) is list and len(ids) <= 4:
                    photos = ['user_files:' + str(v) for v in ids if type(v) is int and 0 < v < 2**53]
            elif source['source_table'] == 'chat_attachments' and source.get('data', {}).get('media_type') in PICTURE_TYPES:
                photos = [source['ref']]
            for n, ref in enumerate(photos):
                item = {**base, 'content': content, 'modality': 'image', 'chunk_no': n,
                        'image_source_ref': ref, 'image_sha256': None, 'content_hash': None}
                previous = previous_images.get((base['ref'], base['source_hash'], n, ref))
                if previous:
                    item['image_sha256'] = previous.get('image_sha256')
                    item['content_hash'] = document_hash(item)
                results.append(item)
        return results

    @staticmethod
    def _provider_image(raw):
        """A bounded derivative; the original stays private and authoritative."""
        from PIL import Image, ImageOps
        out = io.BytesIO()
        with Image.open(io.BytesIO(raw)) as picture:
            picture = ImageOps.exif_transpose(picture)
            picture.thumbnail((1024, 1024))
            if picture.mode in {'RGBA', 'LA'} or 'transparency' in picture.info:
                rgba = picture.convert('RGBA')
                background = Image.new('RGB', rgba.size, 'white')
                background.paste(rgba, mask=rgba.getchannel('A'))
                picture = background
            else:
                picture = picture.convert('RGB')
            picture.save(out, format='JPEG', quality=85)
        result = out.getvalue()
        if not 0 < len(result) <= MAX_IMAGE_BYTES:
            raise MultimodalUnavailable('personal_image_budget')
        return result, 'image/jpeg'

    def validate_proof(self, actor, proof):
        self._actor(actor)
        if (type(proof) is not dict or set(proof) != {'settings_revision', 'model_revision', 'references'}
                or type(proof['references']) is not list or len(proof['references']) > MAX_REFERENCES):
            raise StalePersonalSources()
        state = self._state(actor)
        if state['revision'] != proof['settings_revision']:
            raise StalePersonalSources()
        _, model = self._provider()
        if proof['model_revision'] != model:
            raise StalePersonalSources()
        for item in proof['references']:
            if type(item) is not dict or set(item) != set(PROOF_FIELDS):
                raise StalePersonalSources()
        self._resolve(actor, proof['references'], state)
        return True

    def retrieve(self, actor, question, *, snapshot=None, controls=None, domains=None):
        deadline = self.monotonic() + 40.0
        self._actor(actor)
        if type(question) is not str or not 1 <= len(question) <= 4000:
            raise ValueError('invalid_personal_query')
        if not self.store.available():
            raise MultimodalUnavailable('semantic_store_unavailable')
        state = self._state(actor, controls, domains)
        if not state['domains']:
            return {'references': [], 'proof': None, 'coverage': {'state': 'disabled'}}
        try:
            client, model = self._provider()
        except Exception:
            raise MultimodalUnavailable('semantic_provider_unavailable') from None
        if snapshot is None:
            start, end, bucket = self.core._retrieval_question_range(question, 'all')
            other_domains = state['domains'] - {'ai_records'}
            first, last, low, high, ranges = self.core._period(start, end, bucket)
            snapshot = (self.core.collect(actor, start, end, bucket=bucket,
                question=question, domains=sorted(other_domains)) if other_domains else
                {'member_user_id': actor, 'evidence': [], 'coverage': {},
                 'selected_range': {'start': first.isoformat(), 'end': last.isoformat(), 'bucket': bucket}})
            if 'ai_records' in state['domains']:
                # Chat content can legally be two million characters. Fetch
                # only IDs here; never materialize the core's 500 full turns.
                with self.db.connect() as conn:
                    rows = conn.execute('SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id '
                        "WHERE c.user_id=? AND m.status='complete' AND m.role IN ('user','assistant') "
                        'AND m.created_at>=? AND m.created_at<? ORDER BY m.created_at DESC,m.id DESC LIMIT 501',
                        (actor, low, high)).fetchall()
                snapshot['evidence'].extend({'ref': 'messages:' + str(r['id']),
                    'domain': 'ai_records', 'source_table': 'messages'} for r in rows[:500])
                snapshot['coverage']['ai_records'] = {'truncated': len(rows)>500}
                # Preserve the existing attachment/image path without asking
                # collect() to load full message bodies for their parent IDs.
                with self.db.connect() as conn:
                    attachments = conn.execute('SELECT a.id,a.message_id,a.original_name,a.media_type,a.sha256,'
                        'a.extraction_method,substr(a.extracted_text,1,1600) AS extracted_excerpt,a.created_at '
                        'FROM chat_attachments a JOIN messages m ON m.id=a.message_id '
                        'JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? '
                        'AND a.created_at>=? AND a.created_at<? ORDER BY a.created_at DESC,a.id DESC LIMIT 501',
                        (actor, low, high)).fetchall()
                snapshot['coverage']['ai_records']['truncated'] |= len(attachments)>500
                for raw in attachments[:500]:
                    row = dict(raw)
                    item = _evidence('ai_records', 'chat_attachments', row,
                        data={k: row[k] for k in ('message_id','original_name','media_type','sha256','extraction_method')},
                        statement=row['extracted_excerpt'] or '本人的对话附件引用；未读取二进制或自动确认内容')
                    if item:
                        snapshot['evidence'].append(item)
        if snapshot.get('member_user_id') != actor:
            raise StalePersonalSources()
        with self._locked(deadline):
            ledger = self.store.ledger(actor, model)
            documents = self._documents(actor, snapshot, state, ledger)
            self._budget(deadline)
            if not documents and not ledger:
                return {'references': [], 'proof': {'settings_revision': state['revision'],
                    'model_revision': model, 'references': []}, 'coverage': {'state': 'no_authorized_originals',
                    'new_text_items': 0, 'new_image_items': 0, 'dimensions': 512, 'model_revision': model,
                    'source_page_truncated': any(v.get('truncated') for v in snapshot.get('coverage', {}).values()),
                    'complete_history_search_claim_allowed': False, 'complete_archive_was_not_sent_to_provider': True}}
            existing = {(r['source_ref'], r['modality'], r['chunk_no'], r['source_hash'], r['content_hash'])
                        for r in ledger}
            fresh = [d for d in documents if (d['ref'], d['modality'], d['chunk_no'], d['source_hash'], d['content_hash']) not in existing]
            # Cached source keys are skipped, so repeated authorized requests
            # advance beyond the newest page instead of always sending row one.
            texts, text_bytes = [], 0
            for item in [d for d in fresh if d['modality'] == 'text']:
                size = len(item['content'].encode('utf-8'))
                if text_bytes + size > 24000:
                    continue
                texts.append(item); text_bytes += size
                if len(texts) >= MAX_NEW_TEXT:
                    break
            # Lazy retrieval and background backfill spend from the same
            # durable per-chunk budget. Query/rerank remain foreground work.
            from .vector_coverage11847 import is_target
            claims = []
            budget = self._chunk_budget()
            allowed = []
            for item in texts:
                if not is_target(item['ref']):
                    allowed.append(item)
                elif budget is not None:
                    claim = budget._claim(actor, state, model, item)
                    if claim is not None:
                        allowed.append(item)
                        claims.append(claim)
            texts = allowed
            images, image_bytes = [], {}
            invalid_images = 0
            for item in [d for d in fresh if d['modality'] == 'image'][:16]:
                try:
                    raw, media, digest = self._image(actor, item)
                except StalePersonalSources:
                    invalid_images += 1
                    continue
                item['image_sha256'] = digest
                item['content_hash'] = document_hash(item)
                images.append(item)
                image_bytes[(item['ref'], item['chunk_no'])] = (raw, media)
                self._budget(deadline)
                if len(images) >= MAX_NEW_IMAGES:
                    break
            new, vectors = [], []
            try:
                if texts:
                    guard = lambda: self._guard_sources(actor, texts, state, deadline)
                    guard()
                    values = client.embed_texts([d['content'] for d in texts], pre_send=guard, deadline=deadline)
                    if len(values) != len(texts):
                        raise MultimodalUnavailable('invalid_semantic_batch')
                    new.extend(texts); vectors.extend(values)
                for item in images:
                    raw, media = image_bytes[(item['ref'], item['chunk_no'])]
                    guard = lambda item=item: self._guard_sources(actor, [item], state, deadline)
                    guard()
                    provider_raw, provider_media = self._provider_image(raw)
                    values = client.embed_image(provider_raw, mime_type=provider_media,
                        text=item['content'] or None, pre_send=guard, deadline=deadline)
                    new.append(item); vectors.append(values)
                for values in vectors:
                    vector_literal(values)
                self._budget(deadline)
                if new:
                    with self.db.transaction() as conn:
                        self.lock_owner(conn, actor)
                        self._resolve(actor, new, state)
                        self.store.upsert(actor, model, new, vectors, conn)
                for claim in claims:
                    budget._finish(actor, claim, 'complete')
                claims = []
                guard = lambda: self._guard_state(actor, state, deadline)
                guard()
                query = client.embed_text(question, query=True, pre_send=guard, deadline=deadline)
                vector_literal(query)
                self._same_state(actor, state)
                selected = snapshot.get('selected_range', {})
                start = timestamp(selected.get('start'))
                end = timestamp(selected.get('end'))
                if not start or not end:
                    raise StalePersonalSources()
                candidates = self.store.nearest(actor, model, query, state,
                    start.astimezone(timezone.utc).isoformat(), (end + timedelta(days=1)).astimezone(timezone.utc).isoformat())
                verified, stale, image_candidates = [], [], 0
                for item in candidates:
                    item['occurred_at'] = timestamp(item['occurred_at']).astimezone(timezone.utc).isoformat()
                    item['kind'] = 'raw_record'
                    try:
                        resolved = self._resolve(actor, [item], state)
                        item['kind'] = resolved[item['ref']]['kind']
                        if document_hash(item) != item['content_hash']:
                            raise StalePersonalSources()
                        if item['modality'] == 'image':
                            if image_candidates >= 4:
                                continue
                            image_candidates += 1
                        verified.append(item)
                    except StalePersonalSources:
                        stale.append({**item, 'model_revision': model})
                if stale:
                    with self.db.transaction() as conn:
                        self.lock_owner(conn, actor)
                        self.store.remove(actor, stale, conn)
                rank_inputs, rank_items = [], []
                rank_text_bytes = rank_image_bytes = 0
                query_bytes = len(question.encode('utf-8'))
                for item in verified:
                    text_cost = query_bytes + (len(item['content'].encode('utf-8')) if item['modality'] == 'text' else 0)
                    if rank_text_bytes + text_cost > 60000:
                        continue
                    if item['modality'] == 'image':
                        raw, media, _ = self._image(actor, item)
                        raw, media = self._provider_image(raw)
                        if rank_image_bytes + len(raw) > 6 * 1024 * 1024:
                            continue
                        rank_image_bytes += len(raw)
                        rank_inputs.append({'image': raw, 'mime_type': media})
                    else:
                        rank_inputs.append(item['content'])
                    rank_text_bytes += text_cost
                    rank_items.append(item)
                verified = rank_items
                ranked = []
                if verified:
                    guard = lambda: self._guard_sources(actor, verified, state, deadline)
                    guard()
                    ranked = client.rerank(question, rank_inputs, top_n=min(MAX_REFERENCES, len(verified)),
                        pre_send=guard, deadline=deadline)
                    self._resolve(actor, verified, state)
            except StalePersonalSources:
                for claim in claims:
                    budget._finish(actor, claim, 'failed')
                raise
            except MultimodalUnavailable:
                for claim in claims:
                    budget._finish(actor, claim, 'failed')
                raise
            except Exception:
                for claim in claims:
                    budget._finish(actor, claim, 'failed')
                raise MultimodalUnavailable('personal_semantic_unavailable') from None
            refs, used = [], set()
            for rank in ranked:
                index, score = rank.get('index'), rank.get('relevance_score')
                if (type(index) is not int or not 0 <= index < len(verified) or index in used or
                        type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
                    raise MultimodalUnavailable('invalid_semantic_ranking')
                used.add(index)
                item = verified[index]
                reference = {**{k: item.get(k) for k in PROOF_FIELDS}, 'text': item['content'],
                    'relevance_score': float(score), 'cosine_distance': float(item['cosine_distance']),
                    'model_revision': model, 'source': 'authorized_original_personal_record',
                    'excerpted': True, 'image_is_not_ocr': item['modality'] == 'image',
                    'user_report_not_medical_fact': item['kind'] != 'assistant_context',
                    'speaker': 'assistant' if item['kind'] == 'assistant_context' else 'user',
                    'not_user_fact': item['kind'] == 'assistant_context',
                    'assistant_output_not_user_fact': item['kind'] == 'assistant_context'}
                prior = next((n for n, ref in enumerate(refs) if ref['ref'] == item['ref']), None)
                if prior is not None:
                    if reference['modality'] == 'image' and refs[prior]['modality'] != 'image':
                        refs[prior] = reference
                    continue
                refs.append(reference)
                if len(refs) >= MAX_REFERENCES:
                    break
            self._resolve(actor, refs, state)
            proof = {'settings_revision': state['revision'], 'model_revision': model,
                'references': [{k: r[k] for k in PROOF_FIELDS} for r in refs]}
            return {'references': refs, 'proof': proof, 'coverage': {
                'state': 'ready', 'model_revision': model, 'dimensions': 512,
                'new_text_items': len(texts), 'new_image_items': len(images),
                'invalid_or_missing_images_omitted': invalid_images,
                'new_items_remaining_in_loaded_scope': max(0, len(fresh) - len(new)),
                'loaded_original_documents': len(documents), 'reranked_candidates': len(verified),
                'returned_references': len(refs), 'source_page_truncated': any(
                    v.get('truncated') for v in snapshot.get('coverage', {}).values()),
                'date_scope': selected, 'complete_archive_was_not_sent_to_provider': True,
                'complete_history_search_claim_allowed': False, 'existing_statistics_unchanged': True}}

    def prepare_context(self, actor, question, *, image_budget=0):
        if type(image_budget) is not int or not 0 <= image_budget <= 2:
            raise ValueError('invalid_personal_image_budget')
        result = self.retrieve(actor, question)
        if result.get('proof') is None:
            # A valid profile-only permission choice has no general sources.
            # It must not turn an otherwise authorized personal chat into 409.
            if result.get('references') or result.get('coverage', {}).get('state') != 'disabled':
                raise StalePersonalSources()
            return {**result, 'image_parts': []}
        parts, image_hashes = [], set()
        for item in result['references']:
            if item['modality'] != 'image' or len(parts) >= image_budget:
                continue
            self.validate_proof(actor, result['proof'])
            raw, media, _ = self._image(actor, item)
            if item['image_sha256'] in image_hashes:
                continue
            image_hashes.add(item['image_sha256'])
            if media == 'image/webp':
                from PIL import Image
                out = io.BytesIO()
                with Image.open(io.BytesIO(raw)) as picture:
                    if picture.width * picture.height <= 262144:
                        # A small sending copy must retain its decoded pixels
                        # and alpha; RGB/JPEG would expose hidden transparent RGB.
                        picture.convert('RGBA').save(out, format='PNG')
                        media = 'image/png'
                    else:
                        picture.convert('RGB').save(out, format='JPEG', quality=85)
                        media = 'image/jpeg'
                raw = out.getvalue()
                if len(raw) > MAX_IMAGE_BYTES:
                    continue
            parts.append({'type': 'image_url', 'image_url': {
                'url': 'data:' + media + ';base64,' + base64.b64encode(raw).decode('ascii')}})
        self.validate_proof(actor, result['proof'])
        return {**result, 'image_parts': parts}
