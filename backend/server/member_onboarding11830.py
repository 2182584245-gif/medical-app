"""Owner-bound first meeting. Existing preferences store progress, never a chat."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import nullcontext

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.health import HealthService
from .platform_auth import authentication_dependency, PlatformAuthError
from .platform_ai_proxy import AIProxyError, AIRequest

KEY = 'mobile_onboarding_v1'
HAN = r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]'
STAGES = ('name', 'demographics', 'address', 'tutorial', 'done')
MODEL = 'deepseek-flash'
FORMAT_RULE = ('输出格式必须严格遵守：只输出三个互不重复的中文称呼，每个称呼必须是2到5个汉字，'
               '用两个英文逗号分隔。模板中的X可灵活采用姓或名，使最终称呼达到2到5字。'
               '不要编号、引号、解释或额外内容。')


def valid_name(value):
    if not isinstance(value, str) or re.fullmatch(HAN + '{2,4}', value.strip()) is None:
        raise ValueError('字数不对哦')
    return value.strip()


def nicknames(content):
    """Accept only the requested three comma-separated, distinct Han names."""
    if not isinstance(content, str) or len(content) > 40:
        raise ValueError('称呼未生成完整，请再试一次')
    values = [item.strip() for item in content.strip().split(',')]
    if len(values) != 3 or len(set(values)) != 3 or any(re.fullmatch(HAN + '{2,5}', v) is None for v in values):
        raise ValueError('称呼未生成完整，请再试一次')
    return values


def fingerprint(profile):
    raw = json.dumps([profile.get(k) for k in ('display_name', 'age', 'gender')], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def nickname_prompt(profile):
    name = valid_name(profile.get('display_name'))
    age = HealthService._validate_profile_value('age', profile.get('age'))
    gender = {'male': '男', 'female': '女'}.get(profile.get('gender'))
    if age is None or gender is None:
        raise ValueError('请先选择年龄和性别')
    return ('我们是生活健康管理软件，需要为用户想一个合适的称呼，让我们的软件欢迎语能够向用户打招呼，'
            f'用户输入自己的名字叫“{name}”，年龄是“{age}”岁，性别是“{gender}”。'
            '可用的模板包括“X先生、X女士、X同学、小X同学、X老、X总、X老板、X仔、X哥、X姐、X叔、'
            'X阿姨、X爷爷、X奶奶、X小朋友、X宝宝、X宝贝、X大人”请用用户的姓与名自由结合，'
            '给出三种候选供用户选择以满足各种用户的需要。请直接输出2-5个字的称呼并输出三个让用户选择，'
            '用英文逗号隔开，不要输出任何其他内容。')


class OnboardingService:
    def __init__(self, auth, database, health, proxy):
        self.auth, self.db, self.health, self.proxy = auth, database, health, proxy
        self._locks = {}
        self.local_sync = None

    @staticmethod
    def actor(principal):
        actor = principal.user.id
        if principal.user.role_code != 'member' or type(actor) is not int or actor <= 0:
            raise HTTPException(403, '请先登录会员账号')
        return actor

    def identity(self, principal):
        return self.auth.identity(principal) if self.auth is not None else nullcontext()

    def _preferences(self, conn, actor):
        if conn.execute('SELECT id FROM users WHERE id=?', (actor,)).fetchone() is None:
            raise HTTPException(401, '账号已改变，请重新登录')
        row = conn.execute('SELECT preferences_json FROM user_preferences WHERE user_id=?', (actor,)).fetchone()
        return json.loads(row['preferences_json']) if row else {}

    def _write_state(self, conn, actor, preferences, state, *, preferred_name=None):
        values = {**preferences, KEY: state}
        if preferred_name is not None:
            values['preferred_name'] = preferred_name
        conn.execute('INSERT INTO user_preferences(user_id,preferences_json,updated_at) VALUES(?,?,?) '
                     'ON CONFLICT(user_id) DO UPDATE SET preferences_json=excluded.preferences_json,updated_at=excluded.updated_at',
                     (actor, json.dumps(values, ensure_ascii=False), timestamp_to_db(utc_now())))

    @staticmethod
    def _state(preferences):
        raw = preferences.get(KEY)
        if not isinstance(raw, dict) or raw.get('schema') != 1 or raw.get('stage') not in STAGES:
            return {'schema': 1, 'stage': 'name', 'candidates': [], 'profile_digest': None}
        return {key: raw.get(key) for key in ('schema', 'stage', 'candidates', 'profile_digest')}

    @staticmethod
    def _put_profile(conn, actor, values):
        # Partial UPSERT never replaces unrelated profile fields, e.g. avatar/phone.
        clean = {key: HealthService._validate_profile_value(key, value) for key, value in values.items()}
        keys = tuple(clean)
        assert set(keys) <= {'display_name', 'age', 'gender', 'ai_preferred_name'}
        now = timestamp_to_db(utc_now())
        columns = ','.join(('user_id', *keys, 'created_at', 'updated_at'))
        marks = ','.join('?' for _ in range(len(keys) + 3))
        assignments = ','.join(key + '=excluded.' + key for key in (*keys, 'updated_at'))
        conn.execute('INSERT INTO member_profiles(' + columns + ') VALUES(' + marks + ') '
                     'ON CONFLICT(user_id) DO UPDATE SET ' + assignments,
                     (actor, *[clean[key] for key in keys], now, now))
        HealthService._add_audit_log(conn, actor, 'member_profile.saved', 'member_profile', actor)

    def get(self, principal):
        actor = self.actor(principal)
        with self.identity(principal), self.db.connect() as conn:
            preferences = self._preferences(conn, actor)
            state = self._state(preferences)
        with self.identity(principal):
            profile = self.health.get_profile(actor)
        candidates = state.get('candidates') or []
        if state.get('profile_digest') != fingerprint(profile):
            candidates = []
        if not isinstance(candidates, list) or any(not isinstance(v, str) for v in candidates):
            candidates = []
        extra = {'_profile_state': self.local_sync.state(principal, 'profile')} if self.local_sync is not None else {}
        return {'eligible': state['stage'] != 'done', 'stage': state['stage'], 'profile': profile,
                'candidates': candidates, 'completed': state['stage'] in {'tutorial', 'done'},
                'tutorial_completed': state['stage'] == 'done', **extra}

    def save(self, principal, body):
        actor = self.actor(principal)
        stage = body.get('stage')
        if stage == 'name' and set(body) == {'stage', 'name'}:
            values = {'display_name': valid_name(body['name'])}
            target = 'demographics'
        elif stage == 'demographics' and set(body) == {'stage', 'age', 'gender'}:
            age = HealthService._validate_profile_value('age', body['age'])
            if age is None or body['gender'] not in {'male', 'female'}:
                raise ValueError('请选择年龄和性别')
            values = {'age': age, 'gender': body['gender']}
            target = 'address'
        else:
            raise ValueError('首次见面输入格式不正确')
        with self.identity(principal), self.db.transaction() as conn:
            preferences = self._preferences(conn, actor)
            state = self._state(preferences)
            if state['stage'] in {'tutorial', 'done'}:
                pass  # A replay cannot reopen a completed first meeting.
            elif stage == 'demographics' and state['stage'] == 'name':
                raise ValueError('请先填写姓名')
            else:
                self._put_profile(conn, actor, values)
                self._write_state(conn, actor, preferences,
                                  {'schema': 1, 'stage': target, 'candidates': [], 'profile_digest': None})
        return self.get(principal)

    async def suggest(self, request, principal):
        actor = self.actor(principal)
        # A bounded, in-process single-flight lane; no database lock during AI I/O.
        if actor not in self._locks and len(self._locks) >= 512:
            raise HTTPException(429, '请稍后再试')
        lock = self._locks.setdefault(actor, asyncio.Lock())
        try:
            async with lock:
                status = await asyncio.to_thread(self.get, principal)
                if status['stage'] in {'tutorial', 'done'} or status['candidates']:
                    return status
                if status['stage'] != 'address':
                    raise ValueError('请先填写姓名、年龄和性别')
                profile = status['profile']
                digest = fingerprint(profile)
                messages = [{'role': 'system', 'content': FORMAT_RULE},
                            {'role': 'user', 'content': nickname_prompt(profile)}]
                for attempt in range(2):
                    # Recheck before every paid attempt, including the one bounded correction.
                    await asyncio.to_thread(self.auth.resolve_token, request.headers['authorization'][7:])
                    current = await asyncio.to_thread(self.get, principal)
                    if current['stage'] != 'address' or fingerprint(current['profile']) != digest:
                        raise HTTPException(409, '个人信息已更新，请重新生成称呼')
                    value = AIRequest(model=MODEL, messages=messages,
                                      thinking={'type': 'disabled'}, reasoning_effort='low', max_tokens=256,
                                      web_search=False, tool_mode='none', personal_context=False)
                    result = await self.proxy.complete(request, value)
                    await asyncio.to_thread(self.auth.resolve_token, request.headers['authorization'][7:])
                    current = await asyncio.to_thread(self.get, principal)
                    if current['stage'] != 'address' or fingerprint(current['profile']) != digest:
                        raise HTTPException(409, '个人信息已更新，请重新生成称呼')
                    raw = result.get('content')
                    try:
                        candidates = nicknames(raw)
                    except ValueError:
                        if attempt == 1:
                            raise
                        # Preserve the original user prompt and actual failed answer.
                        # Only the model may choose/rewrite a name; never pad names in code.
                        messages = [*messages, {'role': 'assistant', 'content': raw},
                                    {'role': 'user', 'content': '上一条称呼未通过格式核对。请重新给出三个互不重复的称呼，'
                                     '每个严格2到5个汉字，只用英文逗号分隔，不要任何其他内容。'}]
                        continue
                    await asyncio.to_thread(self._save_candidates, principal, digest, candidates)
                    return await asyncio.to_thread(self.get, principal)
                raise ValueError('称呼未生成完整，请再试一次')
        finally:
            # Keep the current entry while waiters exist, otherwise bound growth.
            if not lock.locked() and not getattr(lock, '_waiters', None):
                self._locks.pop(actor, None)

    def _save_candidates(self, principal, digest, candidates):
        actor = self.actor(principal)
        with self.identity(principal), self.db.transaction() as conn:
            preferences = self._preferences(conn, actor)
            state = self._state(preferences)
            row = conn.execute('SELECT display_name,age,gender FROM member_profiles WHERE user_id=?', (actor,)).fetchone()
            if row is None or fingerprint(dict(row)) != digest or state['stage'] != 'address':
                raise HTTPException(409, '个人信息已更新，请重新生成称呼')
            self._write_state(conn, actor, preferences, {'schema': 1, 'stage': 'address',
                              'candidates': candidates, 'profile_digest': digest})

    def complete(self, principal, nickname):
        actor = self.actor(principal)
        profile = self.get(principal)['profile']
        with self.identity(principal), self.db.transaction() as conn:
            preferences = self._preferences(conn, actor)
            state = self._state(preferences)
            row = conn.execute('SELECT display_name,age,gender FROM member_profiles WHERE user_id=?', (actor,)).fetchone()
            if row is not None:profile = dict(row)
            if state['stage'] in {'tutorial', 'done'}:
                pass
            elif (state['stage'] != 'address' or state.get('profile_digest') != fingerprint(profile)
                  or nickname not in (state.get('candidates') or [])):
                raise ValueError('请选择本次生成的称呼')
            else:
                self._put_profile(conn, actor, {'ai_preferred_name': nickname})
                self._write_state(conn, actor, preferences, {**state, 'stage': 'tutorial'}, preferred_name=nickname)
        return self.get(principal)

    def finish_tutorial(self, principal):
        actor = self.actor(principal)
        with self.identity(principal), self.db.transaction() as conn:
            preferences = self._preferences(conn, actor)
            state = self._state(preferences)
            if state['stage'] not in {'tutorial', 'done'}:
                raise ValueError('请先选择称呼')
            self._write_state(conn, actor, preferences, {**state, 'stage': 'done'})
        return self.get(principal)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    stage: str
    name: str | None = Field(default=None, max_length=16)
    age: int | None = Field(default=None, ge=1, le=120)
    gender: str | None = None


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    nickname: str = Field(min_length=2, max_length=5)


def create_onboarding_router(service):
    router = APIRouter(prefix='/v1/member/onboarding', tags=['member'])
    principal = Depends(authentication_dependency(service.auth))

    def answer(fn):
        try:
            return JSONResponse(jsonable_encoder(fn()), headers={'Cache-Control': 'no-store'})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get('')
    def get(identity=principal):
        return answer(lambda: service.get(identity))

    @router.patch('')
    def save(value: Input, identity=principal):
        return answer(lambda: service.save(identity, value.model_dump(exclude_unset=True)))

    @router.post('/suggest')
    async def suggest(request: Request, identity=principal):
        try:
            return JSONResponse(jsonable_encoder(await service.suggest(request, identity)), headers={'Cache-Control': 'no-store'})
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except AIProxyError as error:
            raise HTTPException(error.status, '称呼暂时未生成，请稍后重试') from None
        except PlatformAuthError as error:
            raise HTTPException(error.status_code, '登录状态已变化，请重新登录') from None

    @router.post('/complete')
    def complete(value: Selection, identity=principal):
        return answer(lambda: service.complete(identity, value.nickname))

    @router.post('/tutorial-complete')
    def tutorial(identity=principal):
        return answer(lambda: service.finish_tutorial(identity))

    return router
