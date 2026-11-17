"""Small synchronous cloud API client; desktop callers must use a UI worker."""

from __future__ import annotations

import ipaddress
import json as json_module
import re
import sys
from threading import RLock
from contextlib import contextmanager
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from .offline_access import OfflineAccessError, validate_lease

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024
_MESSAGES = {
    "configuration": "云端服务地址无效，请使用有可信证书的 HTTPS 域名或公网 IP。",
    "worker_required": "云端操作需要在后台执行，请稍后重试。",
    "closed": "云端连接已关闭，请重新连接。",
    "account_changed": "账号已变更，本次结果未用于当前账号。",
    "account_data_reset": "这份账号资料已失效，请重新读取云端资料。",
    "account_verification_required": "账号资料尚未核验，请联网后重试；旧资料暂不使用。",
    "not_authenticated": "请先登录云端账户。",
    "authentication": "用户名或密码错误，或登录已失效，请重新登录。",
    "permission": "当前账户没有执行此操作的权限。",
    "feature_disabled": "此板块暂未开放；离线修改仍保留为待处理，开放后可重新确认提交。",
    "not_found": "请求的数据不存在，或当前账户无权访问。",
    "conflict": "数据发生冲突，请刷新后确认；不要重复提交新的操作。",
    "validation": "提交内容不符合要求，请检查输入及文件大小。",
    "limited": "请求过于频繁，请稍后再试。",
    "unavailable": "云端服务暂不可用；支持离线保存的资料会保留在本机待提交。",
    "timeout": "等待云端响应超时；请查看同步状态，不要重复新增同一条记录。",
    "network": "暂时无法安全连接云端，请检查网络及系统时间，不要关闭证书验证。",
    "redirect": "云端返回了重定向，已停止发送，请核对服务地址。",
    "protocol": "云端响应格式不正确，请稍后重试或更新应用。",
    "limit": "返回数据超过本次安全读取上限，请缩小查询范围。",
    "offline": "当前为离线访问；此操作需要在线重新登录。",
    "offline_expired": "离线授权已到期或系统时间异常，请在线重新登录。",
}


class CloudAPIError(RuntimeError):
    """Contains only fixed messages and safe metadata; never a response/request object."""

    def __init__(
        self, code: str, *, status_code: int | None = None, outcome_uncertain: bool = False,
        conflict_kind: str = "unknown",
    ) -> None:
        self.code = code if code in _MESSAGES else "protocol"
        self.status_code = status_code
        self.outcome_uncertain = outcome_uncertain
        self.conflict_kind = (conflict_kind if conflict_kind in {"base_version", "idempotency"}
                              else "unknown")
        super().__init__(_MESSAGES[self.code])


def validate_base_url(value: str, *, allow_loopback_http: bool = False) -> str:
    """Validate syntax without DNS/network access; no credentials/query/fragment allowed."""
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        if value != value.strip() or any(ord(c) < 33 for c in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.query or parsed.fragment or "?" in value or "#" in value:
            raise ValueError
        host = parsed.hostname or ""
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        loopback = host == "localhost" or (address is not None and address.is_loopback)
        if loopback:
            if not allow_loopback_http or parsed.scheme not in {"http", "https"}:
                raise ValueError
        else:
            if parsed.scheme != "https":
                raise ValueError
            if address is not None:
                if not address.is_global:
                    raise ValueError
                # A globally routable IP is usable only if the TLS certificate
                # actually contains that IP SAN. httpx verify=True enforces it.
                labels = []
            else:
                labels = host.split(".")
            if address is None and (len(labels) < 2 or len(host) > 253):
                raise ValueError
            if any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", item) for item in labels
            ):
                raise ValueError
            if address is None and (
                not re.search(r"[a-z]", labels[-1])
                or labels[-1]
                in {
                    "localhost",
                    "local",
                    "internal",
                    "lan",
                    "home",
                    "invalid",
                    "test",
                }
            ):
                raise ValueError
        path = parsed.path.rstrip("/")
        if path and (
            not path.startswith("/")
            or any(
                not re.fullmatch(r"[A-Za-z0-9_.~-]+", part) or part in {".", ".."}
                for part in path[1:].split("/")
            )
        ):
            raise ValueError
        return value.rstrip("/")
    except Exception:
        raise CloudAPIError("configuration") from None


