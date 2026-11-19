"""Owned, resumable record-draft tasks using the existing immutable receipt journal."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

from ollama_chat_app.data.database import timestamp_from_db, timestamp_to_db

from .ai139_tools import ToolError, canonical

CAPABILITY = 'try1.2-tasks-v1'
MAX_PAGE_SIZE = 40
MAX_QUERY_LENGTH = 120
_TIME_CURSOR = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z')
_CATEGORY_PATTERNS = {
    'water': r'饮水|喝水|水量',
    'diet': r'饮食|吃饭|早餐|早饭|午餐|午饭|中餐|中饭|晚餐|晚饭|夜宵|加餐',
    'activity': r'运动|锻炼|跑步|步数|散步',
    'sleep': r'睡眠|睡觉',
    'environment': r'居住环境|环境',
    'medical': r'医疗检查|就医|问诊|就诊|检查',
    'metric': r'身体指标|体重|血糖|血压|详细指标',
}
_MEAL_WORDS = {'早餐': '早餐', '早饭': '早餐', '午餐': '午餐', '午饭': '午餐', '中餐': '午餐',
               '中饭': '午餐', '晚餐': '晚餐', '晚饭': '晚餐', '加餐': '加餐', '夜宵': '加餐'}
_SWITCH = re.compile(r'^(?:请|帮我|先|再|我想|麻烦)*\s*(?:回到|切回|切换到|打开|接着|继续|恢复|找出|选中|找到)')
_EDIT_TASK = re.compile(r'^(?:请|帮我|先|再|我想|麻烦)*(?:给|把|在|为)(?P<target>.+?(?:草稿|待完成记录|未完成记录|未保存记录|任务))(?P<reply>补充?|加上?|修改|更正|改)')
_REF = re.compile(r'(?:草稿|任务|待完成|未完成|未保存|那[份条个]|这[份条个]|刚才|刚刚|前面|上次|之前)')
_BARE_REFERENCE = re.compile(r'^(?:就|是|那)?(?:刚才|刚刚|前面|上次|之前)(?:的)?那(?:份|条|个)'
                             r'(?:草稿|记录|任务)?[。.!！ ]*$')
_NEW = re.compile(r'(?:先|重新|另外|再|另)?(?:新增|新建|记一条|另记|再记|重新记|换一条|新记录)')
_NEW_AT_START = re.compile(r'^(?:(?:请|帮我|给我|我要|我想|麻烦|先|另外|然后|再|另|重新)\s*)*'
                           r'(?:新增|新建|另开|新开|记(?!得)|写一条|换一条)')
_INLINE_REPLY = re.compile(r'(?P<target>.+?)(?P<reply>帮我补[充上]?|补充|补上|补|加上|改成|更正为)(?=\S)')
_DATE_SHAPE = re.compile(r'\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}[日号]')
_CLOCK_SHAPE = re.compile(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)')


@dataclass(frozen=True)
class Criteria:
    query: str
    day: str | None
    clock: str | None
    period: str | None
    category: str | None
    meal: str | None
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class Reference:
    lookup: Criteria
    reply: str


def canonical_id(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def normalized(value):
    return unicodedata.normalize('NFKC', value).casefold().strip()


def criteria(query, *, reference=None):
    from .record_assistant_try12 import _explicit_time_parts, _period_clock, _TIME_PERIOD, _PERIOD_BY_WORD

    value = normalized(query).replace('夜里', '深夜')
    if len(value) > MAX_QUERY_LENGTH:
        raise ToolError('record_task_query_too_long', 422)
    parts = _explicit_time_parts(value, reference=reference)
    if not parts.get('record_date') and _DATE_SHAPE.search(value):
        raise ToolError('invalid_record_task_date', 422)
    category = next((key for key, pattern in _CATEGORY_PATTERNS.items() if re.search(pattern, value)), None)
    meal = next((key for word, key in _MEAL_WORDS.items() if word in value), None)
    period_match = _TIME_PERIOD.search(value)
    period = parts.get('record_time_hint') or (_PERIOD_BY_WORD[period_match[0]] if period_match else None)
    clock = parts.get('record_time')
    if clocks := list(_CLOCK_SHAPE.finditer(value)):
        if len(clocks) > 1:
            raise ToolError('invalid_record_task_time', 422)
        hour, minute = map(int, clocks[0].groups())
        shifted = _period_clock(hour, period)
        if shifted is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ToolError('invalid_record_task_time', 422)
        clock = f'{shifted:02d}:{minute:02d}'
    remaining = value
    for pattern in _CATEGORY_PATTERNS.values():
        remaining = re.sub(pattern, '', remaining)
    remaining = re.sub(
        r'\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}[日号]'
        r'|(?:\d{1,2}[:：]\d{2}|[一二两三四五六七八九十零\d]{1,3}[点时](?:半|[零一二三四五六七八九十\d]{1,4}分?)?)'
        r'|星期[一二三四五六日天]|周[一二三四五六日天]', '', remaining)
    remaining = _TIME_PERIOD.sub('', remaining)
    remaining = re.sub(
        r'待完成|未完成|未保存|记录|草稿|任务|今天|昨天|前天|刚才|刚刚|前面|上次|之前'
        r'|那个|那条|那份|这条|这份|那个|那一条|这一条|我的|的', '', remaining)
    tokens = tuple(item for item in re.split(r'\s+|[，,。；;]', remaining) if item)
    return Criteria(value, parts.get('record_date'), clock, period, category, meal, tokens)


def parse_reference(text, *, reference=None):
    """Use the part naming the task for routing and only the rest for changes."""
    value = unicodedata.normalize('NFKC', text).strip()
    if not value or len(value) > 1200:
        return None
    if match := _EDIT_TASK.match(value):
        reply = value[match.start('reply'):].strip()
        return Reference(criteria(match['target'], reference=reference), reply)
    if _BARE_REFERENCE.fullmatch(value):
        return Reference(criteria(value.rstrip('。.!！ '), reference=reference), '')
    match = _SWITCH.match(value)
    if not match:
        return None
    body = value[match.end():].strip()
    # Full-width punctuation is folded to ASCII, but the colon in 08:00
    # remains a time separator, never a task/change separator.
    split = re.split(r'[，,。；;\n]|:(?!\d)', body, maxsplit=1)
    if len(split) == 1 and (inline := _INLINE_REPLY.match(body)):
        split = [inline['target'], body[inline.start('reply'):]]
    target = split[0].strip()
    clued = _REF.search(target) or any(re.search(pattern, target) for pattern in _CATEGORY_PATTERNS.values())
    if not clued or not target or len(target) > MAX_QUERY_LENGTH or _NEW.search(target):
        return None
    return Reference(criteria(target, reference=reference), split[1].strip() if len(split) == 2 else '')


def is_new_request(value):
    from .record_assistant_try12 import _EXAMPLE, _PLAN_OR_REMINDER

    return bool(_NEW_AT_START.search(value) and any(re.search(pattern, value) for pattern in _CATEGORY_PATTERNS.values())
                and not (_EXAMPLE.search(value) or _PLAN_OR_REMINDER.search(value)))


def draft_title(draft):
    from .record_assistant_try12 import CATEGORIES, _temporal_facts

    temporal = _temporal_facts(draft)
    day, clock = temporal.get('record_date'), temporal.get('record_time')
    details = draft.get('details') or {}
    meal_name = {'breakfast': '早餐', 'lunch': '午餐', 'dinner': '晚餐', 'snack': '加餐'}
    meal = meal_name.get(details.get('meal_type')) or _MEAL_WORDS.get(details.get('meal_type'))
    headline = meal or CATEGORIES.get(draft['category'], '生活记录')
    title = ' '.join(item for item in (day, clock, headline) if item)
    return title, day, clock, meal


class RecordTasks:
    def __init__(self, tools):
        self.tools = tools
        self.db = tools.db

    @staticmethod
    def _digest(actor, conversation, kind='tasks'):
        return hashlib.sha256(f'fawnsee-record-tasks11864:{actor}:{conversation}:{kind}'.encode()).hexdigest()

    def _json(self, field, column='payload'):
        if getattr(self.db, 'is_postgres', False):
            return f"(CAST({column} AS jsonb)->>'{field}')"
        return f"json_extract({column}, '$.{field}')"

    def _check_chat(self, conn, actor, conversation):
        if (type(conversation) is not int or not 1 <= conversation <= 9223372036854775807
                or conn.execute('SELECT id FROM conversations WHERE id=? AND user_id=?',
                                (conversation, actor)).fetchone() is None):
            raise ToolError('record_task_unavailable', 404)

    def _scope(self, conn, actor, conversation, kind, value):
        digest = self._digest(actor, conversation, kind)
        previous = conn.execute('SELECT MAX(created_at) AS stamp FROM rpc_requests WHERE user_id=? AND request_digest=?',
                                (actor, digest)).fetchone()
        now = datetime.fromtimestamp(self.tools.clock(), timezone.utc)
        if previous and previous['stamp']:
            now = max(now, timestamp_from_db(previous['stamp']) + timedelta(microseconds=1))
        conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) VALUES(?,?,?,?,?)',
                     (actor, str(uuid4()), digest, canonical(value), timestamp_to_db(now)))

    def _open_where(self):
        # The very same successful receipt that authorizes an official record
        # removes it from pending tasks, including saves by older clients.
        postgres = getattr(self.db, 'is_postgres', False)
        status = ("(CAST(saved.response_json AS jsonb)->'mobile149'->>'status')" if postgres
                  else "json_extract(saved.response_json, '$.mobile149.status')")
        mobile, linked, replaced = (self._json(key) for key in ('mobile_receipt', 'draft_id', 'replacement_receipt'))
        if postgres:
            mobile, linked, replaced = (f'CAST({item} AS uuid)' for item in (mobile, linked, replaced))
        return (
            f" AND {self._json('status')} NOT IN ('closed','cancelled') AND NOT EXISTS ("
            "SELECT 1 FROM rpc_requests saved WHERE saved.user_id=? "
            f"AND saved.request_id={mobile} AND {status}='applied') AND NOT EXISTS ("
            "SELECT 1 FROM rpc_requests saved WHERE saved.user_id=? "
            f"AND saved.request_id IN ({linked},{replaced}))"
        )

    def _query(self, actor, conversation, find=None, *, task_id=None, cursor=None, opened=True):
        id_col = self._json('id', 'response_json')
        source = (" FROM (SELECT response_json AS payload, created_at AS stamp,"
                  f" ROW_NUMBER() OVER (PARTITION BY {id_col} ORDER BY created_at DESC, request_id DESC) AS seq"
                  " FROM rpc_requests WHERE user_id=? AND request_digest=?")
        args = [actor, self._digest(actor, conversation)]
        if task_id:
            source += f" AND {id_col}=?"
            args.append(task_id)
        source += ') pending WHERE seq=1'
        if opened:
            source += self._open_where()
            args.extend((actor, actor))
        if find:
            for key, value in (('category', find.category), ('record_date', find.day),
                               ('record_time', find.clock), ('meal', find.meal)):
                if value:
                    source += f' AND {self._json(key)}=?'
                    args.append(value)
            if find.period and not find.clock:
                from .record_assistant_try12 import _HOURS_BY_PERIOD
                hours = _HOURS_BY_PERIOD[find.period]
                clock = self._json('record_time')
                period = self._json('period')
                placeholders = ','.join('?' for _ in hours)
                source += f' AND (substr({clock},1,2) IN ({placeholders}) OR ({clock} IS NULL AND {period}=?))'
                args.extend([*(f'{hour:02d}' for hour in hours), find.period])
            for token in find.tokens:
                source += f' AND instr({self._json("search")},?)>0'
                args.append(token)
        if cursor:
            source += ' AND stamp<?'
            args.append(cursor)
        return source, tuple(args)

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ('id', 'title', 'category', 'record_date', 'record_time',
                                        'status', 'assistant_message_id', 'draft_id', 'missing', 'preview')}

    def _get(self, conn, actor, conversation, task_id, *, opened=True):
        if not canonical_id(task_id):
            return None
        where, args = self._query(actor, conversation, task_id=task_id, opened=opened)
        row = conn.execute('SELECT payload' + where, args).fetchone()
        return json.loads(row['payload']) if row else None

    def _active(self, conn, actor, conversation):
        row = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_digest=? '
                           'ORDER BY created_at DESC LIMIT 1', (actor, self._digest(actor, conversation, 'focus'))).fetchone()
        return json.loads(row['response_json'])['id'] if row else None

    def _set_active(self, conn, actor, conversation, task_id):
        self._scope(conn, actor, conversation, 'focus', {'id': task_id})

    def list(self, principal, conversation, query='', cursor=None, limit=20):
        actor = self.tools._member(principal)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ToolError('invalid_record_task_page', 422)
        if cursor is not None and (not isinstance(cursor, str) or not _TIME_CURSOR.fullmatch(cursor)):
            raise ToolError('invalid_record_task_cursor', 422)
        find = criteria(query)
        with self.tools.auth.identity(principal), self.db.connect() as conn:
            self._check_chat(conn, actor, conversation)
            all_where, all_args = self._query(actor, conversation, find)
            total = conn.execute('SELECT COUNT(*) AS total' + all_where, all_args).fetchone()['total']
            where, args = self._query(actor, conversation, find, cursor=cursor)
            rows = conn.execute('SELECT payload,stamp' + where + ' ORDER BY stamp DESC LIMIT ?',
                                (*args, limit + 1)).fetchall()
            active = self._active(conn, actor, conversation)
            if active and not self._get(conn, actor, conversation, active):
                active = None
        return {'tasks': [self._public(json.loads(row['payload'])) for row in rows[:limit]],
                'next_cursor': rows[limit - 1]['stamp'] if len(rows) > limit else None,
                'total': total, 'active_task_id': active}

    def _draft(self, conn, actor, row):
        run = self.tools._read(conn, actor, row['assistant_message_id'])
        if run and run.get('status') == 'complete':
            return next((draft for draft in run.get('record_drafts', [])
                         if draft['id'] == row['draft_id'] and not draft.get('blocked')), None)
        return None

    def cancel_draft(self, principal, message_id, draft_id):
        """Close only this owned card revision and fence a later stale confirmation."""
        from .record_assistant_try12 import _has_saved_draft, draft_replacement_marker

        actor = self.tools._member(principal)
        if type(message_id) is not int or not 0 < message_id < 2**63 or not canonical_id(draft_id):
            raise ToolError('invalid_record_draft', 422)
        receipt = {'id': draft_id, 'status': 'cancelled'}
        # Uses the same database write transaction lock as confirmations and revisions.
        with self.tools.auth.identity(principal), self.db.transaction() as conn:
            msg = conn.execute("SELECT m.conversation_id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                               "WHERE m.id=? AND m.role='assistant' AND c.user_id=?", (message_id, actor)).fetchone()
            if not msg:
                raise ToolError('record_task_unavailable', 404)
            run = self.tools._read(conn, actor, message_id)
            draft = next((d for d in (run or {}).get('record_drafts', []) if d.get('id') == draft_id), None)
            if not draft:
                raise ToolError('record_task_unavailable', 404)
            if run.get('status') != 'complete':
                raise ToolError('record_draft_changed', 409)
            if receipt in run.get('record_receipts', []):
                return receipt
            marker = draft_replacement_marker(actor, draft_id)
            if (draft.get('superseded') or _has_saved_draft(conn, actor, draft_id)
                    or conn.execute('SELECT 1 FROM rpc_requests WHERE user_id=? AND request_id=?',
                                    (actor, marker)).fetchone()):
                raise ToolError('record_draft_changed', 409)
            # This per-revision fence is already honored by both local-sync and
            # linked-meal confirmations. Existing client versions cannot reopen it.
            timestamp = timestamp_to_db(datetime.fromtimestamp(self.tools.clock(), timezone.utc))
            digest = hashlib.sha256(f'cancel-record-draft:{actor}:{message_id}:{draft_id}'.encode()).hexdigest()
            conn.execute('INSERT INTO rpc_requests(user_id,request_id,request_digest,response_json,created_at) '
                         'VALUES(?,?,?,?,?)', (actor, marker, digest, canonical({'cancelled_record_draft':draft_id}), timestamp))
            draft['blocked'] = True
            run.setdefault('record_receipts', []).append(receipt)
            self.tools._save(conn, actor, run)
            conversation = msg['conversation_id']
            where, args = self._query(actor, conversation, opened=False)
            row = conn.execute('SELECT payload' + where + f" AND {self._json('draft_id')}=?",
                               (*args, draft_id)).fetchone()
            if row:
                self._scope(conn, actor, conversation, 'tasks', {**json.loads(row['payload']), 'status':'cancelled'})
        return receipt

    def focus(self, principal, conversation, task_id):
        from .record_assistant_try12 import project_record_photo_drafts

        actor = self.tools._member(principal)
        with self.tools.auth.identity(principal), self.db.transaction() as conn:
            self._check_chat(conn, actor, conversation)
            task = self._get(conn, actor, conversation, task_id)
            draft = self._draft(conn, actor, task) if task else None
            if not draft:
                raise ToolError('record_task_unavailable', 404)
            self._set_active(conn, actor, conversation, task['id'])
            public = project_record_photo_drafts([draft], photo_enabled=True, dialogue_enabled=True,
                                                  time_hints_enabled=True, extended_photos=True)[0]
        return {'task': self._public(task), 'draft': public}

    def _prior(self, conn, actor, conversation, row, draft):
        from .record_assistant_try12 import _dialogue_rows

        # Include only the user's turns associated with this exact task.
        source = f"SELECT {self._json('assistant_message_id','r.response_json')} AS mid FROM rpc_requests r "
        source += f"WHERE r.user_id=? AND r.request_digest=? AND {self._json('id','r.response_json')}=? "
        source += 'ORDER BY r.created_at DESC LIMIT 3'
        turns = conn.execute(source, (actor, self._digest(actor, conversation), row['id'])).fetchall()
        pairs = []
        latest_user = ''
        origin = draft.get('_record_origin_message_id', row['assistant_message_id'])
        origin_text = ''
        origin_stamp = None
        def pair(mid):
            return conn.execute(
                "SELECT u.role,substr(u.content,1,1200) AS content,u.created_at FROM messages a "
                "JOIN messages u ON u.conversation_id=a.conversation_id AND u.sequence_no=a.sequence_no-1 "
                "WHERE a.id=? AND a.conversation_id=? AND a.role='assistant' AND u.role='user'",
                (mid, conversation)).fetchone()
        for turn in reversed(turns):
            if item := pair(int(turn['mid'])):
                pairs.append(item)
                latest_user = item['content']
        if item := pair(origin):
            origin_text = item['content']
            origin_stamp = str(item['created_at']) if item['created_at'] else None
        optional = draft.get('missing_optional', [])
        return {'drafts': [draft], 'user': latest_user, 'active_record_context': _dialogue_rows(pairs),
                'record_start': origin_text, 'record_start_at': origin_stamp, 'origin_message_id': origin,
                'ready_followup': not draft.get('missing_required'),
                **({'optional_ready': optional, 'optional_followup': True}
                   if optional and not draft.get('missing_required') else {})}

    def resolve(self, principal, meta, reference=None):
        actor = self.tools._member(principal)
        conversation = meta['conversation_id']
        with self.tools.auth.identity(principal), self.db.connect() as conn:
            self._check_chat(conn, actor, conversation)
            if reference:
                where, args = self._query(actor, conversation, reference.lookup)
                rows = conn.execute('SELECT payload' + where + ' ORDER BY stamp DESC LIMIT ?',
                                    (*args, 6)).fetchall()
                matches = [json.loads(row['payload']) for row in rows]
                if len(matches) != 1:
                    return None, {}, [self._public(row) for row in matches]
                task = matches[0]
            else:
                selected = meta.get('record_task_id') or self._active(conn, actor, conversation)
                task = self._get(conn, actor, conversation, selected) if selected else None
                if task is None:
                    return None, {}, []
            draft = self._draft(conn, actor, task)
            if not draft or draft['id'] in meta.get('decided_record_draft_ids', ()):
                return None, {}, []
            return task, self._prior(conn, actor, conversation, task, draft), []

    def finish_focus(self, conn, identity, task):
        actor, conversation = identity['actor'], identity['conversation_id']
        current = self._get(conn, actor, conversation, task['id'])
        if not current or current['draft_id'] != task['draft_id']:
            raise ToolError('record_draft_changed', 409)
        self._set_active(conn, actor, conversation, task['id'])

    def apply(self, conn, identity, drafts, task_source):
        """Append locator revisions inside the same transaction as the draft."""
        from .mobile_sync149 import NAMESPACE
        from .record_assistant_try12 import draft_replacement_marker

        actor, conversation = identity['actor'], identity['conversation_id']
        active = None
        for draft in drafts:
            matching = task_source if task_source and draft.get('supersedes_draft_id') == task_source['draft_id'] else None
            identifier = matching['id'] if matching else str(uuid4())
            title, day, clock, meal = draft_title(draft)
            details = draft.get('details') or {}
            snippets = []
            for key in ('food', 'name', 'activity', 'notes', 'record_notes', 'description', 'amount_ml'):
                item = details.get(key)
                if item:
                    snippets.append(f'{item} 毫升' if key == 'amount_ml' else str(item))
            preview = (' · '.join(dict.fromkeys(snippets)) or str(draft.get('content') or ''))[:120]
            search = normalized(' '.join((title, preview, str(draft.get('content') or '')[:140])))
            value = {'id': identifier, 'assistant_message_id': identity['assistant_message_id'], 'draft_id': draft['id'],
                     'mobile_receipt': str(uuid5(NAMESPACE, f'{actor}:{draft["id"]}')), 'title': title,
                     'replacement_receipt': draft_replacement_marker(actor, draft['id']),
                     'record_date': day, 'record_time': clock, 'period': draft.get('record_time_hint'),
                     'category': draft['category'], 'meal': meal, 'search': search, 'preview': preview,
                     'missing': [item.get('label', item['field']) for item in draft.get('missing_required', ())],
                     'status': ('closed' if draft.get('blocked') else 'conflict' if draft.get('_draft_conflicts')
                                else 'needs_input' if draft.get('missing_required')
                                else 'ready')}
            self._scope(conn, actor, conversation, 'tasks', value)
            if value['status'] != 'closed':
                active = identifier
        if active and len(drafts) == 1:
            self._set_active(conn, actor, conversation, active)
