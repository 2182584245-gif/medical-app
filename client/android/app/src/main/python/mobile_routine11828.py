"""Local-only decisions for the four device-calendar daily reminder checks.

Android owns its time-zone and DST calendar rules and supplies each occurrence's
local-day boundaries. These functions never infer a missing meal or contact a
server, and do not change the legacy custom-reminder Beijing-time calendar.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import re

KINDS = ('breakfast', 'lunch', 'dinner', 'goals')
_FIELDS = frozenset({'id', 'kind', 'day', 'at', 'day_start', 'day_end',
                     'previous_day_start', 'previous_day_end'})
_TIMES = ('at', 'day_start', 'day_end', 'previous_day_start', 'previous_day_end')
_GRACE_MS = 15 * 60 * 1000
_MEALS = {
    'breakfast': 'breakfast', '早餐': 'breakfast', '早饭': 'breakfast',
    'lunch': 'lunch', '午餐': 'lunch', '午饭': 'lunch',
    'dinner': 'dinner', '晚餐': 'dinner', '晚饭': 'dinner',
}
_NOTICES = {
    'breakfast': {'title': '早餐记录提醒', 'body': '记下今天的早餐，也别忘了记录昨日睡眠情况。'},
    'lunch': {'title': '午餐记录提醒', 'body': '午饭吃了什么？拍一张照片或说一说，留下这顿饭的记录。'},
    'dinner': {'title': '晚餐记录提醒', 'body': '记下今天的晚饭，让这一天的饮食记录更完整。'},
    'goals': {'title': '目标与习惯提醒', 'body': '看看今天的目标与习惯，按自己的节奏安排这一天。'},
}


def scope_for(identity):
    """A stable non-secret owner fingerprint, unchanged by process restarts."""
    actor, endpoint, server = identity.actor_id, identity.endpoint, identity.server_instance_id
    if (type(actor) is not int or actor <= 0 or not isinstance(endpoint, str)
            or not endpoint or not isinstance(server, str)):
        raise ValueError('Invalid reminder owner')
    raw = json.dumps([endpoint.rstrip('/'), actor, server], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf8')).hexdigest()


def checked_slot(value):
    if type(value) is not dict or set(value) != _FIELDS:
        raise ValueError('Invalid routine fields')
    kind = value.get('kind')
    if kind not in KINDS or value.get('id') != 'routine11828.' + kind:
        raise ValueError('Invalid routine identity')
    day = value.get('day')
    if not isinstance(day, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) is None:
        raise ValueError('Invalid routine day')
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError('Invalid routine day')
    if any(type(value.get(key)) is not int or not 0 < value[key] < 2**53 for key in _TIMES):
        raise ValueError('Invalid routine instant')
    start, end = value['day_start'], value['day_end']
    if not start <= value['at'] < end or value['previous_day_end'] != start:
        raise ValueError('Invalid routine day boundary')
    # Real local days can gain or lose an hour. Android separately revalidates
    # the full slot against its actual current calendar before posting.
    if not all(20 * 3600000 <= length <= 28 * 3600000 for length in
               (end - start, start - value['previous_day_start'])):
        raise ValueError('Invalid routine day duration')
    return dict(value)


def checked_slots(slots_json='[]'):
    if not isinstance(slots_json, str) or len(slots_json.encode('utf8')) > 8192:
        raise ValueError('Invalid routine candidates')
    values = json.loads(slots_json)
    if type(values) is not list or len(values) > len(KINDS):
        raise ValueError('Invalid routine candidates')
    result = [checked_slot(value) for value in values]
    if len({value['id'] for value in result}) != len(result):
        raise ValueError('Duplicate routine candidate')
    # Even a completed meal keeps its future check: dispatch rechecks local
    # records, consumes that occurrence, and schedules the following local day.
    return result


def _rows(records):
    if isinstance(records, dict):
        records = records.get('items', [])
    return records if isinstance(records, list) else []


def _meal_recorded(records, kind, start, end):
    for row in _rows(records):
        if not isinstance(row, dict) or row.get('category') != 'diet':
            continue
        details = row.get('details')
        if details is None and isinstance(row.get('details_json'), str):
            try:
                details = json.loads(row['details_json'])
            except (ValueError, TypeError):
                continue
        if not isinstance(details, dict):
            continue
        meal = details.get('meal_type') or details.get('meal')
        if not isinstance(meal, str) or _MEALS.get(meal.strip().lower()) != kind:
            continue
        try:
            raw = row.get('occurred_at')
            if not isinstance(raw, str):
                continue
            stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                continue
            at = stamp.timestamp() * 1000
            if start <= at < end:
                return True
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    return False


def notification(records, slot, now_ms):
    candidate = checked_slot(slot)
    if type(now_ms) is not int or not candidate['at'] <= now_ms <= candidate['at'] + _GRACE_MS:
        return None
    # A delayed previous-day breakfast/dinner must not show after midnight.
    if not candidate['day_start'] <= now_ms < candidate['day_end']:
        return None
    kind = candidate['kind']
    if kind != 'goals' and _meal_recorded(records, kind, candidate['day_start'], candidate['day_end']):
        return None
    return dict(_NOTICES[kind])
