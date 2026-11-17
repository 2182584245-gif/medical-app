"""Read-only history coverage and one bounded, source-bound cold index batch.

No runtime DDL, new transcript, autonomous drain loop or model call from status.
The existing owner-only INSERT/SELECT RPC journal holds only attempt hashes.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import traceback
from pathlib import Path
from ollama_chat_app.data.database import timestamp_to_db

from .memory_job_ids11846 import receipt_id
from .vector_coverage11847 import identity, ledger_identity, scan

SUMMARY = 'member_conversation_memory11844'
VECTORS = 'member_multimodal_vectors11842'
PAGE = 128
SCAN_PAGES = 8
STATUS_INDEX_LIMIT = 2048
STATUS_SUMMARY_LIMIT = 128
COOLDOWN_SECONDS = 900
LOGGER = logging.getLogger(__name__)


def diagnostic(actor, stage, *, error=None, **counts):
    """Bounded operational metadata only; never log exception text or sources."""
    try:
        value = {'actor_id': actor, 'stage': stage, **counts}
        if error is not None:
            value['code'] = 'worker_' + stage
            value['error_type'] = type(error).__name__[:80]
            http_status = getattr(error, 'http_status', None)
            if type(http_status) is int and 100 <= http_status <= 599:
                value['http_status'] = http_status
            frames = traceback.extract_tb(error.__traceback__)
            if frames:
                frame = frames[-1]
                value['error_frame'] = {'file': Path(frame.filename).name,
                                        'function': frame.name, 'line': frame.lineno}
        LOGGER.warning('MEMORY11846_DIAGNOSTIC %s', canonical(value))

    except Exception:
        pass  # Diagnostics must never change the worker outcome.


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def hashed(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result.astimezone(timezone.utc)


def source_scope(actor, state):
    clauses = ["c.user_id=?", "m.status='complete'", "m.role IN ('user','assistant')",
               "(m.provider IS NULL OR m.provider<>'fawnsee-welcome')"]
    params = [actor]
    if state['cutoff']:
        clauses.append('m.created_at>?')
        params.append(state['cutoff'])
    # Keep the recent thirty qualifying completed originals for each conversation.
    # Oversized older sources are separately omitted; they cannot block the cursor.
    sql = ('WITH scoped AS (SELECT m.id,m.conversation_id,m.sequence_no,m.role,m.created_at,'
           'length(m.content) AS message_chars,ROW_NUMBER() OVER('
           'PARTITION BY m.conversation_id ORDER BY m.sequence_no DESC,m.id DESC) AS recent_rank '
           'FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE '
           + ' AND '.join(clauses) + ') ')
    return sql, params


def scoped_read(scope, query):
    # The existing owning adapter deliberately accepts only top-level SELECT
    # for reads. Keep its protection and place this read-only CTE inside one.
    return 'SELECT * FROM (' + scope + query + ') AS conversation_scope11846'


def summary_conversations(database, actor, state):
    sql, params = source_scope(actor, state)
    with database.connect() as conn:
        return [dict(row) for row in conn.execute(scoped_read(sql,
            'SELECT c.id FROM conversations c WHERE c.user_id=? AND ('
            'SELECT COUNT(*) FROM scoped s WHERE s.conversation_id=c.id AND s.recent_rank>30 '
            'AND s.message_chars<=24000 AND s.sequence_no>COALESCE((SELECT MAX(segment_end) '
            'FROM ' + SUMMARY + ' z WHERE z.user_id=? AND z.conversation_id=c.id),0))>=8 '
            'ORDER BY c.updated_at ASC,c.id ASC LIMIT 16'), (*params, actor, actor)).fetchall()]


class ConversationProgress11846:
    def __init__(self, conversation, retriever):
        self.conversation, self.retriever = conversation, retriever
        self.db, self.clock = conversation.db, conversation.clock
        self._cursors = OrderedDict()
        self._last = OrderedDict()
        retriever.chunk_budget = self
        diagnostic(None, 'ready', schema_tables=85)

    @staticmethod
    def _bounded_put(mapping, key, value):
        mapping[key] = value
        mapping.move_to_end(key)
        while len(mapping) > 4096:
            mapping.popitem(last=False)

    def _model(self):
        value = getattr(getattr(self.retriever, 'client', None), 'model_revision', None)
        if value is None:
            from .qwen_multimodal11842 import MODEL_REVISION
            value = MODEL_REVISION
        return value

    def _documents(self, actor, refs, retrieval_state, model):
        documents = self.retriever.fulltext_documents(actor, refs, retrieval_state)
        ledger = self.retriever.chunk_ledger(actor, model, documents)
        existing = {ledger_identity(r) for r in ledger if r['modality'] == 'text'}
        return documents, existing

    def _job(self, actor, state, model, document):
        control = state.get('controls', {}).get(document['domain'])
        authority = (hashed([True, control.get('enabled_for_ai') is True,
                     control.get('collect_events') is True, control.get('forget_before'),
                     control.get('revision', 0)]) if control is not None
                     else state.get('authority_revision', state['revision']))
        return hashed(['fulltext-index11847', actor, authority, model, identity(document)])

    def _read(self, conn, actor, job, attempt, phase):
        row = conn.execute('SELECT request_digest,response_json,created_at FROM rpc_requests '
                           'WHERE user_id=? AND request_id=?', (actor, receipt_id(job, attempt, phase))).fetchone()
        if row is None:
            return None
        value = json.loads(row['response_json'])
        if (row['request_digest'] != job or type(value) is not dict or set(value) != {'conversation_index11846'}
                or value['conversation_index11846'].get('job') != job
                or value['conversation_index11846'].get('attempt') != attempt):
            raise ValueError('memory_job_receipt_conflict')
        return {**value['conversation_index11846'], 'at': row['created_at']}

    def _claim(self, actor, state, model, document):
        source_job = self._job(actor, state, model, document)
        now = self.clock().astimezone(timezone.utc)
        with self.db.transaction() as conn:
            self.retriever.lock_owner(conn, actor)
            self.retriever._resolve(actor, [document], self.retriever._state(actor, domains=[document['domain']]))
            # Recheck the precise live ledger inside the same owner lock.
            if identity(document) in {ledger_identity(r) for r in self.retriever.chunk_ledger(actor, model, [document])
                                      if r['modality'] == 'text'}:
                return None
            for round_number in (1, 2):
                job = hashed(['source-cache-round11846', source_job, round_number])
                successful = None
                for attempt in (1, 2):
                    previous = self._read(conn, actor, job, attempt, 'claim')
                    if previous is None:
                        body = {'conversation_index11846': {'version': 1, 'job': job,
                                'round': round_number, 'attempt': attempt, 'status': 'uncertain'}}
                        claimed = conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) '
                            'VALUES(?,?,?,?,?) ON CONFLICT(user_id,request_id) DO NOTHING RETURNING request_id',
                            (actor, receipt_id(job, attempt, 'claim'), job, canonical(body), timestamp_to_db(now))).fetchone()
                        return (job, attempt, round_number) if claimed is not None else None
                    terminal = self._read(conn, actor, job, attempt, 'terminal')
                    # A crash after claim is explicitly uncertain, never an immediate retry.
                    at = terminal['at'] if terminal else previous['at']
                    if terminal and terminal['status'] == 'complete':
                        successful = at
                        break
                    if now < instant(at) + timedelta(seconds=COOLDOWN_SECONDS):
                        return None
                # A second missing-cache round requires a prior successful round,
                # an actually absent precise vector, and another fifteen minutes.
                # Two failed attempts never silently create a fresh budget.
                if successful is None or now < instant(successful) + timedelta(seconds=COOLDOWN_SECONDS):
                    return None
            return None

    def _finish(self, actor, claim, status):
        job, attempt, round_number = claim
        body = {'conversation_index11846': {'version': 1, 'job': job, 'round': round_number,
                                          'attempt': attempt, 'status': status}}
        with self.db.transaction() as conn:
            self.retriever.lock_owner(conn, actor)
            conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) '
                         'VALUES(?,?,?,?,?) ON CONFLICT(user_id,request_id) DO NOTHING',
                         (actor, receipt_id(job, attempt, 'terminal'), job, canonical(body),
                          timestamp_to_db(self.clock())))

    def _emit(self, actor, state, documents, claims, deferred=0, already=0):
        empty = {'indexed_originals': 0, 'already_indexed': already, 'deferred_items': deferred,
                 'complete_history_indexed': False, 'dimensions': 512}
        if not documents:
            return {'state': 'waiting' if deferred else 'already_indexed', **empty}
        expected = {identity(d) for d in documents}
        try:
            self.retriever._same_state(actor, state)
            warmed = self.retriever.index_original_chunks(actor,
                [{'ref': d['ref'], 'chunk_no': d['chunk_no']} for d in documents], expected=expected)
            # A successful old journal is not proof that a vector still exists.
            # Persist success only while this exact source/model is searchable.
            self.retriever._same_state(actor, state)
            ledger = self.retriever.chunk_ledger(actor, self._model(), documents)
            present = {ledger_identity(r) for r in ledger if r['modality'] == 'text'}
            if not expected <= present:
                raise RuntimeError('semantic_vector_not_committed')
            for claim in claims:
                self._finish(actor, claim, 'complete')
            return {'state': 'ready', **empty, 'indexed_originals': warmed.get('indexed_originals', 0)}
        except Exception as error:
            diagnostic(actor, 'emit_failed', error=error, documents=len(documents), claims=len(claims))
            for claim in claims:
                try:
                    self._finish(actor, claim, 'failed')
                except Exception:
                    pass  # The immutable pre-send uncertain claim is authoritative.
            return {'state': 'unavailable', **empty, 'deferred_items': deferred + len(documents)}

    def warm_selected(self, actor, refs):
        """The legacy selected-summary warm uses the same finite attempt budget."""
        self.retriever._actor(actor)
        if self.retriever.settings_reader(actor).get('enabled') is not True:
            return {'state': 'disabled', 'indexed_originals': 0, 'deferred_items': 0}
        state = self.retriever._state(actor, domains=['ai_records'])
        if not state['domains']:
            return {'state': 'disabled', 'indexed_originals': 0, 'deferred_items': 0}
        if type(refs) is not list or len(refs) > 40 or any(
                type(r) is not dict or set(r) != {'ref'} or type(r['ref']) is not str
                or not r['ref'].startswith('messages:') or not r['ref'][9:].isdigit() for r in refs):
            raise ValueError('invalid_memory_references')
        try:
            _, model = self.retriever._provider()
            retrieval_state = self.retriever._state(actor, domains=['ai_records'])
            documents, existing = self._documents(actor, list(dict.fromkeys(r['ref'] for r in refs)), retrieval_state, model)
            fresh = [d for d in documents if identity(d) not in existing]
            selected, claims, used = [], [], 0
            for document in fresh:
                cost = len(document['content'].encode('utf-8'))
                if len(selected) == 6 or used + cost > 24000:
                    continue
                claim = self._claim(actor, state, model, document)
                if claim is not None:
                    selected.append(document)
                    claims.append(claim)
                    used += cost
            result = self._emit(actor, state, selected, claims,
                                deferred=len(fresh) - len(selected), already=len(documents) - len(fresh))
        except Exception:
            result = {'state': 'unavailable', 'indexed_originals': 0, 'deferred_items': len(refs)}
        self._bounded_put(self._last, actor, result['state'])
        return result

    def run_batch(self, actor):
        """At most six chunks; all formal target sources share this keyset."""
        stage = 'state'
        diagnostic(actor, 'run_enter')
        try:
            self.retriever._actor(actor)
            if self.retriever.settings_reader(actor).get('enabled') is not True:
                return {'state': 'disabled', 'indexed_originals': 0}
            state = self.retriever._state(actor, domains=['life_records', 'ai_records'])
            if not state['domains']:
                return {'state': 'disabled', 'indexed_originals': 0}
            stage = 'provider'
            if not self.retriever.store.available():
                raise RuntimeError('semantic_store_unavailable')
            _, model = self.retriever._provider()
            cursor_key = (actor, state['revision'], model)
            cursor = self._cursors.get(cursor_key)
            selected, claims, used = [], [], 0
            scanned = document_count = 0
            stopped = False
            for _ in range(SCAN_PAGES):
                stage = 'scan'
                rows = scan(self.db, actor, state, cursor, PAGE)
                scanned += len(rows)
                if not rows:
                    self._bounded_put(self._cursors, cursor_key, None)
                    break
                for row in rows:
                    previous = cursor
                    next_cursor = (row['at'], row['source_rank'], row['id'])
                    stage = 'documents'
                    # One business row at a time: even the legal 2m-character
                    # chat never multiplies by the metadata page size.
                    documents, existing = self._documents(actor, [row['ref']], state, model)
                    document_count += len(documents)
                    fresh = [d for d in documents if identity(d) not in existing]
                    all_selected = True
                    for document in fresh:
                        cost = len(document['content'].encode('utf-8'))
                        if len(selected) >= 6 or used + cost > 24000:
                            all_selected = False
                            stopped = True
                            break
                        stage = 'claim'
                        claim = self._claim(actor, state, model, document)
                        if claim is not None:
                            selected.append(document)
                            claims.append(claim)
                            used += cost
                    # An unfinished source stays at the front of the next
                    # batch; exact ledger checks skip its committed chunks.
                    cursor = next_cursor if all_selected else previous
                    self._bounded_put(self._cursors, cursor_key, cursor)
                    if stopped:
                        break
                if stopped:
                    break
                if selected:
                    break
                if len(rows) < PAGE:
                    self._bounded_put(self._cursors, cursor_key, None)
                    break
            stage = 'emit'
            result = self._emit(actor, state, selected, claims)
            if result['state'] != 'unavailable':
                diagnostic(actor, 'run_complete', scanned=scanned, documents=document_count,
                           claims=len(claims), indexed=result.get('indexed_originals', 0))
        except Exception as error:
            diagnostic(actor, stage, error=error)
            if stage == 'state':
                raise
            result = {'state': 'unavailable', 'indexed_originals': 0}
        self._bounded_put(self._last, actor, result['state'])
        return result

    def _summarized(self, actor, state, scope, params):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT * FROM ' + SUMMARY + " WHERE user_id=? AND status='ready' "
                                'ORDER BY segment_start LIMIT ?', (actor, STATUS_SUMMARY_LIMIT + 1)).fetchall()
        if len(rows) > STATUS_SUMMARY_LIMIT:
            return None
        identifiers = set()
        for raw in rows:
            try:
                _, refs = self.conversation._valid_segment(actor, dict(raw), state)
            except Exception:
                continue
            ids = [r['message_id'] for r in refs]
            if not ids:
                continue
            with self.db.connect() as conn:
                found = conn.execute(scoped_read(scope, 'SELECT id FROM scoped WHERE recent_rank>30 '
                    'AND message_chars<=24000 AND id IN (' + ','.join('?' for _ in ids) + ')'),
                    (*params, *ids)).fetchall()
            identifiers.update(r['id'] for r in found)
        return len(identifiers)

    def _indexed(self, actor, state, model, scope, params):
        # Existing UI counts older USER messages. A message counts only when
        # every current full-text chunk, including all pages, is in the ledger.
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(scoped_read(scope,
                "SELECT id FROM scoped WHERE recent_rank>30 AND role='user' ORDER BY id LIMIT ?"),
                (*params, STATUS_INDEX_LIMIT + 1)).fetchall()]
        if len(rows) > STATUS_INDEX_LIMIT:
            return None
        retrieval_state = self.retriever._state(actor, domains=['ai_records'])
        valid = used = 0
        for row in rows:
            documents, existing = self._documents(actor, ['messages:' + str(row['id'])], retrieval_state, model)
            used += sum(len(d['content']) for d in documents)
            if used > 8_200_000:
                return None  # Honest unknown; GET never drains or calls models.
            if documents and all(identity(d) in existing for d in documents):
                valid += 1
        return valid

    def status(self, actor):
        state = self.conversation._state(actor)
        empty = {'schema_version': 1, 'enabled': state['enabled'], 'state': 'disabled' if not state['enabled'] else 'unavailable',
                 'messages': {k: None for k in ('eligible_older', 'summarized', 'pending', 'recent_direct')},
                 'search': {k: None for k in ('eligible_user', 'indexed_user', 'pending_user')},
                 'updated_at': None, 'retry_after_seconds': 60}
        if not state['enabled']:
            return empty
        try:
            scope, params = source_scope(actor, state)
            with self.db.connect() as conn:
                row = conn.execute(scoped_read(scope, 'SELECT '
                    'COALESCE(SUM(CASE WHEN recent_rank>30 THEN 1 ELSE 0 END),0) AS older,'
                    'COALESCE(SUM(CASE WHEN recent_rank<=30 THEN 1 ELSE 0 END),0) AS recent,'
                    "COALESCE(SUM(CASE WHEN recent_rank>30 AND role='user' THEN 1 ELSE 0 END),0) AS users "
                    'FROM scoped'), tuple(params)).fetchone()
                updated = conn.execute('SELECT MAX(updated_at) AS at FROM ' + SUMMARY + ' WHERE user_id=?', (actor,)).fetchone()
                indexed_at = conn.execute('SELECT MAX(updated_at) AS at FROM ' + VECTORS
                    + ' WHERE user_id=? AND model_revision=?', (actor, self._model())).fetchone()
            stamps = [instant(row['at']) for row in (updated, indexed_at) if row and row['at'] is not None]
            last_updated = max(stamps).isoformat() if stamps else None
            older, recent, users = int(row['older']), int(row['recent']), int(row['users'])
            summarized = self._summarized(actor, state, scope, params) if older else 0
            indexed = (self._indexed(actor, state, self._model(), scope, params)
                       if self.retriever.store.available() else None) if users else 0
            if self.conversation._state(actor) != state:
                return empty
            pending = None if summarized is None else max(0, older - summarized)
            pending_user = None if indexed is None else max(0, users - indexed)
            if getattr(self.conversation, '_running_actor', None) == actor:
                status = 'running'
            elif indexed is None or summarized is None:
                status = 'partial' if (indexed or summarized) else 'unavailable'
            elif pending or pending_user:
                status = 'unavailable' if self._last.get(actor) == 'unavailable' else 'partial' if summarized or indexed else 'waiting'
            else:
                status = 'idle'
            return {**empty, 'state': status,
                    'messages': {'eligible_older': older, 'summarized': summarized, 'pending': pending, 'recent_direct': recent},
                    'search': {'eligible_user': users, 'indexed_user': indexed, 'pending_user': pending_user},
                    'updated_at': last_updated}
        except Exception:
            return empty