def _safe_path(path: str) -> None:
    if not isinstance(path, str) or len(path) > 1024 or not path.startswith("/"):
        raise CloudAPIError("configuration")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.~-]+", part) or part in {".", ".."}
        for part in path[1:].split("/")
    ):
        raise CloudAPIError("configuration")


class CloudAPIClient:
    """Contract for the desktop's explicit cloud mode.

    Auth endpoints are /v1/auth/register, login, me, logout, change-password,
    and account-bound reset-password.
    register()/login()/me() return the server's safe User dictionary; login
    keeps the bearer token ONLY in this object and never returns it. Password
    changes use current_password/new_password and preserve the current token.
    The optional recovery store keeps only the server-signed reset capability,
    protected by the current Windows user, so an application restart can still
    offer the safe reset flow without persisting a password.
    logout() clears local token/user even if remote revocation fails (the error
    then accurately reports that remote logout was not confirmed).

    request(method, path, json=..., params=..., authenticated=True) is the sole
    generic JSON transport. It accepts same-base relative paths, never follows
    redirects, never retries any request, and enforces TLS. Callers may repeat
    an idempotent request only with the SAME explicit request_id after checking
    its outcome; a timeout does not imply the write was rolled back.

    rpc(service, method, args, kwargs) sends encoded arguments and returns the
    still-encoded result. The facade owns codec conversion. No API keys or
    local AI provider objects belong in RPC arguments. All network methods are
    synchronous and must run in a Qt worker, not the Qt GUI thread.
    """

    uses_network = True

    def __init__(
        self,
        base_url: str,
        *,
        allow_loopback_http: bool = False,
        transport: httpx.BaseTransport | None = None,
        offline_store=None,
        recovery_store=None,
        recovery_name: str = "platform-auth-recovery",
    ) -> None:
        self.base_url = validate_base_url(base_url, allow_loopback_http=allow_loopback_http)
        self._lock = RLock()
        self._auth_lock = RLock()
        self._token: str | None = None
        # A server-signed, account-bound recovery capability.  The WebView
        # release supplies the existing Windows DPAPI SecretStore so this
        # capability survives a restart without ever persisting a password.
        self._recovery_store = recovery_store
        self._recovery_name = recovery_name
        self._recovery_token: str | None = self._load_recovery_token()
        self._user: dict | None = None
        self._session_serial = 0
        self._application_settings_revision = None
        self._configured_read_timeout = 90.0
        self.offline_store = offline_store
        self._offline_lease = None
        self._offline_session = False
        self.offline_opt_in = False
        self.sync_protocol = 1
        self._closed = False
        try:
            self._http = httpx.Client(
                verify=True,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                transport=transport,
            )
        except Exception:
            raise CloudAPIError("configuration") from None

    def _load_recovery_token(self) -> str | None:
        if self._recovery_store is None:
            return None
        try:
            value = self._recovery_store.get_persistent_secret(self._recovery_name)
        except Exception:
            return None
        return value if isinstance(value, str) and re.fullmatch(
            r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value
        ) else None

    def _save_recovery_token(self, value: str | None) -> None:
        self._recovery_token = value
        if self._recovery_store is None:
            return
        try:
            if value:
                self._recovery_store.save_persistent_secret(self._recovery_name, value)
            else:
                self._recovery_store.delete_persistent_secret(self._recovery_name)
        except Exception:
            # Login remains usable if this optional local persistence layer is
            # unavailable; the server still enforces the signed token.
            return

    @property
    def is_authenticated(self) -> bool:
        with self._lock:
            if self._token is not None:
                return True
            if self._offline_session:
                try:
                    validate_lease(self._offline_lease)
                    return True
                except OfflineAccessError:
                    return False
            return False

    @property
    def is_online_authenticated(self):
        return self._token is not None

    @property
    def session_serial(self):
        """An opaque in-process change counter, never the authentication token."""
        return self._session_serial

    def apply_session_settings(self, revision: int, timeout_seconds: int):
        if type(revision) is not int or revision < 0:
            raise CloudAPIError("validation")
        if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 120:
            raise CloudAPIError("validation")
        with self._lock:
            self._application_settings_revision = revision
            self._configured_read_timeout = float(timeout_seconds)
            self._http.timeout = httpx.Timeout(
                connect=10.0, read=float(timeout_seconds), write=30.0, pool=10.0)

    @property
    def is_offline_session(self):
        return self._offline_session

    @property
    def offline_lease(self):
        return dict(self._offline_lease) if self._offline_lease else None

    @staticmethod
    def _on_gui_thread() -> bool:
        qt = sys.modules.get("PySide6.QtCore")
        if qt is not None:
            application = qt.QCoreApplication.instance()
            if application is not None and qt.QThread.currentThread() == application.thread():
                return True
        return False

    def clear_session(self, *, clear_recovery: bool = True) -> None:
        """Clear bearer/offline state; optionally retain the account recovery capability.

        A successful login issues a short-lived, account-bound recovery capability so
        the password-reset dialog can be used after the user signs out.  It is safe to
        retain across an explicit logout because the server binds it to the account's
        current password hash and rejects it for every other username.  All other
        session resets (new login, reset success, fatal auth errors) use the default
        and clear it.
        """
        with self._lock:
            self._session_serial += 1
            self._application_settings_revision = None
            self._token = None
            if clear_recovery:
                self._save_recovery_token(None)
            self._user = None
            self._offline_lease = None
            self._offline_session = False
            self.offline_opt_in = False
            self._http.cookies.clear()

    def _request_snapshot(self, authenticated):
        with self._lock:
            if self._closed:
                raise CloudAPIError("closed")
            if authenticated and self._token is None:
                raise CloudAPIError("offline" if self._offline_session else "not_authenticated")
            return (self._session_serial, self._token, self._application_settings_revision)

    def _request_current(self, snapshot, *, outcome_uncertain=False):
        with self._lock:
            if self._closed or self._session_serial != snapshot[0] or self._token != snapshot[1]:
                raise CloudAPIError("account_changed", outcome_uncertain=outcome_uncertain)

    @contextmanager
    def _stream_response(self, method, url, **kwargs):
        # httpx pools can serve worker threads concurrently. Only building the
        # immutable request uses the session lock; socket I/O never holds it.
        with self._lock:
            if self._closed:
                raise CloudAPIError("closed")
            request = self._http.build_request(method, url, **kwargs)
            # Every API call uses explicit bearer headers. Never carry a cookie
            # jar from another response, including concurrent responses.
            request.headers.pop("cookie", None)
        response = self._http.send(request, stream=True)
        try:
            yield response
        finally:
            response.close()
            with self._lock:
                self._http.cookies.clear()

    def request(
        self, method: str, path: str, *, json=None, params=None, authenticated: bool = True, timeout=None
    ):
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation

            return run_cloud_operation(
                self.request, method, path, json=json, params=params, authenticated=authenticated,
                timeout=timeout,
            )
        _safe_path(path)
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise CloudAPIError("validation")
        write = method != "GET"
        try:
            body = (
                None
                if json is None
                else json_module.dumps(
                    json, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            )
            if body is not None and len(body) > MAX_REQUEST_BYTES:
                raise CloudAPIError("validation")
        except CloudAPIError:
            raise
        except Exception:
            raise CloudAPIError("validation") from None
        snapshot = self._request_snapshot(authenticated)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if authenticated:
            headers["Authorization"] = "Bearer " + snapshot[1]
            if path.startswith("/v1/ai/") and snapshot[2] is not None:
                headers["X-Application-Settings-Revision"] = str(snapshot[2])
        transport_options = {"timeout": timeout} if timeout is not None else {}
        try:
            with self._stream_response(
                method, self.base_url + path, content=body, params=params, headers=headers, **transport_options
            ) as response:
                self._request_current(snapshot, outcome_uncertain=write)
                status = response.status_code
                if 300 <= status < 400:
                    raise CloudAPIError("redirect", status_code=status)
                if not 200 <= status < 300:
                    detail = None
                    if status in {401,403,409}:
                        # Only fixed feature-denial codes avoid revoking the
                        # account's separate offline authorization. Never echo
                        # a server-supplied message or accept arbitrary codes.
                        denied_body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=1024):
                            if len(denied_body) + len(chunk) > 4096:
                                denied_body.clear()
                                break
                            denied_body.extend(chunk)
                        try:
                            detail = json_module.loads(denied_body).get("detail")
                        except (ValueError, TypeError, AttributeError):
                            detail = None
                        if isinstance(detail,str)and detail in {'account_data_reset','account_verification_required'}:
                            self._request_current(snapshot)
                            raise CloudAPIError(detail,status_code=status)
                    if status == 403:
                        if isinstance(detail, str) and detail in {
                            "feature_disabled_today", "feature_disabled_ai",
                            "feature_disabled_statistics", "feature_disabled_services",
                            "feature_disabled_commerce", "feature_disabled_appointments",
                        }:
                            raise CloudAPIError("feature_disabled", status_code=status)
                    if status in {401, 403} and authenticated:
                        with self._lock:
                            self._request_current(snapshot, outcome_uncertain=write)
                            self.revoke_offline_access()
                            if status == 401:
                                self.clear_session()
                    code = {
                        400: "validation",
                        401: "authentication",
                        403: "permission",
                        404: "not_found",
                        409: "conflict",
                        413: "validation",
                        422: "validation",
                        429: "limited",
                    }.get(status, "unavailable")
                    conflict_kind = "unknown"
                    if status == 409:
                        # Only fixed server codes influence replay safety.
                        # Bound the untrusted body and never retain or show it.
                        conflict_kind = {
                            "offline_base_version_changed": "base_version",
                            "request_conflict": "idempotency",
                        }.get(detail, "unknown")if isinstance(detail,str)else"unknown"
                    raise CloudAPIError(
                        code, status_code=status, outcome_uncertain=write and status >= 500,
                        conflict_kind=conflict_kind,
                    )
                if status == 204:
                    self._request_current(snapshot, outcome_uncertain=write)
                    return None
                content_type = response.headers.get("content-type", "").split(";")[0]
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise CloudAPIError("protocol", outcome_uncertain=write)
                chunks = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise CloudAPIError("limit", outcome_uncertain=write)
                    chunks.extend(chunk)
                try:
                    result = json_module.loads(chunks)
                    self._request_current(snapshot, outcome_uncertain=write)
                    return result
                except CloudAPIError:
                    raise
                except Exception:
                    raise CloudAPIError("protocol", outcome_uncertain=write) from None
        except CloudAPIError:
            raise
        except httpx.TimeoutException:
            raise CloudAPIError("timeout", outcome_uncertain=write) from None
        except httpx.HTTPError:
            raise CloudAPIError("network", outcome_uncertain=write) from None
        except Exception:
            raise CloudAPIError("protocol", outcome_uncertain=write) from None
    def ai_stream(self, payload: dict, *, cancel_event=None):
        """Bounded fixed-route SSE; no retries, redirects, raw errors or API keys."""
        if self._on_gui_thread():
            raise CloudAPIError("worker_required")
        raw = json_module.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > 32 * 1024 * 1024:
            raise CloudAPIError("validation")
        snapshot = self._request_snapshot(True)
        headers = {"Authorization": "Bearer " + snapshot[1], "Content-Type": "application/json"}
        if snapshot[2] is not None:
            headers["X-Application-Settings-Revision"] = str(snapshot[2])
        try:
            with self._stream_response("POST", self.base_url + "/v1/ai/chat/stream",
                                   content=raw, headers=headers,
                                   timeout=httpx.Timeout(
                                       max(self._configured_read_timeout,210.0)
                                       if payload.get('thinking',{}).get('type')=='enabled'
                                       else self._configured_read_timeout,
                                       connect=10.0)) as response:
                self._request_current(snapshot)
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    if status in {401, 403}:
                        with self._lock:
                            self._request_current(snapshot)
                            self.revoke_offline_access()
                            if status == 401:
                                self.clear_session()
                    raise CloudAPIError({401: "authentication", 403: "permission",
                                         413: "validation", 422: "validation", 429: "limited"}
                                        .get(status, "redirect" if 300 <= status < 400
                                             else "unavailable"), status_code=status)
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    raise CloudAPIError("protocol")
                total, buffer = 0, bytearray()
                # Deliver SSE transport chunks immediately instead of
                # holding the first answer until a 1 KiB buffer fills.
                for chunk in response.iter_bytes():
                    self._request_current(snapshot)
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    total += len(chunk)
                    if total > 128 * 1024 * 1024:
                        raise CloudAPIError("limit")
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        if line.startswith(b"data: "):
                            event = json_module.loads(line[6:])
                            if not isinstance(event, dict):
                                raise CloudAPIError("protocol")
                            self._request_current(snapshot)
                            yield event
                    if len(buffer) > 256 * 1024:
                        raise CloudAPIError("limit")
        except CloudAPIError:
            raise
        except httpx.TimeoutException:
            raise CloudAPIError("timeout") from None
        except httpx.HTTPError:
            raise CloudAPIError("network") from None
        except Exception:
            raise CloudAPIError("protocol") from None
    @staticmethod
    def _user_view(value) -> dict:
        fields = {
            "id",
            "username",
            "username_normalized",
            "created_at",
            "last_login_at",
            "role_code",
            "account_status",
            "updated_at",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise CloudAPIError("protocol")
        return dict(value)

    def register(self, username: str, password: str) -> dict:
        return self._user_view(
            self.request(
                "POST",
                "/v1/auth/register",
                json={"username": username, "password": password},
                authenticated=False,
            )
        )

    def login(
        self, username: str, password: str, *, allow_offline=False, remember_offline=False
    ) -> dict:
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation

            return run_cloud_operation(
                self.login, username, password, allow_offline=allow_offline,
                remember_offline=remember_offline,
            )
        with self._auth_lock:
            with self._lock:
                self.clear_session()
                snapshot = self._request_snapshot(False)
            try:
                result = self.request(
                    "POST", "/v1/auth/login",
                    json={"username": username, "password": password}, authenticated=False,
                )
            except CloudAPIError as error:
                self._request_current(snapshot)
                if (
                    error.code in {"authentication", "permission"}
                    and self.offline_store is not None
                ):
                    self.offline_store.revoke(self.base_url, username)
                if (
                    allow_offline and self.offline_store is not None
                    and error.code in {"network", "timeout", "unavailable"}
                ):
                    try:
                        user, lease = self.offline_store.authenticate(
                            self.base_url, username, password
                        )
                    except OfflineAccessError:
                        raise CloudAPIError("offline_expired") from None
                    with self._lock:
                        self._request_current(snapshot)
                        self._user = self._user_view(user)
                        self._offline_lease, self._offline_session = lease, True
                        self.offline_opt_in = True
                        return dict(self._user)
                raise
            with self._lock:
                self._request_current(snapshot)
                if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
                    raise CloudAPIError("protocol")
                token = result["access_token"]
                if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,4096}", token):
                    raise CloudAPIError("protocol")
                if result.get("token_type", "bearer") != "bearer":
                    raise CloudAPIError("protocol")
                user = self._user_view(result.get("user"))
                lease = result.get("offline_lease")
                if lease is not None:
                    try:
                        self._offline_lease = validate_lease(lease, actor_id=user["id"])
                    except OfflineAccessError:
                        raise CloudAPIError("protocol") from None
                if remember_offline and self.offline_store is not None and lease is not None:
                    self.offline_store.enroll(self.base_url, user, password, lease)
                    self.offline_opt_in = True
                self._token, self._user = token, user
                recovery_token = result.get("recovery_token")
                self._save_recovery_token(
                    recovery_token
                    if isinstance(recovery_token, str)
                    and 32 <= len(recovery_token) <= 2048
                    and re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", recovery_token)
                    else None
                )
                self.sync_protocol = 2 if result.get("sync_protocol") == 2 else 1
                return dict(user)
    def developer_login(self, username: str, password: str) -> dict:
        """Fresh password verification for a separate developer-only client.

        This never enrolls offline credentials and cannot reuse a normal login.
        Only the server may issue the step-up marker; no username bypass exists.
        """
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation
            return run_cloud_operation(self.developer_login, username, password)
        with self._auth_lock:
            with self._lock:
                self.clear_session()
                snapshot = self._request_snapshot(False)
            result = self.request(
                "POST", "/v1/developer/login", authenticated=False,
                json={"username": username, "password": password},
            )
            with self._lock:
                self._request_current(snapshot)
                if not isinstance(result, dict) or result.get("developer") is not True:
                    raise CloudAPIError("permission")
                token = result.get("access_token")
                if not isinstance(token, str) or not re.fullmatch(
                    r"[A-Za-z0-9._~+/=-]{16,4096}", token
                ):
                    raise CloudAPIError("protocol")
                if result.get("token_type", "bearer") != "bearer":
                    raise CloudAPIError("protocol")
                user = self._user_view(result.get("user"))
                if user["role_code"] != "operator" or user["account_status"] != "active":
                    raise CloudAPIError("permission")
                self._token, self._user = token, user
                recovery_token = result.get("recovery_token")
                self._save_recovery_token(
                    recovery_token
                    if isinstance(recovery_token, str)
                    and 32 <= len(recovery_token) <= 2048
                    and re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", recovery_token)
                    else None
                )
                return dict(user)
    def me(self) -> dict:
        snapshot = self._request_snapshot(True)
        user = self._user_view(self.request("GET", "/v1/auth/me"))
        with self._lock:
            self._request_current(snapshot)
            if self._user and (
                self._user["id"] != user["id"] or self._user["role_code"] != user["role_code"]
                or user["account_status"] != "active"
            ):
                self.revoke_offline_access()
                self.clear_session()
                raise CloudAPIError("permission")
            self._user = user
            return dict(user)
    def revoke_offline_access(self):
        self._offline_lease = None
        self.offline_opt_in = False
        if self.offline_store is not None and self._user is not None:
            self.offline_store.revoke(self.base_url, self._user["username"])

    def probe_online(self) -> bool:
        """Connectivity only, never authorization. Re-login remains mandatory."""
        self.request("GET", "/health/live", authenticated=False)
        return True

    def change_password(self, current_password: str, new_password: str):
        snapshot = self._request_snapshot(True)
        result = self.request(
            "POST",
            "/v1/auth/change-password",
            json={"current_password": current_password, "new_password": new_password},
        )
        with self._lock:
            self._request_current(snapshot, outcome_uncertain=True)
            self.revoke_offline_access()
        return result

    def reset_password(self, username: str, new_password: str):
        """Use the account-bound recovery capability without a bearer session."""
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation

            return run_cloud_operation(self.reset_password, username, new_password)
        return self.request(
            "POST",
            "/v1/auth/reset-password",
            authenticated=False,
            json={
                "username": username,
                "new_password": new_password,
                "recovery_token": self._recovery_token or "",
            },
        )

    def logout(self) -> None:
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation
            return run_cloud_operation(self.logout)
        with self._auth_lock:
            with self._lock:
                token = self._token
                # Local authority ends before any remote wait. Only this
                # captured old token may be used for the bounded revocation.
                self.clear_session(clear_recovery=False)
            if token is None:
                return
            try:
                with self._stream_response("POST", self.base_url + "/v1/auth/logout",
                    headers={"Authorization": "Bearer " + token},
                    timeout=httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=3.0)) as response:
                    status = response.status_code
                    if not (200 <= status < 300 or status == 401):
                        raise CloudAPIError("unavailable", status_code=status, outcome_uncertain=True)
            except CloudAPIError:
                raise
            except httpx.TimeoutException:
                raise CloudAPIError("timeout", outcome_uncertain=True) from None
            except httpx.HTTPError:
                raise CloudAPIError("network", outcome_uncertain=True) from None
            finally:
                token = None
    def rpc(
        self, service: str, method: str, args: list, kwargs: dict, *, request_id: str | None = None
    ):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", service) or not re.fullmatch(
            r"[a-z][a-z0-9_]*", method
        ):
            raise CloudAPIError("validation")
        try:
            operation_id = str(UUID(request_id)) if request_id is not None else str(uuid4())
        except (ValueError, TypeError, AttributeError):
            raise CloudAPIError("validation") from None
        result = self.request(
            "POST",
            f"/v1/rpc/{service}/{method}",
            json={"args": args, "kwargs": kwargs, "request_id": operation_id},
        )
        if not isinstance(result, dict) or set(result) != {"result"}:
            raise CloudAPIError("protocol", outcome_uncertain=True)
        return result["result"]

    def get_all_pages(
        self, path: str, *, params: dict | None = None, page_size: int = 100, max_pages: int = 100
    ) -> list:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise CloudAPIError("validation")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise CloudAPIError("validation")
        output = []
        for page in range(max_pages):
            batch = self.request(
                "GET",
                path,
                params={**(params or {}), "limit": page_size, "offset": page * page_size},
            )
            if not isinstance(batch, list) or len(batch) > page_size:
                raise CloudAPIError("protocol")
            output.extend(batch)
            if len(batch) < page_size:
                return output
        raise CloudAPIError("limit")

    def close(self) -> None:
        with self._lock:
            self.clear_session(clear_recovery=False)
            self._closed = True
        self._http.close()
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
