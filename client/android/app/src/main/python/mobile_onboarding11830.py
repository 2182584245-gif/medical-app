"""Typed member-only first-meeting transport. No provider key or guessed result."""
from __future__ import annotations

import re
from copy import deepcopy

from ollama_chat_app.web_desktop.bridge import BridgeRequestError

ACTIONS = frozenset({'onboarding.get', 'onboarding.save', 'onboarding.suggest',
                     'onboarding.complete', 'onboarding.tutorial.complete'})
HAN = r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]'
STAGES = {'name', 'demographics', 'address', 'tutorial', 'done'}


def request(api, action, body):
    routes = {'onboarding.get': ('GET', ''), 'onboarding.save': ('PATCH', ''),
              'onboarding.suggest': ('POST', '/suggest'), 'onboarding.complete': ('POST', '/complete'),
              'onboarding.tutorial.complete': ('POST', '/tutorial-complete')}
    if action not in ACTIONS or not isinstance(body, dict):
        raise BridgeRequestError('首次见面请求格式无效。', 'validation')
    if action == 'onboarding.save':
        if body.get('stage') == 'name' and set(body) == {'stage', 'name'}:
            if not isinstance(body['name'], str) or re.fullmatch(HAN + '{2,4}', body['name'].strip()) is None:
                raise BridgeRequestError('字数不对哦', 'validation')
            body = {**body, 'name': body['name'].strip()}
        elif body.get('stage') == 'demographics' and set(body) == {'stage', 'age', 'gender'}:
            if type(body['age']) is not int or not 1 <= body['age'] <= 120 or body['gender'] not in {'male', 'female'}:
                raise BridgeRequestError('请选择年龄和性别。', 'validation')
        else:
            raise BridgeRequestError('首次见面输入格式无效。', 'validation')
    elif action == 'onboarding.complete':
        if set(body) != {'nickname'} or not isinstance(body['nickname'], str) or re.fullmatch(HAN + '{2,5}', body['nickname']) is None:
            raise BridgeRequestError('请选择本次生成的称呼。', 'validation')
    elif body:
        raise BridgeRequestError('首次见面请求不接受其他字段。', 'validation')
    method, suffix = routes[action]
    value = api._client.request(method, '/v1/member/onboarding' + suffix,
                                **({'json': body} if method != 'GET' else {}))
    return checked_status(value, api._actor_id())


def checked_status(value, actor):
    if (not isinstance(value, dict) or type(actor) is not int or actor <= 0
            or value.get('stage') not in STAGES
            or type(value.get('eligible')) is not bool or type(value.get('completed')) is not bool
            or type(value.get('tutorial_completed')) is not bool):
        raise BridgeRequestError('首次见面结果未能核对，请重试。', 'protocol')
    profile = value.get('profile')
    if not isinstance(profile, dict) or profile.get('user_id') != actor:
        raise BridgeRequestError('账号已改变，请重新打开页面。', 'account_changed')
    if profile.get('age') is not None and (type(profile['age']) is not int or not 1 <= profile['age'] <= 120):
        raise BridgeRequestError('年龄数据未能核对。', 'protocol')
    stage = value['stage']
    if (value['eligible'] != (stage != 'done') or value['completed'] != (stage in {'tutorial', 'done'})
            or value['tutorial_completed'] != (stage == 'done')):
        raise BridgeRequestError('首次见面进度未能核对。', 'protocol')
    candidates = value.get('candidates')
    if (not isinstance(candidates, list) or len(candidates) not in {0, 3}
            or any(not isinstance(v, str) or re.fullmatch(HAN + '{2,5}', v) is None for v in candidates)
            or len(set(candidates)) != len(candidates)):
        raise BridgeRequestError('称呼未生成完整，请再试一次。', 'protocol')
    return deepcopy(value)


def observe(local, identity, action, result):
    """Only confirmed cloud state enters encrypted account-local progress/cache."""
    value = checked_status(result, identity.actor_id)
    snapshot = value.pop('_profile_state', None)
    if snapshot is not None:
        if (not isinstance(snapshot, dict) or snapshot.get('schema_version') != 1
                or snapshot.get('actor_id') != identity.actor_id or snapshot.get('resource') != 'profile'
                or snapshot.get('complete') is not True or snapshot.get('offset') != 0
                or snapshot.get('value') != value['profile']
                or not isinstance(snapshot.get('server_instance_id'), str)
                or not re.fullmatch('[a-f0-9]{64}', str(snapshot.get('version', '')))):
            raise BridgeRequestError('本机个人信息未能核对，请重新读取。', 'protocol')
        if identity.server_instance_id and snapshot['server_instance_id'] != identity.server_instance_id:
            raise BridgeRequestError('服务身份已变化，请重新登录。', 'account_changed')
        local._guard(identity)
        local.store.replace_resources(identity, {'profile': {key: snapshot.get(key) for key in
            ('value', 'version', 'complete', 'memory_revision', 'server_instance_id', 'generated_at')}})
        value['_server_instance_id'] = snapshot['server_instance_id']
    local._guard(identity)
    local.store.put(identity, 'meta', 'onboarding11830', value)
    local.store.invalidate_action_cache(identity, 'health.profile.get')
    local.store.invalidate_action_cache(identity, 'preferences.get')
    local._ask_sync(force=True)


def cached_complete(local, identity):
    """A completed member must retain ordinary offline access after reboot."""
    value = local.store.get(identity, 'meta', 'onboarding11830')
    if (not isinstance(value, dict) or value.get('stage') != 'done'
            or not identity.server_instance_id
            or value.get('_server_instance_id') != identity.server_instance_id):
        return None
    result = checked_status(value, identity.actor_id)
    local._guard(identity)
    return result

