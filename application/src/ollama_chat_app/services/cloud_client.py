"""Small synchronous cloud API client; desktop callers must use a UI worker."""

from __future__ import annotations

import ipaddress
import json as json_module
import re
import sys
from threading import RLock
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
    "not_authenticated": "请先登录云端账户。",
    "authentication": "用户名或密码错误，或登录已失效，请重新登录。",
    "permission": "当前账户没有执行此操作的权限。",
    "not_found": "请求的数据不存在，或当前账户无权访问。",
    "conflict": "数据发生冲突，请刷新后确认；不要重复提交新的操作。",
    "validation": "提交内容不符合要求，请检查输入及文件大小。",
    "limited": "请求过于频繁，请稍后再试。",
    "unavailable": "云端服务暂不可用，免费服务可能正在启动，请稍后再试。",
    "timeout": "等待云端响应超时，免费服务可能正在启动；请先刷新确认结果。",
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
        self, code: str, *, status_code: int | None = None, outcome_uncertain: bool = False
    ) -> None:
        self.code = code if code in _MESSAGES else "protocol"
        self.status_code = status_code
        self.outcome_uncertain = outcome_uncertain
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

    Auth endpoints are /v1/auth/register, login, me, logout, change-password.
    register()/login()/me() return the server's safe User dictionary; login
    keeps the bearer token ONLY in this object and never returns it. Password
    changes use current_password/new_password and preserve the current token.
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
    ) -> None:
        self.base_url = validate_base_url(base_url, allow_loopback_http=allow_loopback_http)
        self._lock = RLock()
        self._token: str | None = None
        self._user: dict | None = None
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
                trust_env=True,
                timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                transport=transport,
            )
        except Exception:
            raise CloudAPIError("configuration") from None

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

    def clear_session(self) -> None:
        with self._lock:
            self._token = None
            self._user = None
            self._offline_lease = None
            self._offline_session = False
            self.offline_opt_in = False
            self._http.cookies.clear()

    def request(
        self, method: str, path: str, *, json=None, params=None, authenticated: bool = True
    ):
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation

            return run_cloud_operation(
                self.request, method, path, json=json, params=params, authenticated=authenticated
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
        with self._lock:
            if self._closed:
                raise CloudAPIError("closed")
            if authenticated and self._token is None:
                raise CloudAPIError("offline" if self._offline_session else "not_authenticated")
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if authenticated:
                headers["Authorization"] = "Bearer " + self._token
            self._http.cookies.clear()
            try:
                with self._http.stream(
                    method, self.base_url + path, content=body, params=params, headers=headers
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise CloudAPIError("redirect", status_code=status)
                    if not 200 <= status < 300:
                        if status in {401, 403} and authenticated:
                            self.revoke_offline_access()
                        if status == 401 and authenticated:
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
                        raise CloudAPIError(
                            code, status_code=status, outcome_uncertain=write and status >= 500
                        )
                    if status == 204:
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
                        return json_module.loads(chunks)
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
            finally:
                self._http.cookies.clear()

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
        with self._lock:
            self.clear_session()
            try:
                result = self.request(
                    "POST", "/v1/auth/login",
                    json={"username": username, "password": password}, authenticated=False,
                )
            except CloudAPIError as error:
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
                    self._user = self._user_view(user)
                    self._offline_lease, self._offline_session = lease, True
                    self.offline_opt_in = True
                    return dict(self._user)
                raise
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
            self.sync_protocol = 2 if result.get("sync_protocol") == 2 else 1
            return dict(user)

    def me(self) -> dict:
        user = self._user_view(self.request("GET", "/v1/auth/me"))
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
        result = self.request(
            "POST",
            "/v1/auth/change-password",
            json={"current_password": current_password, "new_password": new_password},
        )
        self.revoke_offline_access()
        return result

    def logout(self) -> None:
        if self._on_gui_thread():
            from ..workers.cloud_bridge import run_cloud_operation

            return run_cloud_operation(self.logout)
        with self._lock:
            try:
                if self._token is not None:
                    self.request("POST", "/v1/auth/logout")
            finally:
                self.clear_session()

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
            self.clear_session()
            self._closed = True
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
