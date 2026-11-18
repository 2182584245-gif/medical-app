"""Backend-only PNVS/SMS. No startup calls, retries, or credential logging.

Wire format follows Aliyun's documented ACS3 fixed signature vector:
https://help.aliyun.com/zh/sdk/product-overview/v3-request-structure-and-signature
SMS uses https://dysmsapi.aliyuncs.com, SendSms, version 2017-05-25.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import stat
from urllib.parse import quote

import httpx
from pydantic import Field
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

ENDPOINT = 'dysmsapi.aliyuncs.com'
PNVS_ENDPOINT = 'dypnsapi.aliyuncs.com'
METADATA = 'http://100.100.100.200'
COMPANY_SIGN = '青岛慢糖创新科技咨询'
COMPANY_TEMPLATE = 'SMS_339017727'
COMPANY_TEMPLATE_TEXT = '您的验证码为：${code}，请勿泄露于他人！'


class SmsUnavailable(RuntimeError):
    """Contains a fixed UI-safe message, never a provider response or request."""


class _ProviderSendLimited(Exception):
    """Internal signal; provider payloads never become user-facing text."""


class SmsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='FAWNSEE_SMS_', env_file=None,
                                      extra='ignore', hide_input_in_errors=True)
    enabled: bool = False
    provider: Literal['pnvs', 'dysms'] = 'pnvs'
    sign_name: str = ''
    template_code: str = ''
    template_text: str = ''
    template_approved: bool = False
    ecs_role: str = ''
    credential_mode: Literal['ecs_ram_role', 'ram_access_key_file'] = 'ecs_ram_role'
    credential_file: str = '/run/sms-secrets/credentials.json'
    pnvs_scheme: str = 'fawnsee-auth-v1'
    pnvs_bind_template_code: str = '100004'
    pnvs_change_template_code: str = '100002'
    timeout_seconds: float = Field(default=10, ge=2, le=20)

    def ready(self, provider: str | None = None) -> bool:
        provider = provider or self.provider
        if provider == 'pnvs':
            # PNVS's pre-provisioned SignName/TemplateCode are selected in its
            # own console. The latest user choice permits the hosted signature;
            # no custom-brand approval flag is required for this provider.
            return (self.enabled and bool(self.sign_name.strip()) and len(self.sign_name) <= 64
                    and not any(c in self.sign_name for c in '\r\n')
                    and bool(re.fullmatch(r'[A-Za-z0-9_]{1,128}', self.template_code))
                    and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,20}', self.pnvs_scheme)))
        # The approval flag is supplied only after the coordinator verifies the
        # actual console/API approval and exact template text. No other brand
        # or hosted default signature is ever an automatic fallback.
        approved_pair = (
            self.sign_name == '凡思' and '凡思' in self.template_text
            or (self.sign_name, self.template_code, self.template_text)
            == (COMPANY_SIGN, COMPANY_TEMPLATE, COMPANY_TEMPLATE_TEXT)
        )
        return (provider == 'dysms' and self.enabled and self.template_approved and approved_pair
                and bool(re.fullmatch(r'SMS_[A-Za-z0-9]{3,64}', self.template_code))
                and '${code}' in self.template_text
                and set(re.findall(r'\$\{([^}]+)\}', self.template_text)) == {'code'})


@dataclass(frozen=True, repr=False)
class Credentials:
    access_key_id: str
    access_key_secret: str
    security_token: str = ''

    def __repr__(self):
        return '<AliyunCredentials hidden>'


def canonical_query(parameters: dict[str, str]) -> str:
    return '&'.join(quote(str(key), safe='-_.~') + '=' + quote(str(value), safe='-_.~')
                    for key, value in sorted(parameters.items()))


def signed_headers(credentials: Credentials, *, host: str, action: str, version: str,
                   query: str, date: str, nonce: str, body: bytes = b'') -> dict[str, str]:
    headers = {'host': host, 'x-acs-action': action, 'x-acs-version': version,
               'x-acs-date': date, 'x-acs-signature-nonce': nonce,
               'x-acs-content-sha256': hashlib.sha256(body).hexdigest()}
    if credentials.security_token:
        headers['x-acs-security-token'] = credentials.security_token
    names = ';'.join(sorted(headers))
    entries = ''.join(key + ':' + headers[key].strip() + '\n' for key in sorted(headers))
    canonical = '\n'.join(('POST', '/', query, entries, names, headers['x-acs-content-sha256']))
    to_sign = 'ACS3-HMAC-SHA256\n' + hashlib.sha256(canonical.encode()).hexdigest()
    signature = hmac.new(credentials.access_key_secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    headers['Authorization'] = ('ACS3-HMAC-SHA256 Credential=' + credentials.access_key_id
                                + ',SignedHeaders=' + names + ',Signature=' + signature)
    return headers


class EcsRoleCredentials:
    """IMDSv2 only, fixed link-local host; no file/AK/default-provider fallback."""
    def __init__(self, role: str = '', *, transport=None, clock=time.time):
        if role and not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', role):
            raise SmsUnavailable('短信服务的服务器身份配置无效。')
        self.role, self.transport, self.clock = role, transport, clock
        self._lock = threading.Lock()
        self._cached, self._expires = None, 0

    def get(self) -> Credentials:
        with self._lock:
            if self._cached and self._expires - self.clock() > 300:
                return self._cached
            try:
                with httpx.Client(transport=self.transport, timeout=3, follow_redirects=False,
                                  trust_env=False) as client:
                    token = client.put(METADATA + '/latest/api/token',
                                       headers={'X-aliyun-ecs-metadata-token-ttl-seconds': '21600'})
                    if token.status_code != 200 or not 1 <= len(token.content) <= 4096:
                        raise ValueError
                    token_text = token.text
                    if any(c in token_text for c in '\r\n'):
                        raise ValueError
                    headers = {'X-aliyun-ecs-metadata-token': token_text}
                    role = self.role
                    if not role:
                        reply = client.get(METADATA + '/latest/meta-data/ram/security-credentials/', headers=headers)
                        if reply.status_code != 200 or len(reply.content) > 256:
                            raise ValueError
                        role = reply.text.strip()
                    if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', role):
                        raise ValueError
                    reply = client.get(METADATA + '/latest/meta-data/ram/security-credentials/' + role, headers=headers)
                    if reply.status_code != 200 or len(reply.content) > 32768:
                        raise ValueError
                    value = reply.json()
                    if value.get('Code') != 'Success':
                        raise ValueError
                    fields = [value.get(key) for key in ('AccessKeyId', 'AccessKeySecret', 'SecurityToken')]
                    if not all(isinstance(v, str) and 8 <= len(v) <= 16384 and not any(c in v for c in '\r\n') for v in fields):
                        raise ValueError
                    expiry = datetime.fromisoformat(value['Expiration'].replace('Z', '+00:00')).timestamp()
                    if expiry <= self.clock() + 60:
                        raise ValueError
                    self._cached, self._expires = Credentials(*fields), expiry
                    return self._cached
            except Exception:
                raise SmsUnavailable('短信服务的服务器身份暂不可用，请稍后再试。') from None


def _runtime_uid():
    method = getattr(os, 'geteuid', None)
    return method() if method is not None else None


def _lstat(path):
    return os.lstat(path)


def _fstat(descriptor):
    return os.fstat(descriptor)


def _open_private(path):
    if not hasattr(os, 'O_NOFOLLOW'):
        raise SmsUnavailable('当前服务器不支持安全读取短信身份文件。')
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))


def validate_private_metadata(info, owner_uid, *, directory=False):
    """POSIX metadata policy, separately testable on non-POSIX workstations."""
    if type(owner_uid) is not int or owner_uid <= 0 or info.st_uid != owner_uid:
        raise ValueError('Invalid private owner')
    mode = stat.S_IMODE(info.st_mode)
    if directory:
        if not stat.S_ISDIR(info.st_mode) or mode not in {0o500,0o700}:
            raise ValueError('Invalid private directory')
    elif (not stat.S_ISREG(info.st_mode) or mode not in {0o400,0o600}
          or info.st_nlink != 1 or not 1 <= info.st_size <= 4096):
        raise ValueError('Invalid private file')


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate credential key')
        result[key] = value
    return result


class RamAccessKeyFile:
    """Explicit server-only file mode, never an IMDS failure fallback.

    Production root is fixed. The root argument exists only for owned temporary
    fixtures; it is not populated from requests or environment variables.
    Values are read per call for controlled atomic rotation and never cached.
    """
    def __init__(self, path='/run/sms-secrets/credentials.json', *, root='/run/sms-secrets'):
        self.path, self.root = Path(path), Path(root)

    def get(self):
        descriptor = None
        try:
            uid = _runtime_uid()
            if uid is None or not self.root.is_absolute() or self.path != self.root / 'credentials.json':
                raise ValueError
            if self.path.resolve(strict=True) != self.path or self.root.resolve(strict=True) != self.root:
                raise ValueError
            for parent in (self.path, *self.path.parents):
                if stat.S_ISLNK(_lstat(parent).st_mode):
                    raise ValueError
            validate_private_metadata(_lstat(self.root), uid, directory=True)
            before = _lstat(self.path)
            validate_private_metadata(before, uid)
            descriptor = _open_private(self.path)
            opened = _fstat(descriptor)
            validate_private_metadata(opened, uid)
            if (before.st_dev,before.st_ino,before.st_size) != (opened.st_dev,opened.st_ino,opened.st_size):
                raise ValueError
            blocks, total = [], 0
            while True:
                block = os.read(descriptor, 4097-total)
                if not block:
                    break
                blocks.append(block);total += len(block)
                if total > 4096:
                    raise ValueError
            after = _fstat(descriptor)
            validate_private_metadata(after, uid)
            if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError
            value = json.loads(b''.join(blocks).decode('utf8'), object_pairs_hook=_unique_json)
            if not isinstance(value,dict) or set(value) != {'access_key_id','access_key_secret'}:
                raise ValueError
            identifier, secret = value['access_key_id'], value['access_key_secret']
            if (not isinstance(identifier,str) or not re.fullmatch(r'LTAI[A-Za-z0-9]{12,124}', identifier)
                or not isinstance(secret,str) or not re.fullmatch(r'[A-Za-z0-9+/=_-]{16,256}', secret)):
                raise ValueError
            return Credentials(identifier, secret)
        except Exception:
            raise SmsUnavailable('短信服务的服务器身份文件暂不可用，请稍后再试。') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


def make_credentials(settings):
    if settings.credential_mode == 'ram_access_key_file':
        return RamAccessKeyFile(settings.credential_file)
    return EcsRoleCredentials(settings.ecs_role)


class AliyunSmsSender:
    def __init__(self, settings: SmsSettings | None = None, *, credentials=None, transport=None):
        self.settings = settings or SmsSettings()
        self.credentials = credentials if credentials is not None else make_credentials(self.settings)
        self.transport = transport

    def send(self, phone: str, code: str, *, challenge_id: str, purpose: str) -> dict:
        if not self.settings.ready('dysms'):
            raise SmsUnavailable('凡思短信签名或模板尚未配置完成，请稍后再试。')
        if not re.fullmatch(r'\+861[3-9][0-9]{9}', phone) or not re.fullmatch(r'[0-9]{6}', code):
            raise SmsUnavailable('短信请求参数无效。')
        if purpose not in {'register', 'login', 'bind', 'change'}:
            raise SmsUnavailable('短信请求用途无效。')
        parameters = {'PhoneNumbers': phone[3:], 'SignName': self.settings.sign_name,
                      'TemplateCode': self.settings.template_code,
                      'TemplateParam': json.dumps({'code': code}, separators=(',', ':')),
                      'OutId': challenge_id}
        return self._request(parameters, host=ENDPOINT, action='SendSms')

    def _request(self, parameters, *, host, action, require_success=False):
        query = canonical_query(parameters)
        headers = signed_headers(self.credentials.get(), host=host, action=action,
                                 version='2017-05-25', query=query,
                                 date=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                 nonce=secrets.token_hex(16))
        # Use the transport directly: httpx.Client logs full request URLs at INFO
        # and RPC query parameters contain the phone/code. Never emit that URL.
        transport = self.transport or httpx.HTTPTransport(retries=0, verify=True, trust_env=False)
        try:
            request = httpx.Request('POST', 'https://' + host + '/?' + query, headers=headers,
                                    content=b'', extensions={'timeout': {
                                        key: self.settings.timeout_seconds for key in ('connect', 'read', 'write', 'pool')}})
            reply = transport.handle_request(request)
            try:
                raw = bytearray()
                for block in reply.iter_bytes():
                    raw.extend(block)
                    if len(raw) > 65536:
                        raise ValueError
                if reply.status_code == 429:
                    raise _ProviderSendLimited
                value = json.loads(raw)
                if value.get('Code') in {'BUSINESS_LIMIT_CONTROL', 'FREQUENCY_FAIL'}:
                    raise _ProviderSendLimited
                if reply.status_code != 200 or value.get('Code') != 'OK' or require_success and value.get('Success') is not True:
                    raise ValueError
                return {'accepted': True}
            finally:
                reply.close()
        except _ProviderSendLimited:
            raise SmsUnavailable('短信服务商暂时限制了本次发送，请稍后再试。') from None
        except Exception:
            # SendSms has no idempotency guarantee. Never automatically retry a
            # timeout or return upstream text that might contain personal data.
            raise SmsUnavailable('凡思验证码暂未确认发送成功，请稍后再试。') from None
        finally:
            if self.transport is None:
                transport.close()


class AliyunPnvsSender(AliyunSmsSender):
    """PNVS hosted signature, with backend-generated HMAC-verified code.

    SendSmsVerifyCode explicitly permits literal TemplateParam code values.
    Such custom codes cannot use CheckSmsVerifyCode; the existing backend
    challenge ledger performs the authoritative one-use verification instead.
    https://help.aliyun.com/zh/pnvs/developer-reference/api-dypnsapi-2017-05-25-sendsmsverifycode
    """
    def send(self, phone: str, code: str, *, challenge_id: str, purpose: str) -> dict:
        if not self.settings.ready('pnvs'):
            raise SmsUnavailable('短信认证服务尚未配置完成，请稍后再试。')
        if not re.fullmatch(r'\+861[3-9][0-9]{9}', phone) or not re.fullmatch(r'[0-9]{6}', code):
            raise SmsUnavailable('短信请求参数无效。')
        if purpose not in {'register','login','bind','change'}:
            raise SmsUnavailable('短信请求用途无效。')
        template = {'bind':self.settings.pnvs_bind_template_code,
                    'change':self.settings.pnvs_change_template_code}.get(purpose, self.settings.template_code)
        if not re.fullmatch(r'[A-Za-z0-9_]{1,128}', template):
            raise SmsUnavailable('此用途的短信认证模板尚未配置完成。')
        parameters = {'CountryCode':'86', 'PhoneNumber':phone[3:],
                      'SignName':self.settings.sign_name, 'TemplateCode':template,
                      'TemplateParam':json.dumps({'code':code,'min':'5'}, separators=(',',':')),
                      'SchemeName':self.settings.pnvs_scheme, 'OutId':challenge_id,
                      # Aliyun's official integration example uses interval=1.
                      # Omitting it restores the provider's 60-second default.
                      # Daily/risk controls still belong to the provider.
                      'CodeLength':'6', 'ValidTime':'300', 'Interval':'1', 'DuplicatePolicy':'1',
                      'ReturnVerifyCode':'false', 'AutoRetry':'0'}
        return self._request(parameters, host=PNVS_ENDPOINT, action='SendSmsVerifyCode', require_success=True)


def make_sms_sender(settings: SmsSettings | None = None, **kwargs):
    settings = settings or SmsSettings()
    return (AliyunPnvsSender if settings.provider == 'pnvs' else AliyunSmsSender)(settings, **kwargs)
