"""Internet-free validation of the native bridge contract."""
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE / 'android/app/src/main/python'), str(SOURCE / 'src')]

from mobile_ai149_recovery import InferenceAttempt
from mobile_api import MobileApplication, _REQUEST_IDENTITY149
from mobile_providers import MobileMemberBridge, ScopedCloudClient
from ollama_chat_app.providers.platform_deepseek import PlatformDeepSeekProvider, RECORD_DIALOGUE_PHOTOS, RECORD_TASKS
from ollama_chat_app.web_desktop.bridge import BridgeRequestError

MEMBER = {'id': 5, 'username': 'isolated-qa', 'role_code': 'member', 'account_status': 'active'}
TASK = 'c48dbdae-7610-43d8-b086-000000000001'
DRAFT = 'c48dbdae-7610-43d8-b086-000000000002'


def test_record_task_authorized_native_round_trip():
    requests = []
    task = {'id': TASK, 'draft_id': DRAFT, 'assistant_message_id': 73, 'category': 'diet', 'title': '午饭', 'status': 'ready'}
    draft = {'id': DRAFT, 'kind': 'record_draft', 'category': 'diet', 'occurred_at': '2026-11-15T12:30:00+08:00',
             'content': '番茄蛋', 'operation': 'create', 'target': None, 'details': {'food': '番茄蛋'},
             'missing_required': [], 'missing_optional': [], 'blocked': False, 'photo_file_ids': [81]}
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/capabilities'):
            return httpx.Response(200, json={'record_tasks': RECORD_TASKS})
        if request.url.path.endswith('/focus'):
            return httpx.Response(200, json={'task': task, 'draft': draft})
        return httpx.Response(200, json={'tasks': [task], 'total': 1, 'active_task_id': TASK, 'next_cursor': None})

    client = ScopedCloudClient('https://example.org/aliyun', transport=httpx.MockTransport(handler))
    client._token = 'a' * 43
    client._user = dict(MEMBER)
    bridge = MobileMemberBridge(base_url=client.base_url, client=client)
    bridge._api._user = dict(MEMBER)
    app = MobileApplication.__new__(MobileApplication)
    app.bridge, app.local, app._auth_epoch, app._native_hooks = bridge, None, 0, None
    scope = _REQUEST_IDENTITY149.set((5, 0))
    def invoke(action, **params):
        return app._online(f'chat.record_tasks.{action}', {'_fawnsee_actor_id': 5, **params}, str(uuid4()))
    try:
        assert invoke('capability')['result'] == {'record_tasks': True}
        assert invoke('list', conversation_id=41, query='番茄蛋', cursor='older-1', limit=20)['result']['tasks'] == [task]
        assert requests[-1].method == 'GET' and dict(requests[-1].url.params) == {
            'conversation_id': '41', 'query': '番茄蛋', 'cursor': 'older-1', 'limit': '20'}
        assert invoke('focus', conversation_id=41, task_id=TASK)['result']['draft']['photo_file_ids'] == [81]
        assert requests[-1].method == 'POST' and json.loads(requests[-1].content) == {'conversation_id': 41}
        assert requests[-1].headers['Authorization'].startswith('Bearer ')
        assert invoke('focus', conversation_id=41, task_id='not-valid')['error']['code'] == 'validation'
        assert invoke('list', conversation_id=41, limit=41)['error']['code'] == 'validation'
        assert len(requests) == 3
    finally:
        _REQUEST_IDENTITY149.reset(scope)
        client.close()


def test_provider_opt_in_keeps_task_id_out_of_other_requests():
    message = [{'role': 'user', 'content': '少盐'}]
    provider = PlatformDeepSeekProvider(object(), assistant_team=RECORD_DIALOGUE_PHOTOS,
                                        tools_enabled=True, record_tasks=RECORD_TASKS, record_task_id=TASK)
    assert 'record_task_id' not in provider._payload('deepseek-flash', message)
    provider.conversation_id, provider.assistant_message_id = 41, 73
    payload = provider._payload('deepseek-flash', message)
    assert payload['record_tasks'] == RECORD_TASKS and payload['record_task_id'] == TASK
    assert 'record_tasks' not in provider._payload('deepseek-flash', message, json_mode=True)
    provider.tools_enabled = False
    assert 'record_tasks' not in provider._payload('deepseek-flash', message)


def test_operation_cannot_replay_under_different_task():
    class Frozen:
        def ai149_payload_store(self, identifier, value):
            return {**value, 'record_task_id': DRAFT}
    attempt = InferenceAttempt(TASK, 'chat.send')
    with pytest.raises(BridgeRequestError) as failure:
        attempt.prepare(Frozen(), {'record_tasks': RECORD_TASKS, 'record_task_id': TASK}, stream=True)
    assert failure.value.code == 'ai_context_changed' and not attempt.has_submitted
