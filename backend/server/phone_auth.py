"""Phone-bound accounts, one-use SMS proof and rotating device login sessions."""
from __future__ import annotations

import base64
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.security.passwords import normalize_username
from ollama_chat_app.services.auth import AuthService
from .platform_auth import (PlatformAuthError, SafeAuthRoute, USER_COLUMNS,
                            _http_call, _safe_operation, authentication_dependency)
from .phone_auth_schema import verify_shape
from .sms_aliyun import make_sms_sender, SmsUnavailable
from .account_erasure import erase_account_rows
from .phone_credentials11838 import PhoneCredentials, reserved_username

DEVELOPER_USERNAME = '88888888'
OTP_SECONDS, REFRESH_SECONDS, ERASE_SECONDS = 300, 30 * 86400, 86400
ENTRY_REGISTRATION_SECONDS = 600
INVALID_CODE = '验证码无效或已过期，请重新获取。'


def server_phone_required():
    """Explicit deployment phase, never selected from requests or SMS keys."""
    value=os.environ.get('FAWNSEE_AUTH_PHONE_REQUIRED','true')
    if value not in {'true','false'}:
        raise RuntimeError('FAWNSEE_AUTH_PHONE_REQUIRED must explicitly be true or false')
    return value=='true'


def server_developer_exempt_username():
    """Only an explicitly enabled reserved member can bypass phone binding."""
    value = os.environ.get('FAWNSEE_AUTH_PHONE_EXEMPT_USERNAME', '')
    if value not in {'', DEVELOPER_USERNAME}:
        raise RuntimeError('FAWNSEE_AUTH_PHONE_EXEMPT_USERNAME must be empty or 88888888')
    return value


def normalize_phone(value: str) -> str:
    if not isinstance(value, str) or len(value) > 20:
        raise PlatformAuthError(422, '请填写有效的中国大陆手机号码。')
    value = value.strip()
    if value.startswith('+86'):
        value = value[3:]
    if not re.fullmatch(r'1[3-9][0-9]{9}', value):
        raise PlatformAuthError(422, '请填写有效的中国大陆手机号码。')
    return '+86' + value


def masked_phone(phone: str) -> str:
    local = phone[3:]
    return local[:3] + '****' + local[-4:]


