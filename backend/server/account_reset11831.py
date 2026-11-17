"""Reset one member's personal data, preserving their account and phone login.

The private transaction deletes only a fixed reviewed owner list. A successful
reset transparently replaces device credentials; the database write fence then
rejects old, already-authenticated requests waiting behind this transaction.
An encrypted short-lived session receipt and a minimal completion tombstone
make a lost response safe to retry without deleting data written afterwards.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime
from uuid import UUID

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from .account_erasure import AUDIT_ENTITIES, audit_owner_predicate
from .account_reset_schema11831 import PERSONAL_OWNERS, RECEIPT_TABLE, verify_shape
from .platform_auth import PlatformAuthError, USER_COLUMNS, _safe_operation

CONFIRMATION = '清空账号内全部数据'
RECEIPT_SECONDS = 86400
# These later owner tables already grant ordinary owner DELETE. Keep the
# historical migration generator byte-identical; no new runtime DDL or grants.
EXTRA_PERSONAL_OWNERS = (('member_cart','user_id'),('member_favorites','user_id'),
    ('member_usage_sessions11841','user_id'),('member_account_metadata11841','user_id'))
CLEAR_PERSONAL_OWNERS = PERSONAL_OWNERS + EXTRA_PERSONAL_OWNERS


class AccountReset:
    def __init__(self, phone_auth):
        self.phone, self.auth = phone_auth, phone_auth.auth
        self.db = self.auth.database
        self.postgres = getattr(self.db, 'dialect', 'sqlite') == 'postgresql'
        self.key = hmac.new(phone_auth.key, b'account-reset11831-envelope', hashlib.sha256).digest()

    def receipt_id(self, token, identifier):
        return self.phone.digest('account-reset11831', token, identifier)

    def _encrypt(self, value, receipt):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = secrets.token_bytes(12)
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        return base64.b64encode(nonce + AESGCM(self.key).encrypt(nonce, raw, receipt.encode())).decode('ascii')

    def _decrypt(self, value, receipt):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(value, validate=True)
        return json.loads(AESGCM(self.key).decrypt(raw[:12], raw[12:], receipt.encode()))

    def _receipt(self, connection, receipt, old_digest, now):
        row=connection.execute('SELECT * FROM '+RECEIPT_TABLE+' WHERE receipt_digest=? AND old_token_digest=?', (receipt,old_digest)).fetchone()
        if row is None:return None
        return row

    def _recover(self, row, receipt, identifier, now):
        actor=int(row['user_id'])
        from .account_data_generation11853 import account_data_state
        with self.phone.context(user_id=actor,role_code='member'),self.db.connect()as connection:
            generation=account_data_state(connection,actor,self.auth.settings.token_pepper.get_secret_value())['data_generation']
        reauthenticate={'reset':True,'request_id':identifier,'local_purge_required':True,
                        'reauthentication_required':True,'session':None,'data_generation':generation}
        if int(row['expires_at'])<=now or row['response_envelope'] is None:
            # A completed reset stays provable after a long offline period.
            # Credentials are removed by the ordinary ephemeral-data sweep.
            if row['response_envelope'] is not None:
                with self.phone.context(token_digest=row['old_token_digest']),self.db.transaction() as connection:
                    connection.execute('UPDATE '+RECEIPT_TABLE+' SET new_token_digest=NULL,password_marker=NULL,response_envelope=NULL WHERE receipt_digest=? AND expires_at<=?',(receipt,now))
            return reauthenticate
        # Receipt HMAC is bound to the original opaque token and request UUID.
        # Internal context is derived from that receipt, never a request actor.
        with self.phone.context(user_id=actor, role_code='member'), self.db.connect() as connection:
            current=connection.execute('SELECT password_hash,role_code,account_status FROM users WHERE id=?',(actor,)).fetchone()
            session=connection.execute('SELECT user_id FROM platform_sessions WHERE user_id=? AND token_digest=? AND revoked_at IS NULL AND expires_at>?',
                (actor,row['new_token_digest'],timestamp_to_db(utc_now()))).fetchone()
            if (current is None or current['role_code']!='member' or current['account_status']!='active' or session is None
                    or not hmac.compare_digest(self.phone.digest('password-marker',str(current['password_hash'])),str(row['password_marker']))):
                return reauthenticate
        result=self._decrypt(row['response_envelope'],receipt)
        if (result.get('request_id')!=identifier or result.get('reset') is not True
                or result.get('session',{}).get('user',{}).get('id')!=actor
                or not hmac.compare_digest(self.auth.security.token_digest(result['session']['access_token']),row['new_token_digest'])):
            raise PlatformAuthError(503, '清空结果暂不可用，请稍后重试。')
        # Old encrypted receipts can predate generation metadata. The live
        # replacement session above is still checked before decorating them.
        result['data_generation']=generation
        result['session']['data_generation']=generation
        return result

    def _clear(self, connection, actor):
        # The shape check precedes any deletion. Optional future unknown tables
        # are never guessed from request input and no identity row is deleted.
        required={name for name,_ in CLEAR_PERSONAL_OWNERS}|{'audit_logs','expansion_referrals'}
        required|={name for name,_ in AUDIT_ENTITIES.values()}|{'messages','report_items','chat_attachments','user_file_contents','visit_task_details'}
        for name in required:connection.execute('SELECT 1 FROM '+name+' LIMIT 0')
        predicate=audit_owner_predicate(str(actor))
        connection.execute('DELETE FROM audit_logs AS a WHERE '+predicate)
        for name,column in CLEAR_PERSONAL_OWNERS:
            connection.execute('DELETE FROM '+name+' WHERE '+column+'=?',(actor,))
        connection.execute('DELETE FROM expansion_referrals WHERE inviter_user_id=? OR invitee_user_id=?',(actor,actor))
        connection.execute('DELETE FROM phone_auth_challenges WHERE user_id=? OR phone IN (SELECT phone FROM phone_auth_bindings WHERE user_id=?)',(actor,actor))

    def _owner_completion(self,connection,actor,identifier):
        """A reauthenticated owner can acknowledge an old reset without wiping.

        The actor is always a resolved principal, never a request field. A new
        bearer cannot use its different HMAC as authority for a second clear
        of the same (owner, request UUID). No old credential is disclosed.
        """
        row=connection.execute('SELECT request_id FROM '+RECEIPT_TABLE+' WHERE user_id=? AND request_id=?',(actor,identifier)).fetchone()
        if row is None:return None
        from .account_data_generation11853 import account_data_state
        generation=account_data_state(connection,actor,self.auth.settings.token_pepper.get_secret_value())['data_generation']
        return {'reset':True,'request_id':identifier,'local_purge_required':True,
                'reauthentication_required':True,'session':None,'data_generation':generation}

    @_safe_operation
    def reset(self, token, request_id, confirmation, *, client_ip):
        if confirmation!=CONFIRMATION:
            raise PlatformAuthError(422, '请确认清空账号内全部数据。')
        if (type(request_id) is not str or type(token) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token)):
            raise PlatformAuthError(422, '清空请求无效，请重试。')
        try:
            identifier=str(UUID(request_id))
            if identifier!=request_id:raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise PlatformAuthError(422, '清空请求无效，请重试。') from None
        receipt=self.receipt_id(token,identifier)
        old_digest=self.auth.security.token_digest(token)
        now=int(self.phone.clock())
        verify_shape(self.db)
        with self.phone.context(token_digest=old_digest), self.db.connect() as connection:
            previous=self._receipt(connection,receipt,old_digest,now)
        if previous is not None:return self._recover(previous,receipt,identifier,now)
        # No password prompt: the live authenticated member and confirmation
        # authorize this fixed self-reset, not account erasure.
        try:
            principal=self.auth.resolve_token(token)
        except PlatformAuthError as error:
            if error.status_code==401:
                with self.phone.context(token_digest=old_digest), self.db.connect() as connection:
                    previous=self._receipt(connection,receipt,old_digest,now)
                if previous is not None:return self._recover(previous,receipt,identifier,now)
            raise
        if principal.user.role_code!='member':
            raise PlatformAuthError(403,'仅可清空当前会员账号的数据。')
        actor=principal.user.id
        with self.auth.identity(principal),self.db.connect()as connection:
            completed=self._owner_completion(connection,actor,identifier)
        if completed is not None:return completed
        # The normal authentication budget and the database's one write lock
        # bound repeated resets. Exact completed retries do not mutate again.
        self.phone.budget(client_ip,receipt,operation='reset')
        try:
            with self.auth.identity(principal), self.db.transaction() as connection:
                previous=self._receipt(connection,receipt,old_digest,now)
                if previous is not None:return self._recover(previous,receipt,identifier,now)
                current=connection.execute('SELECT '+USER_COLUMNS+',password_hash FROM users WHERE id=?'+self.auth._lock_suffix(),(actor,)).fetchone()
                live=connection.execute('SELECT user_id FROM platform_sessions WHERE token_digest=? AND user_id=? AND revoked_at IS NULL AND expires_at>?',
                    (old_digest,actor,timestamp_to_db(utc_now()))).fetchone()
                if current is None or live is None or current['role_code']!='member' or current['account_status']!='active':
                    raise PlatformAuthError(401,'登录状态已改变，请重新登录。')
                completed=self._owner_completion(connection,actor,identifier)
                if completed is not None:return completed
                binding=connection.execute('SELECT phone FROM phone_auth_bindings WHERE user_id=?',(actor,)).fetchone()
                bound_phone=str(binding['phone']) if binding is not None else None
                with self.phone.context(user_id=actor,role_code='member',token_digest=old_digest,auth_phone=bound_phone,erasing=True):
                    self._clear(connection,actor)
                    stamp=timestamp_to_db(utc_now())
                    connection.execute('UPDATE platform_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',(stamp,actor))
                    self.phone.revoke_refresh(connection,actor)
                    expires=now+RECEIPT_SECONDS
                    connection.execute('UPDATE '+RECEIPT_TABLE+' SET new_token_digest=NULL,password_marker=NULL,response_envelope=NULL WHERE user_id=? OR expires_at<=?',(actor,now))
                    # Install the permanent tombstone before issuing the new
                    # session, so its generation includes this exact reset.
                    # The transaction hides these nullable credential fields
                    # until the complete encrypted receipt is committed.
                    connection.execute('INSERT INTO '+RECEIPT_TABLE+'('+','.join(('receipt_digest','user_id','request_id','old_token_digest','new_token_digest','password_marker','response_envelope','completed_at','expires_at'))+') VALUES(?,?,?,?,NULL,NULL,NULL,?,?)',
                        (receipt,actor,identifier,old_digest,now,expires))
                    # Standard envelope, with the same identity/password/binding.
                    # This nested trusted authentication context has no old
                    # token, so it can install the replacement session atomically.
                    session=self.auth._issue_verified_session(current)
                    answer={'reset':True,'request_id':identifier,'local_purge_required':True,'session':session,
                            'data_generation':session['data_generation']}
                    new_digest=self.auth.security.token_digest(session['access_token'])
                    connection.execute('UPDATE '+RECEIPT_TABLE+' SET new_token_digest=?,password_marker=?,response_envelope=? WHERE receipt_digest=? AND user_id=?',
                        (new_digest,self.phone.digest('password-marker',str(current['password_hash'])),self._encrypt(answer,receipt),receipt,actor))
        except PlatformAuthError as error:
            # Another identical request can finish while this request waits for
            # the global write fence; its now-revoked old token is recoverable.
            if error.status_code==401:
                with self.phone.context(token_digest=old_digest), self.db.connect() as connection:
                    previous=self._receipt(connection,receipt,old_digest,now)
                if previous is not None:return self._recover(previous,receipt,identifier,now)
            raise
        return answer
