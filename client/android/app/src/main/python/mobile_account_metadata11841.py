"""Fixed member-only account metadata transport; never accepts tokens or URLs."""
from __future__ import annotations
from uuid import UUID
from datetime import datetime
import ipaddress
from ollama_chat_app.web_desktop.bridge import BridgeRequestError

ACTIONS=frozenset({'account.metadata.get','account.usage.heartbeat'})

def canonical_uuid(value):
    try:
        if type(value)is not str or str(UUID(value))!=value:raise ValueError
        return value
    except (ValueError,TypeError,AttributeError):
        raise BridgeRequestError('使用记录编号格式无效。','validation')from None

def request(api,action,body):
    if action not in ACTIONS or type(body)is not dict:
        raise BridgeRequestError('账户信息请求格式无效。','validation')
    if action=='account.metadata.get':
        if body:raise BridgeRequestError('账户信息请求不接受其他字段。','validation')
        result=api._client.request('GET','/v1/member/account-metadata')
    else:
        if set(body)!={'session_id','event_id','sequence','foreground'}:
            raise BridgeRequestError('使用记录字段无效。','validation')
        canonical_uuid(body['session_id']);canonical_uuid(body['event_id'])
        if type(body['sequence'])is not int or not 0<=body['sequence']<=9007199254740991 or type(body['foreground'])is not bool:
            raise BridgeRequestError('使用记录字段无效。','validation')
        result=api._client.request('POST','/v1/member/usage/heartbeat',json=dict(body))
    if type(result)is not dict or type(result.get('actor_id'))is not int or result.get('actor_id')!=api._actor_id():
        raise BridgeRequestError('账号已变化，请重新打开页面。','account_changed')
    if type(result.get('foreground_ms'))is not int or result['foreground_ms']<0:
        raise BridgeRequestError('账户使用信息未能核对。','protocol')
    if action=='account.usage.heartbeat':
        if set(result)!={'actor_id','foreground_ms','last_seen_at','counted_ms','replayed'}:
            raise BridgeRequestError('账户使用信息未能核对。','protocol')
        if type(result.get('counted_ms'))is not int or not 0<=result['counted_ms']<=30000 or type(result.get('replayed'))is not bool:
            raise BridgeRequestError('账户使用信息未能核对。','protocol')
        timestamps=('last_seen_at',)
    else:
        if (set(result)!={'actor_id','account_created_at','password_configured','last_login_ip','foreground_ms','usage_updated_at','memberships'}
            or type(result.get('password_configured'))is not bool or type(result.get('memberships'))is not list or len(result['memberships'])>100):
            raise BridgeRequestError('账户信息未能核对。','protocol')
        if result['last_login_ip'] is not None:
            try:ipaddress.ip_address(result['last_login_ip'])
            except (ValueError,TypeError):raise BridgeRequestError('账户信息未能核对。','protocol')from None
        for membership in result['memberships']:
            if type(membership)is not dict or set(membership)!={'plan_code','status','starts_at','ends_at'}:
                raise BridgeRequestError('会员信息未能核对。','protocol')
            if any(type(membership.get(key))is not str or not 1<=len(membership[key])<=64 for key in('plan_code','status')):
                raise BridgeRequestError('会员信息未能核对。','protocol')
            for key in('starts_at','ends_at'):
                if membership[key]is not None:_timestamp(membership[key])
        timestamps=('account_created_at','usage_updated_at')
    for key in timestamps:
        if result[key]is not None:_timestamp(result[key])
    # Exact response fields above ensure no upstream credential/hash additions
    # can reach the WebView, even after an unrelated server change.
    return {key:result[key]for key in result}

def _timestamp(value):
    try:
        if type(value)is not str or len(value)>40 or datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is None:raise ValueError
    except (ValueError,TypeError):raise BridgeRequestError('账户时间未能核对。','protocol')from None