class PhoneAuth:
    def __init__(self, auth, *, sender=None, clock=time.time, check_schema=True, phone_required=None):
        self.auth, self.database, self.clock = auth, auth.database, clock
        self.phone_required=server_phone_required() if phone_required is None else phone_required
        if type(self.phone_required) is not bool:
            raise ValueError('Phone-auth phase must be a server boolean')
        self.developer_exempt_username = server_developer_exempt_username()
        self.sender = sender if sender is not None else make_sms_sender()
        self.postgres = getattr(self.database, 'dialect', 'sqlite') == 'postgresql'
        self.key = hmac.new(bytes.fromhex(auth.settings.token_pepper.get_secret_value()),
                            b'fawnsee-phone-auth-v1', hashlib.sha256).digest()
        if check_schema:
            verify_shape(self.database)
        auth.phone_auth = self
        self.credentials = PhoneCredentials(self, check_schema=check_schema)

    def context(self, **values):
        method = getattr(self.database, 'identity', None)
        return method(**values) if method else nullcontext()

    def digest(self, purpose: str, *values: str) -> str:
        return hmac.new(self.key, ('\0'.join((purpose, *values))).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def expire_ephemeral(connection, now):
        connection.execute('DELETE FROM phone_auth_limits WHERE window_start<?', (now - 172800,))
        for table in ('phone_auth_challenges','phone_auth_refreshes','phone_auth_erase_receipts'):
            connection.execute('DELETE FROM ' + table + ' WHERE expires_at<=?', (now,))
        # Keep a minimal completion proof for safe offline reset retries. Only
        # encrypted replacement credentials expire; never repeat an old reset.
        connection.execute('UPDATE account_reset_receipts11831 SET new_token_digest=NULL,password_marker=NULL,response_envelope=NULL WHERE expires_at<=? AND response_envelope IS NOT NULL',(now,))

    def budget(self, client_ip: str, phone: str, *, operation='verify'):
        # Code guessing, refresh and erasure keep their existing protections.
        # Sending has no application-owned count or wait budget.
        now = int(self.clock())
        axes = [('global', '', 60, 300),
                ('ip', client_ip[:256], 60, 60),
                ('phone', phone, 60, 30)]
        rejected = False
        with self.database.transaction() as connection:
            for axis, value, duration, limit in axes:
                key = self.digest('limit', operation, axis, value)
                window = now // duration * duration
                connection.execute(
                    'INSERT INTO phone_auth_limits(scope_key,window_start,attempts) VALUES(?,?,1) '
                    'ON CONFLICT(scope_key) DO UPDATE SET window_start=excluded.window_start,'
                    'attempts=CASE WHEN phone_auth_limits.window_start=excluded.window_start '
                    'THEN CASE WHEN phone_auth_limits.attempts<1000000 THEN phone_auth_limits.attempts+1 ELSE 1000000 END ELSE 1 END',
                    (key, window))
                attempts = connection.execute('SELECT attempts FROM phone_auth_limits WHERE scope_key=?', (key,)).fetchone()[0]
                if int(attempts) > limit:
                    rejected = True
                    break
            self.expire_ephemeral(connection, now)
        if rejected:
            raise PlatformAuthError(429, '操作过于频繁，请稍后再试。')

    def is_developer_exempt(self, username, role):
        return (self.developer_exempt_username == DEVELOPER_USERNAME
                and username == DEVELOPER_USERNAME and role == 'member')

    def require_bound(self, connection, user_id, username, role, *, verified_phone=None):
        if not self.phone_required:
            return
        if verified_phone is None and self.is_developer_exempt(username, role):
            return
        binding = connection.execute('SELECT phone FROM phone_auth_bindings WHERE user_id=?', (user_id,)).fetchone()
        if binding is None:
            if role == 'member' and verified_phone is None:
                password_signup = connection.execute(
                    'SELECT 1 FROM password_signup_members WHERE user_id=?', (user_id,)
                ).fetchone()
                if password_signup is not None:
                    return
            raise PlatformAuthError(401, '账号尚未绑定手机号，请重新完成手机号验证注册。')
        if verified_phone is not None and not hmac.compare_digest(str(binding['phone']), verified_phone):
            raise PlatformAuthError(401, '账号手机号已改变，请重新验证。')

    def initialize_profile_phone(self, connection, user_id):
        """Seed a verified number once; an explicit edit/clear is permanent.

        The profile marker is private bookkeeping and never an AI field. A
        developer-exempt account without a verified binding receives no value.
        """
        binding = connection.execute('SELECT phone FROM phone_auth_bindings WHERE user_id=?',(user_id,)).fetchone()
        if binding is None:return
        now=timestamp_to_db(utc_now())
        connection.execute('INSERT INTO member_profiles(user_id,phone,phone_default_initialized,created_at,updated_at) VALUES(?,?,1,?,?) '
            'ON CONFLICT(user_id) DO UPDATE SET phone=CASE WHEN COALESCE(member_profiles.phone,\'\')=\'\' THEN excluded.phone ELSE member_profiles.phone END,'
            'phone_default_initialized=1,updated_at=excluded.updated_at WHERE member_profiles.phone_default_initialized=0',
            (user_id,str(binding['phone']),now,now))

    @_safe_operation
    def phone_info(self, principal):
        with self.auth.identity(principal), self.database.connect() as connection:
            row = connection.execute('SELECT phone,verified_at FROM phone_auth_bindings WHERE user_id=?', (principal.user.id,)).fetchone()
        return {'phone': str(row['phone']) if row else None, 'verified_at': str(row['verified_at']) if row else None,
                'developer_exempt': self.is_developer_exempt(principal.user.username_normalized, principal.user.role_code)}

    def require_phone_entrypoints(self):
        if not self.phone_required:
            raise PlatformAuthError(503,'手机号验证码功能暂未开放，请使用账号密码登录或注册。')

    @_safe_operation
    def send(self, phone, purpose, *, client_ip, principal=None):
        self.require_phone_entrypoints()
        phone = normalize_phone(phone)
        if purpose not in {'register','login','bind','change'}:
            raise PlatformAuthError(422, '验证码用途无效。')
        user_id = None
        if purpose in {'bind','change'}:
            if principal is None or principal.user.role_code != 'member':
                raise PlatformAuthError(401, '请先登录。')
            current = self.phone_info(principal)['phone']
            if (purpose == 'bind') != (current is None):
                raise PlatformAuthError(409, '手机号绑定状态已改变，请重新打开设置。')
            user_id = principal.user.id
        now, identifier = int(self.clock()), str(uuid4())
        code = f'{secrets.randbelow(1000000):06d}'
        digest = self.digest('otp', identifier, phone, purpose, code)
        identity = {'auth_phone': phone, 'user_id': user_id, 'role_code': 'member' if user_id else None}
        with self.context(**identity), self.database.transaction() as connection:
            self.expire_ephemeral(connection, now)
            connection.execute('INSERT INTO phone_auth_challenges(challenge_id,phone,purpose,user_id,code_digest,created_at,expires_at,attempts,state) VALUES(?,?,?,?,?,?,?,0,?)',
                               (identifier, phone, purpose, user_id, digest, now, now + OTP_SECONDS, 'pending'))
        try:
            result = self.sender.send(phone, code, challenge_id=identifier, purpose=purpose)
            if not isinstance(result, dict) or result.get('accepted') is not True:
                raise SmsUnavailable('短信未确认发送。')
        except Exception as error:
            with self.context(**identity), self.database.transaction() as connection:
                connection.execute("UPDATE phone_auth_challenges SET state='failed' WHERE challenge_id=?", (identifier,))
            message = str(error) if isinstance(error, SmsUnavailable) else '凡思验证码暂未确认发送成功，请稍后再试。'
            raise PlatformAuthError(503, message) from None
        finally:
            code = ''
        with self.context(**identity), self.database.transaction() as connection:
            # PlatformDatabase serializes write transactions across processes;
            # SQLite uses BEGIN IMMEDIATE. Only this short finalization holds a
            # write lock, never provider I/O. The last successful confirmation
            # becomes current; pending requests cannot invalidate one another.
            changed = connection.execute("UPDATE phone_auth_challenges SET state='sent' WHERE challenge_id=? AND state='pending' AND expires_at>?", (identifier, int(self.clock()))).rowcount
            if changed == 1:
                connection.execute("UPDATE phone_auth_challenges SET state='failed' WHERE phone=? AND purpose=? AND challenge_id<>? AND state='sent'", (phone, purpose, identifier))
        if changed != 1:
            raise PlatformAuthError(409, '验证码状态已更新，请稍后重新获取。')
        return {'challenge_id': identifier, 'expires_in': OTP_SECONDS, 'retry_after': 0, 'phone_masked': masked_phone(phone)}

    def consume(self, phone, purpose, identifier, code, *, client_ip, user_id=None):
        phone = normalize_phone(phone)
        try:
            identifier = str(UUID(str(identifier)))
        except ValueError:
            raise PlatformAuthError(401, INVALID_CODE) from None
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
            raise PlatformAuthError(401, INVALID_CODE)
        self.budget(client_ip, phone)
        accepted = False
        with self.context(auth_phone=phone, user_id=user_id, role_code='member' if user_id else None), self.database.transaction() as connection:
            row = connection.execute('SELECT * FROM phone_auth_challenges WHERE challenge_id=? AND phone=? AND purpose=?' + self.auth._lock_suffix(), (identifier, phone, purpose)).fetchone()
            if (row is not None and row['state'] == 'sent' and int(row['expires_at']) > int(self.clock())
                    and int(row['attempts']) < 5 and row['user_id'] == user_id):
                accepted = hmac.compare_digest(str(row['code_digest']), self.digest('otp', identifier, phone, purpose, code))
                connection.execute('UPDATE phone_auth_challenges SET attempts=attempts+1,state=? WHERE challenge_id=?', ('consumed' if accepted else 'sent', identifier))
        # Failed attempt increments must commit; raising inside the transaction
        # would roll them back and permit unlimited guesses.
        if not accepted:
            raise PlatformAuthError(401, INVALID_CODE)
        return phone

    @_safe_operation
    def register_complete(self, username, password, phone, challenge_id, code, *, client_ip):
        self.require_phone_entrypoints()
        if reserved_username(normalize_username(username)):
            raise PlatformAuthError(409, '该账号名称不可用于注册。')
        self.auth.limiter.check(client_ip, normalize_username(username))
        with self.auth.security.hash_slot():
            display, normalized, password_hash = AuthService._prepare_new_account(username, password)
        phone = self.consume(phone, 'register', challenge_id, code, client_ip=client_ip)
        now = timestamp_to_db(utc_now())
        with self.context(auth_username=normalized, registering=True, auth_phone=phone), self.database.transaction() as connection:
            row = AuthService._insert_account(connection, display_username=display, normalized_username=normalized,
                                              password_hash=password_hash, role_code='member', now=now)
            user_id = int(row['id'])
            with self.context(user_id=user_id, role_code='member', auth_phone=phone, auth_username=normalized, registering=True):
                connection.execute('INSERT INTO phone_auth_bindings(user_id,phone,verified_at) VALUES(?,?,?)', (user_id, phone, now))
                full = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?', (user_id,)).fetchone()
                return self.auth._issue_verified_session(full, client_ip=client_ip)

    @_safe_operation
    def phone_login(self, phone, challenge_id, code, *, client_ip):
        self.require_phone_entrypoints()
        phone = self.consume(phone, 'login', challenge_id, code, client_ip=client_ip)
        with self.context(auth_phone=phone), self.database.connect() as connection:
            binding = connection.execute('SELECT user_id FROM phone_auth_bindings WHERE phone=?', (phone,)).fetchone()
        if binding is None:
            raise PlatformAuthError(401, '手机号或验证码无效，请确认账号后重试。')
        with self.context(user_id=int(binding['user_id']), role_code='member'), self.database.connect() as connection:
            row = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?', (int(binding['user_id']),)).fetchone()
        if row is None or row['role_code'] != 'member':
            raise PlatformAuthError(401, '手机号或验证码无效，请确认账号后重试。')
        return self.auth._issue_verified_session(row, verified_phone=phone, client_ip=client_ip)

    def registration_proof(self, phone, challenge_id, expires):
        # The signed claim is useless without the exact, still-live consumed
        # challenge row. Neither plaintext OTP nor credentials enter this token.
        value = {'phone': phone, 'challenge_id': challenge_id, 'expires_at': expires}
        payload = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')
        return 'pr1_' + payload + '.' + self.digest('entry-registration-proof', payload)

    def parse_registration_proof(self, token):
        invalid = PlatformAuthError(401, '手机号验证已失效，请重新验证手机号。')
        if type(token) is not str or not 50 <= len(token) <= 512:
            raise invalid
        match = re.fullmatch(r'pr1_([A-Za-z0-9_-]+)\.([a-f0-9]{64})', token)
        if match is None or not hmac.compare_digest(match[2], self.digest('entry-registration-proof', match[1])):
            raise invalid
        try:
            value = json.loads(base64.b64decode(match[1] + '=' * (-len(match[1]) % 4), altchars=b'-_', validate=True))
            if (type(value) is not dict or set(value) != {'phone','challenge_id','expires_at'}
                    or normalize_phone(value['phone']) != value['phone']
                    or type(value['expires_at']) is not int or value['expires_at'] <= int(self.clock())
                    or type(value['challenge_id']) is not str or str(UUID(value['challenge_id'])) != value['challenge_id']):
                raise ValueError
        except (ValueError,TypeError,KeyError,UnicodeDecodeError):
            raise invalid from None
        return value

    @_safe_operation
    def phone_entry(self, phone, challenge_id, code, *, client_ip):
        self.require_phone_entrypoints()
        phone = self.consume(phone, 'login', challenge_id, code, client_ip=client_ip)
        with self.context(auth_phone=phone), self.database.transaction() as connection:
            binding = connection.execute('SELECT user_id FROM phone_auth_bindings WHERE phone=?', (phone,)).fetchone()
            if binding is None:
                expires = int(self.clock()) + ENTRY_REGISTRATION_SECONDS
                changed = connection.execute("UPDATE phone_auth_challenges SET expires_at=? WHERE challenge_id=? AND phone=? AND purpose='login' AND user_id IS NULL AND state='consumed' AND expires_at>?",
                    (expires, str(challenge_id), phone, int(self.clock()))).rowcount
                if changed != 1:
                    raise PlatformAuthError(401, INVALID_CODE)
                return {'outcome': 'register', 'phone_masked': masked_phone(phone),
                    'registration_token': self.registration_proof(phone, str(challenge_id), expires),
                    'expires_in': ENTRY_REGISTRATION_SECONDS}
            user_id = int(binding['user_id'])
        with self.context(user_id=user_id, role_code='member'), self.database.connect() as connection:
            row = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?', (user_id,)).fetchone()
        if row is None or row['role_code'] != 'member':
            raise PlatformAuthError(401, '手机号或验证码无效，请确认账号后重试。')
        return {'outcome': 'login', **self.auth._issue_verified_session(row, verified_phone=phone, client_ip=client_ip)}

    @_safe_operation
    def register_verified(self, registration_token, username, password, *, client_ip):
        self.require_phone_entrypoints()
        proof = self.parse_registration_proof(registration_token)
        normalized = normalize_username(username)
        if reserved_username(normalized):
            raise PlatformAuthError(409, '该账号名称不可用于注册。')
        self.auth.limiter.check(client_ip, normalized)
        with self.auth.security.hash_slot():
            display, normalized, password_hash = AuthService._prepare_new_account(username, password)
        phone, identifier = proof['phone'], proof['challenge_id']
        # Establish only the owner found through this authenticated phone claim.
        # This also permits narrow lost-response recovery after the same request
        # created its one account; new credentials can never reuse this proof.
        with self.context(auth_phone=phone), self.database.connect() as connection:
            existing = connection.execute('SELECT user_id FROM phone_auth_bindings WHERE phone=?', (phone,)).fetchone()
        actor = int(existing['user_id']) if existing else None
        with self.context(auth_phone=phone, user_id=actor, role_code='member' if actor else None,
                          auth_username=normalized, registering=True), self.database.transaction() as connection:
            challenge = connection.execute('SELECT * FROM phone_auth_challenges WHERE challenge_id=? AND phone=?' + self.auth._lock_suffix(), (identifier, phone)).fetchone()
            if (challenge is None or challenge['purpose'] != 'login'
                    or int(challenge['expires_at']) != proof['expires_at'] or int(challenge['expires_at']) <= int(self.clock())):
                raise PlatformAuthError(401, '手机号验证已失效，请重新验证手机号。')
            binding = connection.execute('SELECT user_id FROM phone_auth_bindings WHERE phone=?', (phone,)).fetchone()
            if challenge['state'] == 'failed' and challenge['user_id'] is not None and binding is not None and int(challenge['user_id']) == int(binding['user_id']) == actor:
                row = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?' + self.auth._lock_suffix(), (actor,)).fetchone()
                if row is None or row['username_normalized'] != normalized or row['role_code'] != 'member':
                    raise PlatformAuthError(409, '手机号已完成注册，请返回手机号登录。')
                if not self.auth.security.verify_password(password, str(row['password_hash'])):
                    raise PlatformAuthError(401, '手机号已完成注册，请使用已设置的账号密码登录。')
                return self.auth._issue_verified_session(row, verified_phone=phone, client_ip=client_ip)
            if challenge['state'] != 'consumed' or challenge['user_id'] is not None:
                raise PlatformAuthError(401, '手机号验证已失效，请重新验证手机号。')
            if binding is not None:
                raise PlatformAuthError(409, '手机号已完成注册，请返回手机号登录。')
            now = timestamp_to_db(utc_now())
            row = AuthService._insert_account(connection, display_username=display, normalized_username=normalized,
                password_hash=password_hash, role_code='member', now=now)
            actor = int(row['id'])
            with self.context(user_id=actor, role_code='member', auth_phone=phone, auth_username=normalized, registering=True):
                connection.execute('INSERT INTO phone_auth_bindings(user_id,phone,verified_at) VALUES(?,?,?)', (actor, phone, now))
                changed = connection.execute("UPDATE phone_auth_challenges SET state='failed',user_id=? WHERE challenge_id=? AND phone=? AND state='consumed' AND user_id IS NULL", (actor, identifier, phone)).rowcount
                if changed != 1:
                    raise PlatformAuthError(401, '手机号验证已失效，请重新验证手机号。')
                full = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?', (actor,)).fetchone()
                return self.auth._issue_verified_session(full, verified_phone=phone, client_ip=client_ip)

    def password_row(self, principal, password, client_ip):
        self.auth.limiter.check(client_ip, principal.user.username_normalized)
        with self.auth.identity(principal), self.database.connect() as connection:
            row = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?', (principal.user.id,)).fetchone()
        stored = str(row['password_hash']) if row else self.auth.security.dummy_hash
        if not self.auth.security.verify_password(password, stored) or row is None:
            raise PlatformAuthError(401, '当前密码不正确。')
        return row

    @_safe_operation
    def bind(self, principal, phone, challenge_id, code, current_password, *, client_ip):
        self.require_phone_entrypoints()
        if principal.user.role_code != 'member':
            raise PlatformAuthError(403, '此功能只向当前会员账号开放。')
        row = self.password_row(principal, current_password, client_ip)
        current = self.phone_info(principal)['phone']
        phone = self.consume(phone, 'change' if current else 'bind', challenge_id, code,
                             client_ip=client_ip, user_id=principal.user.id)
        now = timestamp_to_db(utc_now())
        with self.context(user_id=principal.user.id, role_code='member', auth_phone=phone), self.database.transaction() as connection:
            locked = connection.execute('SELECT password_hash,account_status,role_code FROM users WHERE id=?' + self.auth._lock_suffix(), (principal.user.id,)).fetchone()
            session = connection.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',
                                         (principal.token_digest, principal.user.id, timestamp_to_db(utc_now()))).fetchone()
            if locked is None or session is None or locked['role_code'] != 'member' or locked['account_status'] != 'active' or not hmac.compare_digest(str(locked['password_hash']), str(row['password_hash'])):
                raise PlatformAuthError(401, '账号状态已改变，请重新登录。')
            existing = connection.execute('SELECT phone FROM phone_auth_bindings WHERE user_id=?', (principal.user.id,)).fetchone()
            if (str(existing['phone']) if existing else None) != current:
                raise PlatformAuthError(409, '手机号绑定状态已改变，请重试。')
            connection.execute('INSERT INTO phone_auth_bindings(user_id,phone,verified_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET phone=excluded.phone,verified_at=excluded.verified_at', (principal.user.id, phone, now))
            self.revoke_refresh(connection, principal.user.id)
            connection.execute('UPDATE platform_sessions SET revoked_at=? WHERE user_id=? AND token_digest<>? AND revoked_at IS NULL', (now, principal.user.id, principal.token_digest))
        return self.phone_info(principal)

    def issue_refresh(self, connection, user_id, access_digest, password_hash, *, token=None, family=None):
        now = int(self.clock())
        token, family = token or 'fr1_' + secrets.token_urlsafe(32), family or str(uuid4())
        connection.execute('INSERT INTO phone_auth_refreshes(token_digest,user_id,family_id,access_digest,password_marker,created_at,expires_at,revoked_at,rotated_to,request_digest) VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                           (self.digest('refresh', token), user_id, family, access_digest, self.digest('password-marker', password_hash), now, now + REFRESH_SECONDS))
        return {'refresh_token': token, 'refresh_expires_at': datetime.fromtimestamp(now + REFRESH_SECONDS, timezone.utc).isoformat().replace('+00:00','Z')}

    def revoke_refresh(self, connection, user_id, *, access_digest=None):
        sql = 'UPDATE phone_auth_refreshes SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL'
        values = [int(self.clock()), user_id]
        if access_digest:
            sql += ' AND family_id IN (SELECT family_id FROM phone_auth_refreshes WHERE user_id=? AND access_digest=?)'
            values += [user_id, access_digest]
        connection.execute(sql, tuple(values))

    def rotated_token(self, token, request_id):
        raw = hmac.new(self.key, ('rotate\0' + token + '\0' + request_id).encode(), hashlib.sha256).digest()
        return 'fr1_' + base64.urlsafe_b64encode(raw).decode().rstrip('=')

    @_safe_operation
    def refresh(self, token, request_id, *, client_ip):
        if not isinstance(token, str) or not re.fullmatch(r'fr1_[A-Za-z0-9_-]{43}', token):
            raise PlatformAuthError(401, '需要重新登录。')
        identifier = str(UUID(str(request_id))) if request_id else str(uuid4())
        digest, now = self.digest('refresh', token), int(self.clock())
        self.budget(client_ip, digest, operation='refresh')
        rejected, result = False, None
        with self.context(auth_refresh_digest=digest), self.database.transaction() as connection:
            # A digest proves read visibility before the account is known. PG
            # FOR UPDATE also requires owner UPDATE visibility, so establish
            # the server-derived owner first, then lock and recheck that row.
            old = connection.execute('SELECT * FROM phone_auth_refreshes WHERE token_digest=?', (digest,)).fetchone()
            if old is None or int(old['expires_at']) <= now:
                rejected = True
            else:
                user_id = int(old['user_id'])
                with self.context(user_id=user_id, role_code='member', auth_refresh_digest=digest):
                    old = connection.execute('SELECT * FROM phone_auth_refreshes WHERE token_digest=?' + self.auth._lock_suffix(), (digest,)).fetchone()
                    row = connection.execute('SELECT ' + USER_COLUMNS + ',password_hash FROM users WHERE id=?' + self.auth._lock_suffix(), (user_id,)).fetchone()
                    if (old is None or int(old['user_id']) != user_id or int(old['expires_at']) <= now
                            or row is None or row['role_code'] != 'member' or row['account_status'] != 'active'
                            or not hmac.compare_digest(str(old['password_marker']), self.digest('password-marker', str(row['password_hash'])))):
                        rejected = True
                    else:
                        next_token = self.rotated_token(token, identifier)
                        next_digest = self.digest('refresh', next_token)
                        request_digest = self.digest('refresh-request', token, identifier)
                        recovering = old['revoked_at'] is not None
                        successor = connection.execute('SELECT * FROM phone_auth_refreshes WHERE token_digest=?' + self.auth._lock_suffix(), (next_digest,)).fetchone() if recovering else None
                        allowed_recovery = (recovering and old['request_digest'] == request_digest and old['rotated_to'] == next_digest
                                            and now - int(old['revoked_at']) <= 300 and successor is not None
                                            and successor['revoked_at'] is None and successor['rotated_to'] is None and int(successor['expires_at']) > now)
                        if recovering and not allowed_recovery:
                            connection.execute('UPDATE phone_auth_refreshes SET revoked_at=? WHERE user_id=? AND family_id=? AND revoked_at IS NULL', (now, user_id, old['family_id']))
                            connection.execute('UPDATE platform_sessions SET revoked_at=? WHERE user_id=? AND token_digest IN (SELECT access_digest FROM phone_auth_refreshes WHERE user_id=? AND family_id=?) AND revoked_at IS NULL', (timestamp_to_db(utc_now()), user_id, user_id, old['family_id']))
                            rejected = True
                        else:
                            self.require_bound(connection, user_id, row['username_normalized'], row['role_code'])
                            result = self.auth._issue_verified_session(row, issue_refresh=False, client_ip=client_ip)
                            access = self.auth.security.token_digest(result['access_token'])
                            if allowed_recovery:
                                connection.execute('UPDATE platform_sessions SET revoked_at=? WHERE token_digest=? AND user_id=? AND revoked_at IS NULL', (timestamp_to_db(utc_now()), successor['access_digest'], user_id))
                                connection.execute('UPDATE phone_auth_refreshes SET access_digest=? WHERE token_digest=?', (access, next_digest))
                                result.update({'refresh_token': next_token, 'refresh_expires_at': datetime.fromtimestamp(int(successor['expires_at']), timezone.utc).isoformat().replace('+00:00','Z')})
                            else:
                                connection.execute('UPDATE phone_auth_refreshes SET revoked_at=?,rotated_to=?,request_digest=? WHERE token_digest=?', (now, next_digest, request_digest, digest))
                                result.update(self.issue_refresh(connection, user_id, access, str(row['password_hash']), token=next_token, family=old['family_id']))
        if rejected:
            raise PlatformAuthError(401, '登录状态已失效，请重新登录。')
        return result

    def completed_erasure(self, receipt, now):
        with self.context(erase_receipt_digest=receipt), self.database.connect() as connection:
            previous = connection.execute('SELECT expires_at FROM phone_auth_erase_receipts WHERE receipt_digest=?', (receipt,)).fetchone()
        return previous is not None and int(previous['expires_at']) > now

    @_safe_operation
    def erase(self, token, request_id, password, confirmation, *, client_ip):
        if confirmation != '删除账号及全部数据':
            raise PlatformAuthError(422, '请确认删除账号及全部数据。')
        identifier = str(UUID(str(request_id))) if request_id else str(uuid4())
        receipt = self.digest('erase-receipt', token, identifier)
        self.budget(client_ip, receipt, operation='erase')
        now = int(self.clock())
        if self.completed_erasure(receipt, now):
            return {'erased': True, 'local_purge_required': True}
        try:
            principal = self.auth.resolve_token(token)
            if principal.user.role_code != 'member':
                raise PlatformAuthError(403, '此入口只允许注销当前会员账号。')
            row = self.password_row(principal, password, client_ip)
        except PlatformAuthError as error:
            # A concurrent copy of this exact request may have committed its
            # deletion after our first receipt read but before authentication.
            if error.status_code == 401 and self.completed_erasure(receipt, now):
                return {'erased': True, 'local_purge_required': True}
            raise
        phone = self.phone_info(principal)['phone']
        with self.context(user_id=principal.user.id, role_code='member', auth_phone=phone, erasing=True, erase_receipt_digest=receipt), self.database.transaction() as connection:
            current = connection.execute('SELECT password_hash,account_status FROM users WHERE id=?' + self.auth._lock_suffix(), (principal.user.id,)).fetchone()
            session = connection.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?', (principal.token_digest, principal.user.id, timestamp_to_db(utc_now()))).fetchone()
            if current is None or session is None or current['account_status'] != 'active' or not hmac.compare_digest(str(current['password_hash']), str(row['password_hash'])):
                previous = connection.execute('SELECT expires_at FROM phone_auth_erase_receipts WHERE receipt_digest=?', (receipt,)).fetchone()
                if previous is not None and int(previous['expires_at']) > now:
                    return {'erased': True, 'local_purge_required': True}
                raise PlatformAuthError(401, '账号状态已改变，请重新登录。')
            erase_account_rows(connection, principal.user.id, postgres=self.postgres)
            connection.execute('INSERT INTO phone_auth_erase_receipts(receipt_digest,completed_at,expires_at) VALUES(?,?,?)', (receipt, now, now + ERASE_SECONDS))
        return {'erased': True, 'local_purge_required': True}


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class SendPayload(StrictPayload):
    phone: str = Field(min_length=1, max_length=20)
    purpose: str = Field(pattern=r'^(register|login|bind|change)$')


class CodePayload(StrictPayload):
    phone: str = Field(min_length=1, max_length=20)
    challenge_id: UUID
    code: SecretStr = Field(min_length=6, max_length=6)


class RegisterPayload(CodePayload):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8, max_length=256)


