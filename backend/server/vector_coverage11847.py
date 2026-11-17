"""Canonical full business text, bounded chunks, and owner-only source loading.

No model calls, jobs, or writes live here. Internal page references fit the
reviewed vector schema; they always resolve to the original business row.
"""
from __future__ import annotations

import json
import re

from .member_memory_core import _row_hash, iso, parsed_json, timestamp

TARGETS = ('life_records', 'expansion_observations', 'messages')
PIPELINE = 'owner-fulltext11847:unicode1900:pages32:role-bound:v1'
BODY_CHARS = 1900
CHUNKS_PER_PAGE = 32
REF = re.compile(r'^(life_records|expansion_observations|messages):([1-9][0-9]{0,18})(?::part:([1-9][0-9]{0,8}))?$')
MAX_PLAN_CHARS = 32 * 1024 * 1024  # existing RPC envelope, not a new text truncation


def source_ref(value):
    match = REF.fullmatch(value) if type(value) is str else None
    if match is None or int(match[2]) >= 2**53:
        raise ValueError('invalid_fulltext_reference')
    table, identifier, page = match[1], int(match[2]), int(match[3] or 0)
    return table, identifier, page, table + ':' + str(identifier)


def is_target(value):
    return type(value) is str and value.partition(':')[0] in TARGETS


def page_ref(root, page):
    return root if page == 0 else root + ':part:' + str(page)


def safe_fields(value):
    """Omit opaque storage handles, never clip user text or list elements."""
    if type(value) is dict:
        return {k: safe_fields(v) for k, v in value.items() if type(k) is str
                and not k.endswith(('_id', '_ids')) and k not in {'id', 'sha256', 'source_hash'}}
    if type(value) is list:
        return [safe_fields(v) for v in value]
    return value


