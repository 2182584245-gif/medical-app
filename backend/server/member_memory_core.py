"""Owner-scoped, dated evidence over existing business facts.

No model call, raw SQL endpoint, credential access, runtime DDL or copied file
binary belongs here. Original business rows remain the source of truth. Daily
rows contain statistics and provenance references, never a second transcript.
"""
from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo
from uuid import UUID

BEIJING = ZoneInfo('Asia/Shanghai')
UTC = timezone.utc
DOMAINS = ('profile', 'life_records', 'reminders', 'goals_habits',
           'services_appointments', 'reading', 'shopping', 'forum',
           'operations', 'preferences', 'ai_records')
CATEGORIES = ('diet', 'water', 'activity', 'sleep', 'environment', 'medical', 'metric')
PROFILE_FIELDS = ('display_name', 'gender', 'birth_date', 'age', 'phone', 'height_cm', 'weight_kg',
                  'living_situation', 'ai_preferred_name', 'reminder_frequency',
                  'emergency_contact_name',
                  'emergency_contact_phone', 'health_goals', 'medical_notes')
DOMAIN_WORDS = {
    'profile': ('姓名', '名字', '称呼', '生日', '年龄', '性别', '身高', '电话', '手机', '联系人', '个人信息', '住', '偏好'),
    'life_records': ('生活', '吃', '饮食', '喝', '饮水', '睡', '运动', '散步', '步数', '环境', '温度', '血压', '血糖', '体重', '医疗', '服药', '就医', '指标', '身体'),
    'reminders': ('提醒', '闹钟', '通知'),
    'goals_habits': ('目标', '习惯', '打卡', '坚持'),
    'services_appointments': ('预约', '服务', '顾问', '上门', '回访'),
    'reading': ('阅读', '读书', '文章', '读了', '读过', '收藏文章'),
    'shopping': ('购买', '购物', '订单', '商品', '购物车', '收藏夹', '花费', '消费'),
    'forum': ('论坛', '帖子', '发帖', '读帖', '点赞', '评论', '社区'),
    'operations': ('操作', '点击', '使用应用', '使用软件', '打开页面', '访问', '频次'),
    'preferences': ('喜欢', '偏好', '习惯', '经常', '倾向'),
    'ai_records': ('聊过', '对话', '聊天', '问过', '说过', '提过', '上次', '之前', '记得', '附件', '图片', '文件'),
}


class MemoryCoreError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def explicit_preference_entries(values):
    """Validate literal member edits; never infer, summarize or execute text."""
    if type(values) is not list or len(values) > 20:
        raise MemoryCoreError('invalid_ai_profile_entries')
    result = []
    identifiers = set()
    for item in values:
        if type(item) is not dict or set(item) != {'id', 'text'}:
            raise MemoryCoreError('invalid_ai_profile_entry')
        identifier, text = item['id'], item['text']
        try:
            if type(identifier) is not str or str(UUID(identifier)) != identifier or identifier in identifiers:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise MemoryCoreError('invalid_ai_profile_entry') from None
        if (type(text) is not str or not 1 <= len(text.strip()) <= 2000 or
                any(ord(char) < 32 and char not in '\n\t' for char in text)):
            raise MemoryCoreError('invalid_ai_profile_text')
        identifiers.add(identifier)
        result.append({'id': identifier, 'text': text.strip()})
    if sum(len(item['text']) for item in result) > 6000:
        raise MemoryCoreError('ai_profile_too_large')
    return result


def stored_preference_entries(value):
    """Require the server-owned dates before a stored preference can be used."""
    rows = parsed_json(value, None)
    if type(rows) is not list or any(type(row) is not dict or set(row) != {'id','text','created_at','updated_at'} for row in rows):
        raise MemoryCoreError('invalid_ai_profile_storage')
    validated = explicit_preference_entries([{'id': row['id'], 'text': row['text']} for row in rows])
    result = []
    for raw, clean in zip(rows, validated):
        created, updated = timestamp(raw['created_at']), timestamp(raw['updated_at'])
        if not created or not updated or created > updated:
            raise MemoryCoreError('invalid_ai_profile_storage')
        result.append({**clean, 'created_at': raw['created_at'], 'updated_at': raw['updated_at']})
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), default=str, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parsed_json(value, fallback=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, (dict, list)) else fallback
    except (ValueError, TypeError):
        return fallback


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min, BEIJING)
    else:
        try:
            raw = str(value)
            if len(raw) == 10:
                result = datetime.combine(date.fromisoformat(raw), time.min, BEIJING)
            else:
                result = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None
    return result if result.tzinfo is not None else None


def day_of(value):
    stamp = timestamp(value)
    return stamp.astimezone(BEIJING).date().isoformat() if stamp else None


def iso(value):
    stamp = timestamp(value)
    return stamp.astimezone(UTC).isoformat() if stamp else None


def checked_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise MemoryCoreError('invalid_memory_date')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MemoryCoreError('invalid_memory_date') from None


def date_ranges(now, earliest='1970-01-01'):
    today = now.astimezone(BEIJING).date()
    return {'timezone': 'Asia/Shanghai', 'as_of': now.astimezone(UTC).isoformat(),
            'recent': {'start': (today - timedelta(days=7)).isoformat(), 'end': today.isoformat()},
            'mid': {'start': (today - timedelta(days=90)).isoformat(), 'end': (today - timedelta(days=8)).isoformat()},
            'remote': {'start': earliest, 'end': (today - timedelta(days=91)).isoformat()}}


def band(day, now):
    age = (now.astimezone(BEIJING).date() - checked_day(day)).days
    return 'planned' if age < 0 else 'recent' if age <= 7 else 'mid' if age <= 90 else 'remote'


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


def _rows(connection, sql, params=()):
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _row_hash(row):
    return fingerprint({k: v for k, v in row.items() if k not in {'user_id', 'member_user_id', 'author_user_id'}})


def _evidence(domain, table, row, *, when='created_at', identifier='id', data=None, statement=None, kind=None):
    source_id = str(row[identifier])
    occurred = iso(row.get(when))
    if occurred is None:
        return None
    if kind is None:
        kind = 'user_confirmed' if row.get('source') == 'ai_confirmed' else 'raw_record'
    original = str(statement if statement is not None else row.get('content') or row.get('title') or row.get('name') or '')
    excerpt = original[:1600]
    result = {'ref': table + ':' + source_id, 'domain': domain,
              'source_table': table, 'source_id': source_id,
              'occurred_at': occurred, 'recorded_at': iso(row.get('created_at')) or occurred,
              'updated_at': iso(row.get('updated_at')) or occurred,
              'kind': kind, 'statement': excerpt, 'data': data or {},
              'source_hash': _row_hash(row)}
    if len(excerpt) < len(original):
        result['excerpted'] = True
    return result


def evidence_reference(item):
    return {key: item[key] for key in ('ref', 'source_table', 'source_id', 'occurred_at', 'kind', 'source_hash')}


