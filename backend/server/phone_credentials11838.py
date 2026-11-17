"""Phone-first entry with an optional, one-time password-login setup.

Existing account and legacy phone-entry payloads are unchanged. Absence of a
marker means an existing account already owns credentials. Personal-data resets
preserve this authentication table; full account deletion removes it by FK.
"""
from __future__ import annotations

import secrets
from uuid import UUID, uuid4

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.security.passwords import normalize_username
from ollama_chat_app.services.auth import AuthService
from .platform_auth import PlatformAuthError, USER_COLUMNS, _safe_operation
from .phone_credentials_schema11838 import TABLE, verify_shape

INTERNAL_PREFIX = '__fawnsee_phone_'


def reserved_username(normalized):
    return normalized == '88888888' or normalized.startswith(INTERNAL_PREFIX)


class PhoneCredentials:
    def __init__(self, phone, *, check_schema=True):
        self.phone, self.auth, self.database = phone, phone.auth, phone.database
        if check_schema:
            verify_shape(self.database)

    @staticmethod
    def state(connection, user_id):
        row = connection.execute('SELECT credentials_set FROM '+TABLE+' WHERE user_id=?', (user_id,)).fetchone()
        return row is None or int(row['credentials_set']) == 1

    @_safe_operation
    def status(self, principal):
        with self.auth.identity(principal), self.database.connect() as connection:
            return {'credentials_set': self.state(connection, principal.user.id)}

    def password_login_allowed(self, row):
        with self.phone.context(user_id=int(row['id']), role_code=str(row['role_code'])), self.database.connect() as connection:
            return self.state(connection, int(row['id']))

    @_safe_operation
    def quick_entry(self, phone, challenge_id, code, *, client_ip):
        self.phone.require_phone_entrypoints()
        phone = self.phone.consume(phone, 'login', challenge_id, code, client_ip=client_ip)
        with self.phone.context(auth_phone=phone), self.database.connect() as connection:
            binding = connection.execute('SELECT user_id FROM phone_auth_bindings WHERE phone=?', (phone,)).fetchone()
        # Hash outside the short writer transaction. Internal names reveal no
        # phone number; the random password is discarded and cannot be chosen.
        prepared = None
        if binding is None:
            with self.auth.security.hash_slot():
                prepared = AuthService._prepare_new_account(INTERNAL_PREFIX+uuid4().hex, secrets.token_urlsafe(48))
        created, now = False, timestamp_to_db(utc_now())
        actor = int(binding['user_id']) if binding else None
        normalized = prepared[1] if prepared else None
        with self.phone.context(auth_phone=phone, user_id=actor, role_code='member' if actor else None,
                                auth_username=normalized, registering=prepared is not None), self.database.transaction() as connection:
            # Recheck under the database's writer lock: two valid SMS proofs
            # for the same phone must converge on the one unique binding.
            binding = connection.execute('SELECT user_id FROM phone_auth_bindings WHERE phone=?', (phone,)).fetchone()
            if binding is None:
                if prepared is None:
                    raise PlatformAuthError(401, '账号状态已改变，请重新验证手机号。')
                display, normalized, password_hash = prepared
                row = AuthService._insert_account(connection, display_username=display, normalized_username=normalized,
                    password_hash=password_hash, role_code='member', now=now)
                actor, created = int(row['id']), True
                with self.phone.context(user_id=actor, role_code='member', auth_phone=phone, auth_username=normalized, registering=True):
                    connection.execute('INSERT INTO phone_auth_bindings(user_id,phone,verified_at) VALUES(?,?,?)', (actor, phone, now))
                    connection.execute('INSERT INTO '+TABLE+'(user_id,credentials_set,created_at,completed_at,setup_request_id) VALUES(?,0,?,NULL,NULL)', (actor, now))
            else:
                actor = int(binding['user_id'])
            with self.phone.context(user_id=actor, role_code='member', auth_phone=phone):
                full = connection.execute('SELECT '+USER_COLUMNS+',password_hash FROM users WHERE id=?', (actor,)).fetchone()
                if full is None or full['role_code'] != 'member':
                    raise PlatformAuthError(401, '手机号或验证码无效，请重新验证。')
                completed = self.state(connection, actor)
                session = self.auth._issue_verified_session(full, verified_phone=phone, client_ip=client_ip)
        return {**session, 'outcome': 'login', 'created': created, 'credentials_set': completed}

    @_safe_operation
    def complete(self, principal, username, password, request_id, *, client_ip):
        if principal.user.role_code != 'member':
            raise PlatformAuthError(403, '此功能只向当前会员账号开放。')
        identifier = str(UUID(str(request_id)))
        normalized = normalize_username(username)
        if reserved_username(normalized):
            raise PlatformAuthError(409, '该账号名称不可用于设置。')
        self.auth.limiter.check(client_ip, normalized)
        with self.auth.security.hash_slot():
            display, normalized, password_hash = AuthService._prepare_new_account(username, password)
        actor, now = principal.user.id, timestamp_to_db(utc_now())
        with self.auth.identity(principal), self.database.transaction() as connection:
            row = connection.execute('SELECT '+USER_COLUMNS+',password_hash FROM users WHERE id=?'+self.auth._lock_suffix(), (actor,)).fetchone()
            marker = connection.execute('SELECT credentials_set,setup_request_id FROM '+TABLE+' WHERE user_id=?'+self.auth._lock_suffix(), (actor,)).fetchone()
            if row is None or row['role_code'] != 'member' or row['account_status'] != 'active':
                raise PlatformAuthError(401, '登录状态已改变，请重新登录。')
            if marker is None or int(marker['credentials_set']) == 1:
                # A lost response may be retried without revoking the only
                # token available to that device. Never treat a new request as
                # a password-reset operation after credentials were completed.
                if (marker is None or marker['setup_request_id'] != identifier
                        or row['username_normalized'] != normalized
                        or not self.auth.security.verify_password(password, str(row['password_hash']))):
                    raise PlatformAuthError(409, '账号密码已设置，请重新打开页面查看。')
                return {**self.auth._issue_verified_session(row, client_ip=client_ip), 'credentials_set': True}
            connection.execute('UPDATE users SET username=?,username_normalized=?,password_hash=?,updated_at=? WHERE id=?', (display, normalized, password_hash, now, actor))
            connection.execute('UPDATE '+TABLE+' SET credentials_set=1,completed_at=?,setup_request_id=? WHERE user_id=? AND credentials_set=0', (now, identifier, actor))
            # Keep this device's active bearer and refresh family valid. Other
            # devices cannot keep using credentials from before this change.
            families = [str(value['family_id']) for value in connection.execute('SELECT DISTINCT family_id FROM phone_auth_refreshes WHERE user_id=? AND access_digest=?', (actor, principal.token_digest)).fetchall()]
            if families:
                placeholders = ','.join('?' for _ in families)
                connection.execute('UPDATE phone_auth_refreshes SET password_marker=? WHERE user_id=? AND family_id IN ('+placeholders+')', (self.phone.digest('password-marker', password_hash), actor, *families))
                connection.execute('UPDATE phone_auth_refreshes SET revoked_at=? WHERE user_id=? AND family_id NOT IN ('+placeholders+') AND revoked_at IS NULL', (int(self.phone.clock()), actor, *families))
            else:
                self.phone.revoke_refresh(connection, actor)
            connection.execute('UPDATE platform_sessions SET revoked_at=? WHERE user_id=? AND token_digest<>? AND revoked_at IS NULL', (now, actor, principal.token_digest))
            full = connection.execute('SELECT '+USER_COLUMNS+',password_hash FROM users WHERE id=?', (actor,)).fetchone()
            return {**self.auth._issue_verified_session(full, client_ip=client_ip), 'credentials_set': True}