class BindPayload(CodePayload):
    current_password: SecretStr = Field(min_length=1, max_length=256)


class VerifiedRegisterPayload(StrictPayload):
    registration_token: SecretStr = Field(min_length=50, max_length=512)
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8, max_length=256)


class RefreshPayload(StrictPayload):
    refresh_token: SecretStr = Field(min_length=47, max_length=47)
    request_id: UUID | None = None


class ErasePayload(StrictPayload):
    current_password: SecretStr = Field(min_length=0, max_length=256)
    confirmation: str = Field(max_length=32)
    request_id: UUID | None = None


class ResetPayload(StrictPayload):
    confirmation: str = Field(max_length=32)
    request_id: UUID


class CompleteCredentialsPayload(StrictPayload):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8, max_length=256)
    request_id: UUID


def create_phone_auth_router(service: PhoneAuth):
    router = APIRouter(prefix='/v1/auth', tags=['authentication'], route_class=SafeAuthRoute)
    authenticated = authentication_dependency(service.auth)
    ip = lambda request: request.client.host if request.client else 'unknown'
    from .account_reset11831 import AccountReset
    account_reset = AccountReset(service)

    @router.post('/phone/send')
    def send(payload: SendPayload, request: Request):
        _http_call(service.require_phone_entrypoints)
        principal = authenticated(request) if payload.purpose in {'bind','change'} else None
        return _http_call(service.send, payload.phone, payload.purpose, client_ip=ip(request), principal=principal)

    @router.post('/register/complete', status_code=201)
    def register(payload: RegisterPayload, request: Request):
        return _http_call(service.register_complete, payload.username, payload.password.get_secret_value(), payload.phone,
                          str(payload.challenge_id), payload.code.get_secret_value(), client_ip=ip(request))

    @router.post('/phone/login')
    def login(payload: CodePayload, request: Request):
        return _http_call(service.phone_login, payload.phone, str(payload.challenge_id), payload.code.get_secret_value(), client_ip=ip(request))

    @router.post('/phone/entry')
    def entry(payload: CodePayload, request: Request, response: Response):
        response.headers['Cache-Control']='no-store'
        return _http_call(service.phone_entry, payload.phone, str(payload.challenge_id), payload.code.get_secret_value(), client_ip=ip(request))

    @router.post('/register/verified', status_code=201)
    def verified_register(payload: VerifiedRegisterPayload, request: Request, response: Response):
        response.headers['Cache-Control']='no-store'
        return _http_call(service.register_verified, payload.registration_token.get_secret_value(), payload.username, payload.password.get_secret_value(), client_ip=ip(request))

    @router.post('/phone/quick-entry')
    def quick_entry(payload: CodePayload, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return _http_call(service.credentials.quick_entry, payload.phone, str(payload.challenge_id), payload.code.get_secret_value(), client_ip=ip(request))

    @router.get('/credentials')
    def credentials(request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return _http_call(service.credentials.status, authenticated(request))

    @router.post('/credentials/complete')
    def complete_credentials(payload: CompleteCredentialsPayload, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return _http_call(service.credentials.complete, authenticated(request), payload.username, payload.password.get_secret_value(), str(payload.request_id), client_ip=ip(request))

    @router.get('/phone')
    def info(request: Request):
        return _http_call(service.phone_info, authenticated(request))

    @router.post('/phone/bind')
    def bind(payload: BindPayload, request: Request):
        _http_call(service.require_phone_entrypoints)
        return _http_call(service.bind, authenticated(request), payload.phone, str(payload.challenge_id), payload.code.get_secret_value(),
                          payload.current_password.get_secret_value(), client_ip=ip(request))

    @router.post('/refresh')
    def refresh(payload: RefreshPayload, request: Request):
        return _http_call(service.refresh, payload.refresh_token.get_secret_value(), str(payload.request_id) if payload.request_id else None, client_ip=ip(request))

    @router.post('/account/erase')
    def erase(payload: ErasePayload, request: Request):
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme.casefold() != 'bearer' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise HTTPException(401, '需要重新登录。')
        return _http_call(service.erase, token, str(payload.request_id) if payload.request_id else None,
                          payload.current_password.get_secret_value(), payload.confirmation, client_ip=ip(request))

    @router.post('/account/reset')
    def reset(payload: ResetPayload, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme.casefold() != 'bearer' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise HTTPException(401, '需要重新登录。')
        return _http_call(account_reset.reset, token, str(payload.request_id), payload.confirmation, client_ip=ip(request))

    return router


def provision_developer_member(database, *, confirm: str):
    """Explicit coordinator-only seed, NEVER called by startup or public routes."""
    if confirm != 'provision-approved-phone-exempt-member-88888888':
        raise ValueError('Explicit coordinator confirmation is required')
    display, normalized, password_hash = AuthService._prepare_new_account(DEVELOPER_USERNAME, DEVELOPER_USERNAME)
    with database.transaction() as connection:
        if connection.execute('SELECT id FROM users WHERE username_normalized=?', (normalized,)).fetchone() is not None:
            raise ValueError('Reserved account already exists; no account was overwritten')
        row = AuthService._insert_account(connection, display_username=display, normalized_username=normalized,
                                          password_hash=password_hash, role_code='member', now=timestamp_to_db(utc_now()))
    return {'id': int(row['id']), 'username': DEVELOPER_USERNAME, 'role_code': 'member', 'phone_exempt': True}