class MemberMemoryCore:
    def __init__(self, database, clock=None, *, content_metadata=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))
        # Trusted server catalog metadata only. Never accept a client-provided
        # title/topic, load an arbitrary path, or guess a topic from an opaque ID.
        self.content_metadata = {}
        for key, value in (content_metadata or {}).items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            metadata = {name: value[name][:maximum] for name, maximum in (('title', 300), ('category', 80))
                        if isinstance(value.get(name), str) and value[name].strip()}
            if isinstance(value.get('tags'), list):
                metadata['tags'] = [item[:80] for item in value['tags'][:12] if isinstance(item, str)]
            if metadata.get('title'):
                self.content_metadata[key] = metadata

    def invalidate_goal_memory(self, connection, actor, goal_id, habit_entry_ids):
        """Remove only derived rows carrying explicit references to this goal.

        The caller owns the business transaction. Raising aborts the goal delete
        as well. Chat transcripts and unrelated memory rows remain untouched.
        """
        self._actor(actor)
        if type(goal_id) is not int or goal_id<=0 or any(type(i)is not int or i<=0 for i in habit_entry_ids):
            raise MemoryCoreError('invalid_goal_memory_reference')
        refs={'expansion_goals:'+str(goal_id)}|{'expansion_habit_entries:'+str(i)for i in habit_entry_ids}
        semantic_invalidator = getattr(self, 'multimodal_source_invalidator', None)
        if semantic_invalidator:
            semantic_refs = sorted(refs)
            for offset in range(0, len(semantic_refs), 128):
                semantic_invalidator(actor, semantic_refs[offset:offset + 128], connection=connection)
        legacy_refs={'goal:'+str(goal_id)}|refs
        removed=0
        # The older review flow also has explicit, validated source_ref chains.
        # Revoke those derived records without scanning prose or chat text.
        pending=_rows(connection,'SELECT id,payload_json FROM expansion_memories WHERE user_id=?',(actor,))
        while pending:
            linked_rows=[r for r in pending if parsed_json(r['payload_json'],{}).get('source_ref')in legacy_refs]
            if not linked_rows:break
            identifiers={r['id']for r in linked_rows}
            for identifier in identifiers:
                connection.execute('DELETE FROM expansion_memories WHERE user_id=? AND id=?',(actor,identifier))
                legacy_refs.update(('memory:'+str(identifier),'expansion_memories:'+str(identifier)))
                refs.add('expansion_memories:'+str(identifier));removed+=1
            pending=[r for r in pending if r['id']not in identifiers]
        for row in _rows(connection,'SELECT id,payload_json FROM expansion_analyses WHERE user_id=?',(actor,)):
            payload=parsed_json(row['payload_json'],{})
            sources=payload.get('prepared_source_refs',[])if isinstance(payload,dict)else[]
            if isinstance(sources,list)and any(type(r)is str and r in legacy_refs for r in sources):
                connection.execute('DELETE FROM expansion_analyses WHERE user_id=? AND id=?',(actor,row['id']));removed+=1
        event_sources={'goal':{str(goal_id)},'expansion_goals':{str(goal_id)},
                       'habit':{str(i)for i in habit_entry_ids},'expansion_habit_entries':{str(i)for i in habit_entry_ids}}
        for row in _rows(connection,'SELECT event_id,entity_type,entity_id FROM member_memory_events WHERE user_id=?',(actor,)):
            if row['entity_id']in event_sources.get(row['entity_type'],set()):
                connection.execute('DELETE FROM member_memory_events WHERE user_id=? AND event_id=?',(actor,row['event_id']));removed+=1
        def linked(value):
            rows=parsed_json(value,[])
            return isinstance(rows,list)and any(isinstance(r,dict)and r.get('ref')in refs for r in rows)
        for row in _rows(connection,'SELECT domain,day,category,evidence_json FROM member_memory_daily WHERE user_id=?',(actor,)):
            if linked(row['evidence_json']):
                connection.execute('DELETE FROM member_memory_daily WHERE user_id=? AND domain=? AND day=? AND category=?',
                    (actor,row['domain'],row['day'],row['category']));removed+=1
        for row in _rows(connection,'SELECT preference_key,evidence_json FROM member_memory_preferences WHERE user_id=?',(actor,)):
            if linked(row['evidence_json']):
                connection.execute('DELETE FROM member_memory_preferences WHERE user_id=? AND preference_key=?',(actor,row['preference_key']));removed+=1
        if removed:
            # In-flight summary completion must not resurrect deleted evidence.
            connection.execute("UPDATE member_memory_jobs SET status='stale',revision=revision+1,finished_at=?,last_error_code='goal_source_deleted' WHERE user_id=? AND job_key='preference_summary'",
                (self._now().isoformat(),actor))
        return removed

    def _reading_metadata(self, row):
        key = row.get('content_id') or row.get('entity_id')
        metadata = self.content_metadata.get(key)
        if metadata is not None:
            row['_content_metadata'] = metadata
            return {'available': True, **metadata}
        row['_content_metadata'] = {'available': False}
        return {'available': False, 'topic_is_unknown': True}

    def _now(self):
        current = self.clock()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise MemoryCoreError('invalid_memory_clock')
        return current

    def _actor(self, actor):
        if type(actor) is not int or actor <= 0:
            raise MemoryCoreError('permission_denied')
        scope = getattr(self.database, '_identity', None)
        if scope is not None:
            identity = scope.get()
            if identity.user_id != actor or identity.role_code != 'member':
                raise MemoryCoreError('permission_denied')
        with self.database.connect() as connection:
            row = connection.execute('SELECT role_code,account_status FROM users WHERE id=?', (actor,)).fetchone()
        if row is None or row['role_code'] != 'member' or row['account_status'] != 'active':
            raise MemoryCoreError('permission_denied')
        return actor

    def _controls(self, actor):
        result = {domain: {'enabled_for_ai': True, 'collect_events': True,
                           'forget_before': None, 'revision': 0} for domain in DOMAINS}
        with self.database.connect() as connection:
            rows = _rows(connection, 'SELECT domain,enabled_for_ai,collect_events,forget_before,revision FROM member_memory_controls WHERE user_id=?', (actor,))
        for row in rows:
            if row['domain'] in result:
                result[row['domain']] = {**row, 'enabled_for_ai': bool(row['enabled_for_ai']),
                                        'collect_events': bool(row['collect_events'])}
        return result

    def explicit_preferences(self, actor, *, controls=None):
        self._actor(actor)
        effective = self._controls(actor)['preferences']
        if not effective['enabled_for_ai'] or controls and controls.get('preferences') is False:
            return []
        with self.database.connect() as connection:
            row = connection.execute('SELECT entries_json FROM member_ai_preferences WHERE user_id=?', (actor,)).fetchone()
        if row is None:
            return []
        shadow = getattr(self, 'legacy_preference_shadow', None)
        if shadow and shadow(actor, row):
            return []
        cutoff = timestamp(effective.get('forget_before'))
        return [{**item, 'source': 'user_confirmed', 'priority': 'highest_personal_reference'}
                for item in stored_preference_entries(row['entries_json'])
                if cutoff is None or timestamp(item['updated_at']) > cutoff]

    def _period(self, start_day, end_day, bucket):
        now = self._now()
        ranges = date_ranges(now)
        if bucket not in {'all', 'recent', 'mid', 'remote'}:
            raise MemoryCoreError('invalid_memory_bucket')
        selected = {'start': '1970-01-01', 'end': now.astimezone(BEIJING).date().isoformat()} if bucket == 'all' else ranges[bucket]
        start = checked_day(start_day or selected['start'])
        end = checked_day(end_day or selected['end'])
        if start > end or end > now.astimezone(BEIJING).date():
            raise MemoryCoreError('invalid_memory_range')
        low = datetime.combine(start, time.min, BEIJING).astimezone(UTC).isoformat()
        high = datetime.combine(end + timedelta(days=1), time.min, BEIJING).astimezone(UTC).isoformat()
        return start, end, low, high, ranges

    def collect(self, actor, start_day=None, end_day=None, domains=None, *, bucket='all', limit=500, question='', offset=0,
                _context_budget=None):
        """Read finite owner-scoped pages; coverage always discloses truncation."""
        actor = self._actor(actor)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MemoryCoreError('invalid_memory_limit')
        if type(offset) is not int or not 0 <= offset <= 10000000:
            raise MemoryCoreError('invalid_memory_offset')
        if _context_budget is not None and (type(_context_budget) is not int or not 1<=_context_budget<=2_000_000):
            raise MemoryCoreError('invalid_context_budget')
        requested = tuple(DOMAINS if domains is None else domains)
        if not requested or any(domain not in DOMAINS for domain in requested):
            raise MemoryCoreError('invalid_memory_domain')
        start, end, low, high, ranges = self._period(start_day, end_day, bucket)
        controls = self._controls(actor)
        coverage = {d: {'source_rows': 0, 'returned_rows': 0, 'truncated': False,
                        'start': start.isoformat(), 'end': end.isoformat(),
                        'unrecorded_history_is_unknown': True} for d in requested}
        evidence, profile, current_state = [], {}, {}

        def keep(item):
            if item is None:
                return
            domain = item['domain']
            cutoff = controls[domain].get('forget_before')
            if cutoff and timestamp(item['occurred_at']) < timestamp(cutoff):
                coverage[domain]['forgotten_items_omitted'] = True
                return
            evidence.append(item)
            coverage[domain]['source_rows'] += 1
            coverage[domain]['returned_rows'] += 1

        with self.database.connect() as connection:
            def query(domain, sql, parameters):
                rows = _rows(connection, sql + ' LIMIT ? OFFSET ?', (*parameters, limit + 1, offset))
                if len(rows) > limit:
                    coverage[domain]['truncated'] = True
                return rows[:limit]

            if 'profile' in requested and offset == 0:
                rows = _rows(connection, 'SELECT user_id,' + ','.join(PROFILE_FIELDS) + ',created_at,updated_at FROM member_profiles WHERE user_id=?', (actor,))
                if rows:
                    row = rows[0]
                    cutoff = controls['profile'].get('forget_before')
                    if not cutoff or (timestamp(row['updated_at']) and timestamp(row['updated_at']) > timestamp(cutoff)):
                        profile = {key: row[key] for key in PROFILE_FIELDS if row.get(key) not in (None, '')}
                        profile['updated_at'] = iso(row.get('updated_at'))
                        source = dict(row, id='current')
                        keep(_evidence('profile', 'member_profiles', source, when='updated_at', data=profile,
                                       statement='用户在个人信息页面保存的当前资料', kind='user_confirmed'))

            if 'life_records' in requested:
                for row in query('life_records', 'SELECT id,category,occurred_at,local_date,content,details_json,source,created_at,updated_at FROM life_records WHERE user_id=? AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at DESC,id DESC', (actor, low, high)):
                    if row['category'] not in CATEGORIES:
                        continue
                    details = parsed_json(row.get('details_json'), {})
                    keep(_evidence('life_records', 'life_records', row, when='occurred_at', data={'category': row['category'], 'details': details, 'record_source': row.get('source')}))
                for row in query('life_records', 'SELECT id,kind,measured_at,payload_json,created_at,updated_at FROM expansion_observations WHERE user_id=? AND measured_at>=? AND measured_at<? ORDER BY measured_at DESC,id DESC', (actor, low, high)):
                    details = parsed_json(row.get('payload_json'), {})
                    keep(_evidence('life_records', 'expansion_observations', row, when='measured_at',
                                   data={'category': 'metric', 'metric_kind': row['kind'], 'details': details},
                                   statement='用户记录的身体指标，未由模型推导或补值'))

            if 'reminders' in requested:
                for row in query('reminders', "SELECT id,title,reminder_type,scheduled_at,status,repeat_rule,source,paused_until,created_at,updated_at FROM reminders WHERE user_id=? AND ((created_at>=? AND created_at<?) OR (scheduled_at>=? AND scheduled_at<?) OR status='active') ORDER BY updated_at DESC,id DESC", (actor, low, high, low, high)):
                    keep(_evidence('reminders', 'reminders', row, data={key: row[key] for key in ('reminder_type', 'scheduled_at', 'status', 'repeat_rule', 'paused_until')},
                                   statement=row['title']))
                coverage['reminders']['historical_firing_and_dismissal_unknown'] = True

            if 'goals_habits' in requested:
                goals = query('goals_habits', "SELECT id,kind,status,payload_json,created_at,updated_at FROM expansion_goals WHERE user_id=? AND ((created_at>=? AND created_at<?) OR status='active') ORDER BY updated_at DESC,id DESC", (actor, low, high))
                for row in goals:
                    payload = parsed_json(row.get('payload_json'), {})
                    keep(_evidence('goals_habits', 'expansion_goals', row, data={'kind': row['kind'], 'status': row['status'], **{k: payload[k] for k in ('title', 'target', 'unit', 'frequency', 'reminder_time') if k in payload}}, statement=payload.get('title', '目标设置')))
                for row in query('goals_habits', 'SELECT id,goal_id,day,note,created_at FROM expansion_habit_entries WHERE user_id=? AND day>=? AND day<=? ORDER BY day DESC,id DESC', (actor, start.isoformat(), end.isoformat())):
                    keep(_evidence('goals_habits', 'expansion_habit_entries', row, when='day', data={'goal_id': row['goal_id'], 'day': row['day']}, statement=row['note']))
                coverage['goals_habits']['past_target_versions_unknown'] = True

            if 'services_appointments' in requested:
                for row in query('services_appointments', 'SELECT t.id,t.title,t.scheduled_at,t.status,t.notes,t.created_at,t.updated_at,d.service_type,d.outcome_status FROM visit_tasks t LEFT JOIN visit_task_details d ON d.task_id=t.id WHERE t.member_user_id=? AND ((t.created_at>=? AND t.created_at<?) OR (t.scheduled_at>=? AND t.scheduled_at<?)) ORDER BY t.created_at DESC,t.id DESC', (actor, low, high, low, high)):
                    keep(_evidence('services_appointments', 'visit_tasks', row, data={k: row[k] for k in ('scheduled_at', 'status', 'service_type', 'outcome_status')}, statement=row['title']))
                for row in query('services_appointments', 'SELECT id,task_id,visited_at,summary,created_at,updated_at FROM visit_records WHERE member_user_id=? AND visited_at>=? AND visited_at<? ORDER BY visited_at DESC,id DESC', (actor, low, high)):
                    keep(_evidence('services_appointments', 'visit_records', row, when='visited_at', data={'task_id': row['task_id']}, statement=row['summary']))

            if 'reading' in requested:
                for row in query('reading', 'SELECT content_id,progress,saved,completed_at,updated_at FROM expansion_content_progress WHERE user_id=? AND ((updated_at>=? AND updated_at<?) OR (completed_at>=? AND completed_at<?)) ORDER BY updated_at DESC,content_id', (actor, low, high, low, high)):
                    metadata = self._reading_metadata(row)
                    keep(_evidence('reading', 'expansion_content_progress', row, identifier='content_id', when='updated_at', data={**{k: row[k] for k in ('content_id', 'progress', 'saved', 'completed_at')}, 'content_metadata': metadata}, statement='用户阅读进度的当前快照；不等于完整阅读会话历史'))
                coverage['reading']['historical_session_count_and_dwell_unknown'] = True

            if 'shopping' in requested:
                for row in query('shopping', 'SELECT id,product_id,product_name_snapshot,quantity,total_amount_cents,currency,status,delivered_at,created_at,updated_at FROM orders WHERE member_user_id=? AND ((created_at>=? AND created_at<?) OR (delivered_at>=? AND delivered_at<?)) ORDER BY created_at DESC,id DESC', (actor, low, high, low, high)):
                    keep(_evidence('shopping', 'orders', row, data={k: row[k] for k in ('product_id', 'quantity', 'total_amount_cents', 'currency', 'status', 'delivered_at')}, statement=row['product_name_snapshot']))
                counts = {}
                for name in ('member_cart', 'member_favorites'):
                    counts[name] = connection.execute('SELECT COUNT(*) AS n FROM ' + name + ' WHERE user_id=?', (actor,)).fetchone()['n']
                current_state['shopping'] = {'as_of': self._now().isoformat(), **counts,
                                             'counts_are_current_product_rows_not_quantity': True,
                                             'historical_cart_and_favorite_dates': 'unknown'}

            if 'forum' in requested:
                for table, owner in (('forum_posts', 'author_user_id'), ('forum_comments', 'author_user_id')):
                    columns = 'id,topic_id,content,status,created_at,updated_at' if table == 'forum_posts' else 'id,post_id,content,created_at'
                    for row in query('forum', 'SELECT ' + columns + ' FROM ' + table + ' WHERE ' + owner + '=? AND created_at>=? AND created_at<? ORDER BY created_at DESC,id DESC', (actor, low, high)):
                        keep(_evidence('forum', table, row, data={k: row[k] for k in ('post_id', 'topic_id', 'status') if k in row}))
                for row in query('forum', 'SELECT post_id,created_at FROM forum_likes WHERE user_id=? AND created_at>=? AND created_at<? ORDER BY created_at DESC,post_id', (actor, low, high)):
                    keep(_evidence('forum', 'forum_likes', row, identifier='post_id', data={'post_id': row['post_id']}, statement='用户仍保留的点赞；不包含已取消点赞历史'))
                coverage['forum']['unrecorded_post_views_and_unlikes_unknown'] = True

            if 'ai_records' in requested:
                words = [word for word in ('过敏', '喜欢', '不喜欢', '早餐', '午餐', '晚餐', '睡眠', '喝水', '血糖', '血压', '体重', '药', '预约', '生日', '名字') if word in question][:8]
                filter_sql = ''
                word_params = []
                if words:
                    filter_sql = ' AND (' + ' OR '.join('m.content LIKE ?' for _ in words) + ')'
                    word_params = ['%' + word + '%' for word in words]
                # User statements retain their original fact/source hashes.
                # Assistant output is never a user fact; read its display excerpt
                # in SQL so many long answers cannot inflate a memory snapshot.
                chat_columns = "m.id,m.conversation_id,m.sequence_no,m.role,CASE WHEN m.role='user' THEN m.content ELSE substr(m.content,1,1600) END AS content,m.status,m.created_at,m.updated_at,c.title"
                # Full daily scans retain exact original hashes and counts,
                # but never fetch a 500-row batch of multi-megabyte bodies.
                query_columns = 'm.id,m.role,length(m.content) AS content_chars'
                used_body_chars = body_reads = 0
                for row in query('ai_records', 'SELECT ' + query_columns + " FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.created_at>=? AND m.created_at<? AND (m.provider IS NULL OR m.provider<>'fawnsee-welcome')" + filter_sql + ' ORDER BY m.created_at DESC,m.id DESC', (actor, low, high, *word_params)):
                    if _context_budget is not None:
                        cost = int(row['content_chars']) if row['role']=='user' else min(1600,int(row['content_chars']))
                        if body_reads>=32 or used_body_chars+cost>_context_budget:
                            coverage['ai_records']['truncated']=True
                            coverage['ai_records']['context_body_budget_limited']=True
                            continue
                    # The original user row is read intact, one at a time.
                    # Its canonical source hash is never an excerpt hash.
                    found = connection.execute('SELECT ' + chat_columns +
                        ' FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.id=?',
                        (actor,row['id'])).fetchone()
                    if found is None:
                        continue
                    row=dict(found)
                    used_body_chars+=len(row['content'] or '')
                    body_reads+=1
                    if row['role'] not in {'user', 'assistant'}:
                        continue
                    item = _evidence('ai_records', 'messages', row, data={k: row[k] for k in ('conversation_id', 'sequence_no', 'status', 'title')}, kind='raw_record' if row['role'] == 'user' else 'model_inferred')
                    if item:
                        item['speaker'] = row['role']
                        item['not_user_fact'] = row['role'] == 'assistant'
                        if row['role'] == 'assistant' and len(row['content'] or '') >= 1600:
                            item['excerpted'] = True
                    keep(item)
                for row in query('ai_records', 'SELECT a.id,a.message_id,a.original_name,a.media_type,a.sha256,a.extraction_method,substr(a.extracted_text,1,1600) AS extracted_excerpt,a.created_at FROM chat_attachments a JOIN messages m ON m.id=a.message_id JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND a.created_at>=? AND a.created_at<? ORDER BY a.created_at DESC,a.id DESC', (actor, low, high)):
                    keep(_evidence('ai_records', 'chat_attachments', row, data={k: row[k] for k in ('message_id', 'original_name', 'media_type', 'sha256', 'extraction_method')}, statement=row['extracted_excerpt'] or '本人的对话附件引用；未读取二进制或自动确认内容'))
                coverage['ai_records']['archive'] = 'all original conversations, messages and attachments retained in source tables'
                coverage['ai_records']['retrieval_keywords'] = words

            event_domains = [d for d in ('reading', 'forum', 'operations') if d in requested]
            for domain in event_domains:
                for row in query(domain, 'SELECT event_id,event_type,occurred_at,received_at,entity_type,entity_id,payload_json FROM member_memory_events WHERE user_id=? AND domain=? AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at DESC,event_id', (actor, domain, low, high)):
                    payload = parsed_json(row.get('payload_json'), {})
                    row['created_at'] = row['received_at']
                    metadata = {'content_metadata': self._reading_metadata(row)} if domain == 'reading' else {}
                    keep(_evidence(domain, 'member_memory_events', row, identifier='event_id', when='occurred_at',
                                   data={'action': row['event_type'], 'resource_type': row['entity_type'], 'resource_id': row['entity_id'], 'duration_ms': payload.get('duration_ms'), **metadata},
                                   statement='应用实际记录的操作事件；不代表操作必然成功'))
                coverage[domain]['event_collection_start'] = connection.execute('SELECT MIN(occurred_at) AS earliest FROM member_memory_events WHERE user_id=? AND domain=?', (actor, domain)).fetchone()['earliest']

            preferences = []
            if 'preferences' in requested and offset == 0:
                preferences = _rows(connection, "SELECT preference_key,statement,status,confidence,evidence_json,support_start,support_end,expires_at,model,source_digest,revision,created_at,updated_at FROM member_memory_preferences WHERE user_id=? AND status IN ('inferred','confirmed') ORDER BY updated_at DESC,preference_key LIMIT ?", (actor, limit))
                for row in preferences:
                    row['evidence'] = parsed_json(row.pop('evidence_json'), [])
                coverage['preferences']['source_rows'] = len(preferences)

        evidence.sort(key=lambda item: (item['occurred_at'], item['ref']), reverse=True)
        references = sorted((item['ref'], item['source_hash']) for item in evidence)
        digest = fingerprint({'references': references, 'controls': controls, 'start': start.isoformat(), 'end': end.isoformat()})
        return {'schema': 'member-memory-v1', 'member_user_id': actor,
                'generated_at': self._now().isoformat(), 'date_ranges': ranges,
                'selected_range': {'start': start.isoformat(), 'end': end.isoformat(), 'bucket': bucket},
                'profile': profile, 'evidence': evidence, 'preferences': preferences,
                'current_state': current_state, 'coverage': coverage,
                'controls': controls, 'source_digest': digest}

    def build_daily(self, actor, snapshot, event_rows=None, *, _groups=None, _finalize=True):
        """One daily logical database per domain, and seven per-category life rows."""
        if snapshot.get('member_user_id') != actor:
            raise MemoryCoreError('permission_denied')
        groups = {} if _groups is None else _groups
        def group(domain, day, category='all'):
            key = domain, day, category
            if key not in groups:
                groups[key] = {'domain': domain, 'day': day, 'category': category,
                               'metrics': {'source_record_count': 0}, 'evidence': [],
                               'coverage': {'unrecorded_activity_is_unknown': True,
                                            'source_page_truncated': snapshot['coverage'].get(domain, {}).get('truncated', False)},
                               '_metric_values': {}, '_seen_refs': set(), '_reference_hash': hashlib.sha256()}
            return groups[key]
        def touch(result, item):
            reference = evidence_reference(item)
            key = reference['ref']
            if key in result['_seen_refs']:
                return
            result['_seen_refs'].add(key)
            result['_reference_hash'].update(canonical(reference).encode())
            result['_reference_hash'].update(b'\n')
            result['metrics']['source_record_count'] += 1
            if len(result['evidence']) < 100:
                result['evidence'].append(reference)
            else:
                result['coverage']['evidence_refs_omitted'] = result['metrics']['source_record_count'] - 100
        start, end = (snapshot['selected_range'][k] for k in ('start', 'end'))
        for item in snapshot['evidence']:
            if item['domain'] in {'profile', 'preferences'}:
                continue
            data, table = item['data'], item['source_table']
            for field, metric in (('scheduled_at', 'next_scheduled_items'),
                                  ('completed_at', 'first_read_completions'),
                                  ('delivered_at', 'recorded_deliveries')):
                target_day = day_of(data.get(field))
                if target_day and start <= target_day <= end:
                    target = group(item['domain'], target_day)
                    touch(target, item)
                    target['metrics'][metric] = target['metrics'].get(metric, 0) + 1
                    target['coverage']['schedule_is_not_proof_of_execution'] = field == 'scheduled_at'
            day = day_of(item['occurred_at'])
            if day is None or not start <= day <= end:
                continue
            category = item['data'].get('category', 'all') if item['domain'] == 'life_records' else 'all'
            result = group(item['domain'], day, category)
            metrics = result['metrics']
            touch(result, item)
            def count(key, amount=1):
                metrics[key] = metrics.get(key, 0) + amount
            def add(key, value):
                value = number(value)
                if value is not None:
                    metrics[key] = round(metrics.get(key, 0) + value, 6)
            if item['domain'] == 'life_records':
                details = data.get('details') or {}
                count('record_count')
                if category == 'water': add('water_ml', details.get('amount_ml'))
                if category == 'activity':
                    add('activity_minutes', details.get('duration_minutes')); add('steps', details.get('steps'))
                if category == 'sleep': add('sleep_hours', details.get('duration_hours'))
                if category == 'diet': add('estimated_energy_kcal', details.get('calories_kcal'))
                if category == 'metric':
                    value = number(details.get('value'))
                    if value is not None:
                        key = (data.get('metric_kind', 'custom'), str(details.get('unit') or 'unspecified'))
                        stats = result['_metric_values'].setdefault(key, {'count': 0, 'sum': 0., 'min': value, 'max': value})
                        stats['count'] += 1; stats['sum'] += value
                        stats['min'] = min(stats['min'], value); stats['max'] = max(stats['max'], value)
                metrics['missing_values_not_filled'] = True
            elif table == 'reminders':
                count('created_records'); count('current_status_' + str(data.get('status', 'unknown')))
                metrics['historical_fired_count'] = None
                metrics['status_snapshot_as_of'] = snapshot['generated_at']
            elif table == 'expansion_goals':
                count('created_goals'); count('current_status_' + str(data.get('status', 'unknown')))
                metrics['status_snapshot_as_of'] = snapshot['generated_at']
            elif table == 'expansion_habit_entries':
                count('checkin_count')
            elif table == 'visit_tasks':
                count('appointments_created'); count('current_status_' + str(data.get('status', 'unknown')))
                metrics['status_snapshot_as_of'] = snapshot['generated_at']
            elif table == 'visit_records': count('service_visit_records')
            elif table == 'expansion_content_progress':
                count('progress_snapshots'); count('current_saved_count', int(bool(data.get('saved'))))
                metrics['historical_read_session_count'] = None
            elif table == 'orders':
                count('orders_created'); add('item_quantity', data.get('quantity'))
                count('current_status_' + str(data.get('status', 'unknown')))
                currency = str(data.get('currency') or 'unspecified')
                add('order_value_cents_' + currency, data.get('total_amount_cents'))
                metrics['order_value_is_not_confirmed_payment'] = True
                metrics['status_snapshot_as_of'] = snapshot['generated_at']
            elif table == 'forum_posts': count('posts_created')
            elif table == 'forum_comments': count('comments_created')
            elif table == 'forum_likes': count('retained_likes_created')
            elif table == 'messages':
                count('user_messages' if item.get('speaker') == 'user' else 'assistant_messages')
                count('message_characters', len(item['statement']))
                if item.get('excerpted'): metrics['character_count_is_lower_bound'] = True
            elif table == 'chat_attachments': count('attachment_count')
            elif table == 'member_memory_events':
                action = data.get('action', 'unknown')
                if action in {'page_view', 'button_click', 'content_view', 'post_view'}:
                    count(action + '_count')
                duration = number(data.get('duration_ms'))
                if action in {'page_view', 'content_view', 'post_view'} and duration is not None:
                    add('observed_visible_duration_ms', duration)
                metrics['button_click_is_attempt_not_success'] = True
        if not _finalize:
            return groups
        rows = []
        for result in groups.values():
            values = result.pop('_metric_values')
            if values:
                result['metrics']['measurements'] = [{'kind': kind, 'unit': unit, 'count': stats['count'],
                    'min': stats['min'], 'max': stats['max'], 'mean': round(stats['sum'] / stats['count'], 6)}
                    for (kind, unit), stats in sorted(values.items())]
            result['band'] = band(result['day'], self._now())
            result['source_digest'] = result.pop('_reference_hash').hexdigest()
            result.pop('_seen_refs')
            rows.append(result)
        return sorted(rows, key=lambda row: (row['day'], row['domain'], row['category']), reverse=True)

    def days(self, actor, start_day=None, end_day=None, domains=None, *, bucket='recent', persist=True):
        # Source writes, consent changes and daily projection must share the
        # platform's serialized write transaction. Otherwise a completed forget
        # can race a projection and resurrect its old references.
        scope = self.database.transaction() if persist else nullcontext()
        with scope:
            return self._days(actor, start_day, end_day, domains, bucket=bucket, persist=persist)

    def _days(self, actor, start_day=None, end_day=None, domains=None, *, bucket='recent', persist=True):
        start, end, _, _, _ = self._period(start_day, end_day, bucket)
        if (end - start).days > 365:
            raise MemoryCoreError('memory_daily_range_too_large')
        offset, groups, snapshot, totals = 0, {}, None, None
        all_references = hashlib.sha256()
        while True:
            page = self.collect(actor, start.isoformat(), end.isoformat(), domains, bucket=bucket, offset=offset)
            if snapshot is None:
                snapshot = page
                totals = {domain: dict(value) for domain, value in page['coverage'].items()}
                all_references.update(canonical({'selected_range': page['selected_range'],
                    'controls': page['controls']}).encode())
                all_references.update(b'\n')
            else:
                for domain, value in page['coverage'].items():
                    totals[domain]['source_rows'] += value['source_rows']
                    totals[domain]['returned_rows'] += value['returned_rows']
            for item in page['evidence']:
                all_references.update(canonical(evidence_reference(item)).encode())
                all_references.update(b'\n')
            groups = self.build_daily(actor, page, _groups=groups, _finalize=False)
            if not any(value['truncated'] for value in page['coverage'].values()):
                break
            offset += 500
        snapshot['evidence'] = []
        days = self.build_daily(actor, snapshot, _groups=groups)
        for value in totals.values():
            value['truncated'] = False
            value['statistics_complete'] = True
        for row in days:
            row['coverage']['source_page_truncated'] = False
            row['coverage']['statistics_complete'] = True
        snapshot['coverage'] = totals
        snapshot['source_digest'] = all_references.hexdigest()
        if persist:
            with self.database.transaction() as connection:
                for row in days:
                    connection.execute('INSERT INTO member_memory_daily(user_id,domain,day,category,metrics_json,evidence_json,source_digest,coverage_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,domain,day,category) DO UPDATE SET metrics_json=excluded.metrics_json,evidence_json=excluded.evidence_json,source_digest=excluded.source_digest,coverage_json=excluded.coverage_json,updated_at=excluded.updated_at',
                        (actor, row['domain'], row['day'], row['category'], canonical(row['metrics']), canonical(row['evidence']), row['source_digest'], canonical(row['coverage']), self._now().isoformat()))
                # A source edit/delete must not leave a previous daily statistic alive.
                existing = _rows(connection, 'SELECT domain,day,category FROM member_memory_daily WHERE user_id=? AND day>=? AND day<=?', (actor, snapshot['selected_range']['start'], snapshot['selected_range']['end']))
                retained = {(row['domain'], row['day'], row['category']) for row in days}
                for row in existing:
                    key = row['domain'], row['day'], row['category']
                    if row['domain'] in snapshot['coverage'] and key not in retained and not snapshot['coverage'][row['domain']]['truncated']:
                        connection.execute('DELETE FROM member_memory_daily WHERE user_id=? AND domain=? AND day=? AND category=?', (actor, *key))
        return {'member_user_id': actor, 'generated_at': snapshot['generated_at'],
                'date_ranges': snapshot['date_ranges'], 'selected_range': snapshot['selected_range'],
                'days': days, 'coverage': snapshot['coverage'], 'source_digest': snapshot['source_digest']}

    def refresh_day(self, actor, day=None, domains=None):
        """Refresh one owner/day after a committed business/event mutation.

        The caller may coalesce pending domains and call this sequentially for
        a small set of days. Historical backfills use this same bounded seam;
        they never require a transaction locking an entire member archive.
        """
        selected = checked_day(day) if day is not None else self._now().astimezone(BEIJING).date()
        return self.days(actor, selected.isoformat(), selected.isoformat(), domains)

    def overview(self, actor, *, bucket='all'):
        snapshot = self.collect(actor, bucket=bucket, limit=100)
        return {'member_user_id': actor, 'generated_at': snapshot['generated_at'],
                'date_ranges': snapshot['date_ranges'], 'coverage': snapshot['coverage'],
                'current_state': snapshot['current_state'], 'controls': snapshot['controls'],
                'domain_counts': dict(Counter(item['domain'] for item in snapshot['evidence'])),
                'source_digest': snapshot['source_digest']}

    def _question_range(self, question, bucket):
        now = self._now().astimezone(BEIJING).date()
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', question)
        if dates:
            return checked_day(dates[0]).isoformat(), checked_day(dates[-1]).isoformat(), bucket
        if '昨天' in question:
            day = (now - timedelta(days=1)).isoformat()
            return day, day, bucket
        if '今天' in question:
            return now.isoformat(), now.isoformat(), bucket
        if any(word in question for word in ('本月', '这个月', '当月')):
            return now.replace(day=1).isoformat(), now.isoformat(), bucket
        recent = re.search(r'(?:最近|近|过去)(\d{1,3}|七|三十|九十|一年|一周|一个月)(?:天|日)?', question)
        if recent:
            count = {'七': 7, '三十': 30, '九十': 90, '一年': 366,
                     '一周': 7, '一个月': 30}.get(recent[1])
            if count is None:
                count = int(recent[1])
            if 1 <= count <= 999:
                return (now - timedelta(days=count - 1)).isoformat(), now.isoformat(), bucket
        if '上个月' in question:
            end = now.replace(day=1) - timedelta(days=1)
            return end.replace(day=1).isoformat(), end.isoformat(), bucket
        if bucket == 'all' and any(word in question for word in ('远期', '很久以前', '三个月前')):
            bucket = 'remote'
        return None, None, bucket

    def _retrieval_question_range(self, question, bucket):
        """Select past sources for an explicit future plan, never future facts.

        This is only the initial semantic-retrieval policy. Historical tools
        keep _question_range/_period strict. Preserve any requested past range
        and all controls/forget cutoffs in the ordinary collection pipeline.
        """
        now = self._now().astimezone(BEIJING).date()
        dates = [checked_day(value) for value in re.findall(r'\d{4}-\d{2}-\d{2}', question)]
        planning = any(word in question for word in ('安排', '计划', '规划', '建议', '推荐'))
        if not planning or not any(day > now for day in dates):
            return self._question_range(question, bucket)
        past = [day.isoformat() for day in dates if day <= now]
        if past:
            return past[0], past[-1], bucket
        # Removing only ISO dates retains an explicit "最近七天"/"昨天"
        # source window, if one was requested alongside the future plan.
        return self._question_range(re.sub(r'\d{4}-\d{2}-\d{2}', '', question), bucket)

    def resolve_references(self, actor, references):
        """Revalidate only bounded original source references, even outside the
        current question's date window. Never trust a stored model's source hash.
        """
        actor = self._actor(actor)
        if not isinstance(references, list) or len(references) > 128:
            raise MemoryCoreError('invalid_memory_references')
        controls = self._controls(actor)
        sources = {
            'life_records': ('life_records', 'id,category,occurred_at,local_date,content,details_json,source,created_at,updated_at', 'user_id', 'id', 'occurred_at'),
            'expansion_meals': ('life_records', 'id,user_id,life_record_id,occurred_at,meal_type,payload_json,created_at,updated_at', 'user_id', 'id', 'occurred_at'),
            'medical_reports': ('life_records', 'id,user_id,file_id,report_type,report_date,institution,summary,status,created_at,updated_at', 'user_id', 'id', 'created_at'),
            'expansion_observations': ('life_records', 'id,kind,measured_at,payload_json,created_at,updated_at', 'user_id', 'id', 'measured_at'),
            'reminders': ('reminders', 'id,title,reminder_type,scheduled_at,status,repeat_rule,source,paused_until,created_at,updated_at', 'user_id', 'id', 'created_at'),
            'expansion_goals': ('goals_habits', 'id,kind,status,payload_json,created_at,updated_at', 'user_id', 'id', 'created_at'),
            'expansion_habit_entries': ('goals_habits', 'id,goal_id,day,note,created_at', 'user_id', 'id', 'day'),
            'visit_records': ('services_appointments', 'id,task_id,visited_at,summary,created_at,updated_at', 'member_user_id', 'id', 'visited_at'),
            'expansion_content_progress': ('reading', 'content_id,progress,saved,completed_at,updated_at', 'user_id', 'content_id', 'updated_at'),
            'orders': ('shopping', 'id,product_id,product_name_snapshot,quantity,total_amount_cents,currency,status,delivered_at,created_at,updated_at', 'member_user_id', 'id', 'created_at'),
            'forum_posts': ('forum', 'id,topic_id,content,status,created_at,updated_at', 'author_user_id', 'id', 'created_at'),
            'forum_comments': ('forum', 'id,post_id,content,created_at', 'author_user_id', 'id', 'created_at'),
            'forum_likes': ('forum', 'post_id,created_at', 'user_id', 'post_id', 'created_at'),
            'member_memory_events': (None, 'domain,event_id,event_type,occurred_at,received_at,entity_type,entity_id,payload_json', 'user_id', 'event_id', 'occurred_at'),
        }
        resolved = {}
        with self.database.connect() as connection:
            for ref in references:
                if not isinstance(ref, dict) or not isinstance(ref.get('ref'), str):
                    continue
                table, separator, source_id = ref['ref'].partition(':')
                if not separator or len(source_id) > 200:
                    continue
                domain, when, row = None, None, None
                if table in sources:
                    domain, columns, owner, key, when = sources[table]
                    bound = source_id
                    if key in {'id', 'post_id'}:
                        if not source_id.isdigit() or int(source_id) <= 0:
                            continue
                        bound = int(source_id)
                    found = connection.execute('SELECT ' + columns + ' FROM ' + table + ' WHERE ' + owner + '=? AND ' + key + '=?', (actor, bound)).fetchone()
                    row = dict(found) if found is not None else None
                    if table == 'expansion_meals' and row is not None:
                        parent = connection.execute('SELECT id,details_json FROM life_records WHERE id=? AND user_id=?',
                            (row['life_record_id'], actor)).fetchone()
                        if parent is None or 'photo_file_ids' in parsed_json(parent['details_json'], {}):
                            row = None
                    if table == 'medical_reports' and row is not None:
                        if row['status'] != 'confirmed' or type(row['file_id']) is not int or row['file_id'] <= 0:
                            row = None
                        else:
                            when = 'report_date' if row['report_date'] is not None else 'created_at'
                    if table == 'member_memory_events' and row is not None:
                        domain = row.pop('domain')
                        row['created_at'] = row['received_at']
                elif table == 'member_profiles' and source_id == 'current':
                    found = connection.execute('SELECT user_id,' + ','.join(PROFILE_FIELDS) + ',created_at,updated_at FROM member_profiles WHERE user_id=?', (actor,)).fetchone()
                    row = dict(found, id='current') if found is not None else None
                    domain, when = 'profile', 'updated_at'
                elif table == 'messages' and source_id.isdigit():
                    found = connection.execute("SELECT m.id,m.conversation_id,m.sequence_no,m.role,m.content,m.status,m.created_at,m.updated_at,c.title FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.id=? AND m.role='user'", (actor, int(source_id))).fetchone()
                    row = dict(found) if found is not None else None
                    domain, when = 'ai_records', 'created_at'
                elif table == 'chat_attachments' and source_id.isdigit():
                    found = connection.execute('SELECT a.id,a.message_id,a.original_name,a.media_type,a.sha256,a.extraction_method,substr(a.extracted_text,1,1600) AS extracted_excerpt,a.created_at FROM chat_attachments a JOIN messages m ON m.id=a.message_id JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND a.id=?', (actor, int(source_id))).fetchone()
                    row = dict(found) if found is not None else None
                    domain, when = 'ai_records', 'created_at'
                elif table == 'visit_tasks' and source_id.isdigit():
                    found = connection.execute('SELECT t.id,t.title,t.scheduled_at,t.status,t.notes,t.created_at,t.updated_at,d.service_type,d.outcome_status FROM visit_tasks t LEFT JOIN visit_task_details d ON d.task_id=t.id WHERE t.member_user_id=? AND t.id=?', (actor, int(source_id))).fetchone()
                    row = dict(found) if found is not None else None
                    domain, when = 'services_appointments', 'created_at'
                if row is None or domain not in controls or not controls[domain]['enabled_for_ai']:
                    continue
                if domain == 'reading':
                    self._reading_metadata(row)
                occurred = timestamp(row.get(when))
                cutoff = timestamp(controls[domain].get('forget_before'))
                if occurred is None or (cutoff and occurred < cutoff) or (table == 'messages' and row.get('role') != 'user'):
                    continue
                resolved[ref['ref']] = {'ref': ref['ref'], 'domain': domain,
                    'source_table': table, 'source_id': source_id,
                    'source_hash': _row_hash(row), 'occurred_at': occurred.isoformat(),
                    'kind': 'user_confirmed' if table == 'member_profiles' or row.get('source') == 'ai_confirmed' else 'raw_record'}
        return resolved

    def preference_support_status(self, actor, rows, *, controls=None, known_evidence=None):
        """Effective statuses without rewriting user feedback or old evidence.

        Rejected/revoked/stale choices retain their status. Active inferred and
        confirmed claims become stale as soon as consent, expiry, a source edit
        or deletion makes their dated references unusable. A displayed user
        confirmation cannot make deleted or forgotten source facts reappear.
        """
        actor = self._actor(actor)
        effective = self._controls(actor)
        if controls:
            for domain in DOMAINS:
                if controls.get(domain) is False:
                    effective[domain]['enabled_for_ai'] = False
        known = dict(known_evidence or {})
        active_rows, missing = [], {}
        result = {}
        for row in rows:
            key, status = row.get('preference_key'), row.get('status')
            if not isinstance(key, str):
                continue
            result[key] = status
            if status not in {'inferred', 'confirmed'}:
                continue
            expiry = timestamp(row.get('expires_at'))
            cutoff = timestamp(effective['preferences'].get('forget_before'))
            updated = timestamp(row.get('updated_at'))
            refs = row.get('evidence', parsed_json(row.get('evidence_json'), []))
            if (not effective['preferences']['enabled_for_ai'] or
                    (expiry and expiry <= self._now()) or
                    (cutoff and (updated is None or updated < cutoff)) or
                    not isinstance(refs, list) or not 1 <= len(refs) <= 128):
                result[key] = 'stale'
                continue
            active_rows.append((key, refs))
            for ref in refs:
                if isinstance(ref, dict) and isinstance(ref.get('ref'), str) and ref['ref'] not in known:
                    missing[ref['ref']] = ref
        references = list(missing.values())
        for offset in range(0, len(references), 128):
            known.update(self.resolve_references(actor, references[offset:offset + 128]))
        for key, refs in active_rows:
            for ref in refs:
                current = known.get(ref.get('ref')) if isinstance(ref, dict) else None
                if (current is None or current.get('domain') not in effective or
                        not effective[current['domain']]['enabled_for_ai'] or
                        current.get('speaker') == 'assistant' or
                        current.get('kind') == 'model_inferred' or
                        current.get('source_hash') != ref.get('source_hash')):
                    result[key] = 'stale'
                    break
        return result

    def context(self, actor, question, *, bucket='all', max_chars=18000, controls=None, current_state=False,
                initial_retrieval=False):
        if (not isinstance(question, str) or not 1 <= len(question) <= 4000 or type(max_chars) is not int
                or not 4000 <= max_chars <= 24000 or type(current_state) is not bool
                or type(initial_retrieval) is not bool or current_state and initial_retrieval):
            raise MemoryCoreError('invalid_memory_context')
        selected = {domain for domain, words in DOMAIN_WORDS.items() if any(word in question for word in words)}
        selected.update(('profile', 'preferences'))
        if not selected - {'profile', 'preferences'}:
            selected.update(('life_records', 'ai_records'))
        # Only server-owned current-profile/preference tools enable this mode.
        # Preserve the original question for field/semantic selection, while a
        # future plan date cannot become a historical source-query interval.
        start, end, bucket = ((None,None,'all') if current_state else
            self._retrieval_question_range(question,bucket) if initial_retrieval else self._question_range(question,bucket))
        # Read enabled support internally across all categories so a long-lived
        # preference is not declared unsupported just because this question
        # selected a different display category. Only selected categories enter
        # the model's facts below, and disabled support still invalidates a claim.
        snapshot = self.collect(actor, start, end, DOMAINS, bucket=bucket, question=question,
                                _context_budget=2_000_000)
        effective = snapshot['controls']
        if controls:
            for domain in DOMAINS:
                if controls.get(domain) is False:
                    effective[domain]['enabled_for_ai'] = False
        active = {domain for domain in selected if effective[domain]['enabled_for_ai']}
        profile = dict(snapshot['profile']) if 'profile' in active else {}
        if not any(word in question for word in ('电话', '手机号', '联系方式')):
            profile.pop('phone', None)
        if '紧急联系人' not in question:
            profile.pop('emergency_contact_name', None); profile.pop('emergency_contact_phone', None)
        category_reader = getattr(self, 'category_preference_reader', None)
        category_preferences = category_reader(actor, controls=controls, snapshot=snapshot) if category_reader else []
        explicit = self.explicit_preferences(actor, controls=controls)
        semantic=[];semantic_state='disabled'
        retriever=getattr(self,'profile_introduction_retriever',None)
        if 'profile' in active and retriever:
            from .profile_embedding11841 import EmbeddingUnavailable
            try:
                semantic=retriever(actor,question,controls=controls)
                semantic_state='ready' if semantic else 'no_authorized_current_introduction'
            except EmbeddingUnavailable:
                semantic_state='unavailable'
        multimodal = {'references': [], 'coverage': {'state': 'not_configured'}}
        multimodal_retriever = getattr(self, 'multimodal_retriever', None)
        if multimodal_retriever:
            from .member_multimodal11842 import MultimodalUnavailable, StalePersonalSources
            try:
                multimodal = multimodal_retriever(actor, question, snapshot=snapshot,
                    controls=controls, domains=active)
            except MultimodalUnavailable:
                multimodal = {'references': [], 'coverage': {'state': 'unavailable',
                    'no_fabricated_vectors': True, 'complete_history_search_claim_allowed': False}}
            except StalePersonalSources:
                raise MemoryCoreError('personal_context_changed') from None
        result = {'explicit_preferences': explicit, 'personal_introduction_references':semantic, 'category_preferences': category_preferences,
                  'multimodal_references': multimodal['references'],
                  'schema': 'member-memory-v1', 'member_user_id': actor,
                  'generated_at': snapshot['generated_at'], 'date_ranges': snapshot['date_ranges'],
                  'selected_range': snapshot['selected_range'], 'profile': profile,
                  'current_state': {domain: value for domain, value in snapshot['current_state'].items()
                                    if domain in active},
                  'confirmed_facts': [], 'raw_records': [], 'daily_summaries': [],
                  'inferred_preferences': [], 'ai_history': [], 'evidence': [],
                  'capability_coverage': {domain: ({**snapshot['coverage'][domain],
                      'enabled_for_ai': True} if domain in active else
                      {'enabled_for_ai': False, 'source_rows': None, 'returned_rows': None,
                       'unrecorded_history_is_unknown': True}) for domain in selected},
                  'source_digest': fingerprint([snapshot['source_digest'], explicit]), 'omitted_items': 0,
                  'facts_policy': {'confirmed_is_user_confirmation_not_independent_verification': True,
                      'explicit_preferences_have_highest_personal_reference_priority': True,
                      'current_semantic_introduction_has_highest_personal_reference_priority':True,
                      'raw_user_messages_are_self_report': True, 'assistant_output_is_not_user_fact': True,
                      'missing_record_is_not_absence_of_activity': True,
                      'dates_and_ranges_must_be_stated_for_history_claims': True}}
        result['capability_coverage']['personal_introduction']={'state':semantic_state,'semantic_vector_retrieval':True,
            'raw_text_fallback':False,'current_owner_and_version_only':True}
        result['capability_coverage']['multimodal_retrieval'] = multimodal['coverage']
        def append(key, item):
            result[key].append(item)
            if len(canonical(result)) > max_chars:
                result[key].pop(); result['omitted_items'] += 1
                return False
            return True
        preference_states = self.preference_support_status(actor, snapshot['preferences'], controls=controls,
            known_evidence={item['ref']: item for item in snapshot['evidence']})
        for row in snapshot['preferences']:
            if 'preferences' not in active:
                break
            if preference_states.get(row['preference_key']) not in {'inferred', 'confirmed'}:
                continue
            item = {key: row[key] for key in ('preference_key', 'statement', 'confidence', 'support_start', 'support_end', 'revision')}
            item.update({'kind': 'user_confirmed' if row['status'] == 'confirmed' else 'model_inferred',
                         'evidence': row['evidence'], 'never_present_inference_as_fact': row['status'] != 'confirmed'})
            append('confirmed_facts' if row['status'] == 'confirmed' else 'inferred_preferences', item)
        # Exact statistics have their own complete scan. A raw retrieval page is
        # deliberately bounded and therefore cannot be used as a denominator or
        # a daily total. Generic questions refresh only the recent eight days;
        # explicit historical queries cover up to 366 days and say so plainly.
        statistics_domains = sorted(active - {'profile', 'preferences'})
        if statistics_domains:
            requested_start = checked_day(snapshot['selected_range']['start'])
            statistics_end = checked_day(snapshot['selected_range']['end'])
            statistics_start = requested_start
            if start is None and end is None and bucket == 'all':
                statistics_start = max(statistics_start, statistics_end - timedelta(days=7))
            statistics_start = max(statistics_start, statistics_end - timedelta(days=365))
            stats = self.days(actor, statistics_start.isoformat(), statistics_end.isoformat(), statistics_domains)
            result['capability_coverage']['statistics'] = {
                'requested_start': requested_start.isoformat(),
                'requested_end': statistics_end.isoformat(),
                'statistics_start': statistics_start.isoformat(),
                'statistics_end': statistics_end.isoformat(),
                'statistics_range_bounded': statistics_start != requested_start,
                'complete_for_covered_range': True,
                'unrecorded_activity_is_unknown': True,
                'empty_daily_slot_means_no_retained_source_record': True}
            totals = {}
            for row in stats['days']:
                key = row['domain'], row['category']
                aggregate = totals.setdefault(key, {'domain': row['domain'], 'category': row['category'],
                    'summary_kind': 'range_daily_metric_totals', 'start': statistics_start.isoformat(),
                    'end': statistics_end.isoformat(), 'metrics': {}, 'recorded_daily_groups': 0,
                    'coverage': {'statistics_complete': True, 'unrecorded_activity_is_unknown': True,
                        'source_record_count_is_sum_of_daily_references': True}})
                aggregate['recorded_daily_groups'] += 1
                for name, value in row['metrics'].items():
                    if number(value) is not None:
                        aggregate['metrics'][name] = round(aggregate['metrics'].get(name, 0) + value, 6)
                    elif value is None or type(value) is bool:
                        aggregate['metrics'][name] = value
            for domain in statistics_domains:
                result['capability_coverage'][domain].update({
                    'statistics_complete': True, 'statistics_start': statistics_start.isoformat(),
                    'statistics_end': statistics_end.isoformat()})
                if not any(key[0] == domain for key in totals):
                    totals[(domain, 'all')] = {'domain': domain, 'category': 'all',
                        'summary_kind': 'range_daily_metric_totals', 'start': statistics_start.isoformat(),
                        'end': statistics_end.isoformat(), 'recorded_daily_groups': 0,
                        'metrics': {'source_record_count': 0},
                        'coverage': {'statistics_complete': True, 'unrecorded_activity_is_unknown': True}}
            for row in totals.values():
                append('daily_summaries', row)
            for row in stats['days']:
                append('daily_summaries', {key: row[key] for key in ('domain', 'day', 'category', 'band', 'metrics', 'coverage')})
        words = [word for words in DOMAIN_WORDS.values() for word in words if word in question]
        def priority(item):
            score = sum(word in item['statement'] or word in canonical(item['data']) for word in words)
            return score, item['occurred_at']
        for item in sorted(snapshot['evidence'], key=priority, reverse=True):
            if item['domain'] not in active or item['domain'] == 'profile':
                continue
            key = 'ai_history' if item['domain'] == 'ai_records' else 'confirmed_facts' if item['kind'] == 'user_confirmed' else 'raw_records'
            if append(key, item):
                append('evidence', evidence_reference(item))
        result['capability_coverage']['retrieval'] = {'budget_chars': max_chars,
            'complete_archive_was_not_sent_to_model': True,
            'full_source_archive_retained': True,
            'selected_items_may_be_incomplete': result['omitted_items'] > 0 or any(v.get('truncated') for v in snapshot['coverage'].values())}
        # Reserve enough room for the final budget metadata without splitting a JSON item.
        while len(canonical(result)) > max_chars:
            for key in ('evidence', 'daily_summaries', 'ai_history', 'raw_records', 'confirmed_facts', 'inferred_preferences', 'multimodal_references'):
                if result[key]:
                    result[key].pop(); result['omitted_items'] += 1; break
            else:
                if result['category_preferences']:
                    minimum={'schema':result['schema'],'member_user_id':actor,'generated_at':result['generated_at'],
                        'explicit_preferences':result['explicit_preferences'],'category_preferences':result['category_preferences'],
                        'personal_introduction_references':result['personal_introduction_references'],
                        'source_digest':result['source_digest'],'omitted_items':result['omitted_items']+1,
                        'memory_budget_incomplete':True,
                        'capability_coverage':{'state':'preference_priority_budget','not_evidence_of_no_history':True,
                            'full_source_archive_retained':True}}
                    if len(canonical(minimum))<=max_chars:
                        return minimum
                raise MemoryCoreError('memory_context_budget_too_small')
        return result

    def preference_life_records(self, actor):
        """A small date/type-diverse sample; frequent water rows cannot hide sleep.

        This is not a complete-history statistic. Existing source rows remain
        authoritative, and per-domain forgetting is applied before returning.
        """
        self._actor(actor)
        control=self._controls(actor)['life_records']
        if not control['enabled_for_ai'] or not control['collect_events']:
            return []
        _,_,low,high,_=self._period(None,None,'all')
        cutoff=timestamp(control.get('forget_before'))
        columns='id,category,occurred_at,local_date,content,details_json,source,created_at,updated_at'
        result=[]
        with self.database.connect() as connection:
            for category in CATEGORIES:
                sql=('SELECT '+columns+' FROM (SELECT '+columns+',ROW_NUMBER() OVER '
                    '(PARTITION BY local_date ORDER BY occurred_at DESC,id DESC) AS memory_position FROM life_records '
                    'WHERE user_id=? AND category=? AND occurred_at>=? AND occurred_at<? AND local_date IN '
                    '(SELECT DISTINCT local_date FROM life_records WHERE user_id=? AND category=? AND occurred_at>=? AND occurred_at<? '
                    'ORDER BY local_date DESC LIMIT 3)) AS sampled WHERE memory_position<=2 ORDER BY occurred_at DESC,id DESC')
                for row in _rows(connection,sql,(actor,category,low,high,actor,category,low,high)):
                    item=_evidence('life_records','life_records',row,when='occurred_at',
                        data={'category':category,'details':parsed_json(row.get('details_json'),{}),'record_source':row.get('source')})
                    if item and (cutoff is None or timestamp(item['occurred_at'])>cutoff):
                        result.append(item)
        return result

    def summarize_inputs(self, actor, *, bucket='all', max_chars=18000):
        """Only evidence candidates; an external bounded job owns paid AI calls."""
        question = '生活 饮食 喝水 睡眠 运动 环境 指标 提醒 目标 习惯 预约 阅读 购物 论坛 操作 聊过 偏好'
        return self.context(actor, question, bucket=bucket, max_chars=max_chars)


__all__ = ['MemberMemoryCore', 'MemoryCoreError', 'DOMAINS', 'CATEGORIES',
           'PROFILE_FIELDS', 'date_ranges', 'fingerprint', 'canonical',
           'evidence_reference', 'explicit_preference_entries', 'stored_preference_entries']
