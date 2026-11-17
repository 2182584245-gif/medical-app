"""Authenticated cloud memory adapter. No member archive or summaries on device."""
from __future__ import annotations

import re
from uuid import UUID
from ollama_chat_app.services.cloud_client import CloudAPIError

MEMORY_ROUTES = {
    'member.ai-profile.categories.get': ('GET','/ai-profile/categories'),
    'member.ai-profile.categories.save': ('PUT','/ai-profile/categories'),
    'member.ai-profile.categories.refresh': ('POST',None),
    'member.ai-profile.get': ('GET','/ai-profile'),
    'member.ai-profile.save': ('PUT','/ai-profile'),
    'member.memory.settings.get': ('GET','/settings'),
    'member.memory.conversation.status': ('GET','/conversations/status'),
    'member.memory.controls.update': ('PATCH','/controls'),
    'member.memory.forget': ('POST','/forget'),
    'member.memory.events.append': ('POST','/events'),
    'member.memory.overview': ('GET','/overview'),
    'member.memory.days': ('GET','/days'),
    'member.memory.preferences.list': ('GET','/preferences'),
    'member.memory.preferences.summarize': ('POST','/preferences/summarize'),
    'member.memory.preference.feedback': ('POST',None),
}

CATEGORY_DOMAINS = frozenset({'profile','life_records','reminders','goals_habits',
    'services_appointments','reading','shopping','forum','operations','preferences','ai_records'})
CATEGORY_WRITES = frozenset({'member.ai-profile.categories.save','member.ai-profile.categories.refresh'})
CATEGORY_READS = frozenset({'member.ai-profile.categories.get'})


def _category_request(action, payload):
    from ollama_chat_app.web_desktop.bridge import BridgeRequestError
    def invalid():
        raise BridgeRequestError('请重新核对档案类别、文字与保存版本。','validation')
    def revision(value):
        return type(value) is int and 0 <= value < 2**53
    if action in CATEGORY_READS:
        if payload:invalid()
        return '/ai-profile/categories',payload
    request = payload.get('request_id')
    try:
        if not isinstance(request,str) or str(UUID(request)) != request:invalid()
    except (ValueError,TypeError,AttributeError):invalid()
    if action.endswith('.refresh'):
        if set(payload) != {'domain','expected_revision','request_id'} or not isinstance(payload['domain'],str) or payload['domain'] not in CATEGORY_DOMAINS or not revision(payload['expected_revision']):invalid()
        return '/ai-profile/categories/'+payload['domain']+'/refresh', {key:value for key,value in payload.items() if key != 'domain'}
    if set(payload) != {'categories','request_id'} or type(payload['categories']) is not list or not 1 <= len(payload['categories']) <= 11:invalid()
    seen=set();total=0
    for row in payload['categories']:
        if type(row) is not dict or set(row) != {'domain','text','expected_revision'}:invalid()
        domain,text=row.get('domain'),row.get('text')
        if not isinstance(domain,str) or domain not in CATEGORY_DOMAINS or domain in seen or not isinstance(text,str) or not revision(row['expected_revision']):invalid()
        if len(text) > (6400 if domain == 'preferences' else 2000):invalid()
        seen.add(domain);total+=len(text)
    if total>12000:invalid()
    return '/ai-profile/categories',payload


def cloud_memory_request(api, action, body):
    from ollama_chat_app.web_desktop.bridge import BridgeRequestError
    method,path = MEMORY_ROUTES[action]
    payload = dict(body)
    if action in CATEGORY_READS or action in CATEGORY_WRITES:
        path,payload = _category_request(action,payload)
    if action == 'member.memory.preference.feedback':
        key = payload.pop('key',None)
        if not isinstance(key,str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,63}',key):
            raise BridgeRequestError('偏好编号无效。','validation')
        path = '/preferences/' + key + '/feedback'
    if method == 'GET' and action != 'member.memory.days' and payload:
        raise BridgeRequestError('记忆读取参数无效。','validation')
    if action == 'member.memory.days' and set(payload) != {'domain','start_day','end_day'}:
        raise BridgeRequestError('请填写记忆分类和起止日期。','validation')
    return api._client.request(method,'/v1/member/memory'+path,
                               **({'params':payload} if method=='GET' else {'json':payload}))


class CloudMemberMemoryContext:
    def __init__(self, api, question):
        self.api, self.question, self.last_snapshot = api,question,None

    def build_member_context(self, user_id, *, context_days=7):
        from ollama_chat_app.web_desktop.bridge import BridgeRequestError
        if user_id != self.api._actor_id():
            raise BridgeRequestError('个人资料身份校验失败。','permission')
        try:
            result = self.api._client.request('POST','/v1/member/memory/context',
                                              json={'question':self.question,'bucket':'all'})
        except CloudAPIError:
            result = {'member_user_id':user_id,'memory_enabled':False,
                      'capability_coverage':{'state':'unavailable','not_evidence_of_no_history':True}}
        if not isinstance(result,dict) or result.get('member_user_id') != user_id:
            raise BridgeRequestError('个人资料身份校验失败。','permission')
        self.last_snapshot = result
        return result

    def permits_conversation_history(self):
        value = self.last_snapshot or {}
        if value.get('memory_enabled') is False:
            return False
        # The dated cloud archive handles old turns after a forget operation.
        # The legacy ChatTurn window has no dates, so it cannot bypass the cutoff.
        control = (value.get('memory_controls') or {}).get('ai_records') or {}
        return control.get('enabled_for_ai',True) and not control.get('forget_before')

    def assert_current(self):
        from ollama_chat_app.web_desktop.bridge import BridgeRequestError
        value = self.last_snapshot or {}
        revision = value.get('memory_revision')
        if revision is None:
            return
        current = self.api._client.request('GET','/v1/member/memory/settings')
        if not isinstance(current,dict) or current.get('revision') != revision or not current.get('enabled'):
            raise BridgeRequestError('个人资料参考设置已变化，本次回答未保存，请重新提问。','memory_changed')