def rendered_fields(table, row):
    if table == 'messages':
        return str(row.get('content') or '')
    if table == 'life_records':
        fields = {'category': row['category'], 'content': row['content'],
                  'details': safe_fields(parsed_json(row.get('details_json'), {}))}
    elif table == 'expansion_observations':
        fields = {'metric_kind': row['kind'],
                  'details': safe_fields(parsed_json(row.get('payload_json'), {}))}
    else:
        raise ValueError('unsupported_fulltext_source')
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def plan(table, row, hash_document):
    """All text is covered. Each persisted chunk fits the existing 2k CHECK."""
    root = table + ':' + str(row['id'])
    role = row['role'] if table == 'messages' else 'user_record'
    if role not in {'user', 'assistant', 'user_record'}:
        raise ValueError('unsupported_fulltext_role')
    body = rendered_fields(table, row)
    if len(body) > MAX_PLAN_CHARS:
        raise ValueError('fulltext_source_exceeds_business_contract')
    if not body.strip():
        return []
    domain = 'ai_records' if table == 'messages' else 'life_records'
    when = 'created_at' if table == 'messages' else 'occurred_at' if table == 'life_records' else 'measured_at'
    at = iso(row.get(when))
    if at is None:
        return []
    digest = _row_hash(row)
    if role == 'assistant':
        prefix = '[AI历史回复；不是用户事实]\n'
    elif role == 'user':
        prefix = '[用户聊天原话]\n'
    elif table == 'life_records':
        prefix = '[用户正式记录；类别：' + str(row['category'])[:40] + ']\n'
    else:
        prefix = '[用户正式身体指标；类型：' + str(row['kind'])[:40] + ']\n'
    result = []
    for number, offset in enumerate(range(0, len(body), BODY_CHARS)):
        item = {'ref': page_ref(root, number // CHUNKS_PER_PAGE), 'domain': domain,
                'source_hash': digest, 'occurred_at': at,
                'kind': 'assistant_context' if role == 'assistant' else 'raw_record',
                'content': prefix + body[offset:offset + BODY_CHARS], 'modality': 'text',
                'chunk_no': number % CHUNKS_PER_PAGE, 'image_source_ref': None, 'image_sha256': None}
        item['content_hash'] = hash_document(item)
        result.append(item)
    return result


def load_row(database, actor, ref, state):
    table, identifier, _, root = source_ref(ref)
    domain = 'ai_records' if table == 'messages' else 'life_records'
    if domain not in state['domains']:
        return None
    with database.connect() as conn:
        if table == 'messages':
            raw = conn.execute('SELECT m.id,m.conversation_id,m.sequence_no,m.role,m.content,m.status,'
                'm.created_at,m.updated_at,c.title FROM messages m JOIN conversations c ON c.id=m.conversation_id '
                "WHERE c.user_id=? AND m.id=? AND m.status='complete' AND m.role IN ('user','assistant')",
                (actor, identifier)).fetchone()
            when = 'created_at'
        elif table == 'life_records':
            raw = conn.execute('SELECT id,category,occurred_at,local_date,content,details_json,source,created_at,updated_at '
                               'FROM life_records WHERE user_id=? AND id=?', (actor, identifier)).fetchone()
            when = 'occurred_at'
        else:
            raw = conn.execute('SELECT id,kind,measured_at,payload_json,created_at,updated_at '
                               'FROM expansion_observations WHERE user_id=? AND id=?', (actor, identifier)).fetchone()
            when = 'measured_at'
    if raw is None:
        return None
    row = dict(raw)
    at = timestamp(row.get(when))
    cutoff = timestamp(state['controls'].get(domain, {}).get('forget_before'))
    if at is None or cutoff and at <= cutoff:
        return None
    return table, root, row


def documents(database, actor, refs, state, hash_document, *, selected=None):
    """Bounded callers select roots; no newest-page/excerpt substitution."""
    roots = list(dict.fromkeys(source_ref(ref)[3] for ref in refs))
    result = []
    used = 0
    for root in roots:
        loaded = load_row(database, actor, root, state)
        if loaded:
            # One unusually long legal source is allowed. A caller cannot
            # accidentally retain hundreds of such full plans at once.
            length = len(rendered_fields(loaded[0], loaded[2]))
            if selected is None and used and used + length > 4_100_000:
                raise ValueError('fulltext_planning_budget_exceeded')
            current = plan(loaded[0], loaded[2], hash_document)
            if selected is not None:
                current = [d for d in current if (d['ref'], d['chunk_no']) in selected]
            result.extend(current)
            used += sum(len(d['content']) for d in current)
    return result


def identity(item):
    return item['ref'], item['chunk_no'], item['source_hash'], item['content_hash']


def ledger_identity(row):
    return row['source_ref'], row['chunk_no'], row['source_hash'], row['content_hash']


def scan(database, actor, state, cursor=None, limit=128):
    """Metadata only, global keyset across all requested source types/roles."""
    statements, params = [], []
    for rank, table in enumerate(TARGETS):
        domain = 'ai_records' if table == 'messages' else 'life_records'
        if domain not in state['domains']:
            continue
        if table == 'messages':
            sql = ('SELECT m.created_at AS at,' + str(rank) + ' AS source_rank,m.id AS id FROM messages m '
                   'JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? '
                   "AND m.status='complete' AND m.role IN ('user','assistant')")
            when = 'm.created_at'
        else:
            when = 'occurred_at' if table == 'life_records' else 'measured_at'
            sql = 'SELECT created_at AS at,' + str(rank) + ' AS source_rank,id FROM ' + table + ' WHERE user_id=?'
        params.append(actor)
        cutoff = state['controls'].get(domain, {}).get('forget_before')
        if cutoff:
            sql += ' AND ' + when + '>?'
            params.append(cutoff)
        statements.append(sql)
    if not statements:
        return []
    query = 'SELECT * FROM (' + ' UNION ALL '.join(statements) + ') AS fulltext_sources11847'
    if cursor:
        query += ' WHERE (at>? OR (at=? AND source_rank>?) OR (at=? AND source_rank=? AND id>?))'
        params.extend([cursor[0], cursor[0], cursor[1], cursor[0], cursor[1], cursor[2]])
    query += ' ORDER BY at,source_rank,id LIMIT ?'
    with database.connect() as conn:
        rows = [dict(row) for row in conn.execute(query, (*params, limit)).fetchall()]
    return [{**row, 'ref': TARGETS[row['source_rank']] + ':' + str(row['id'])} for row in rows]
