"""Public connection preferences only; no accounts, tokens or business database.

Loading never creates or rewrites a file. A bad existing configuration fails
closed and is not silently replaced with the new release's defaults.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DEFAULT_ALIYUN_ENDPOINT
from .paths import legacy_user_data_dir
from .services.cloud_client import CloudAPIError, validate_base_url

TARGET_LABELS = {"aliyun": "阿里云"}
MAX_SETTINGS_BYTES = 16 * 1024


class EndpointSettingsError(ValueError):
    """Fixed user-facing text only; a malformed URL may contain pasted secrets."""


def validated_endpoint(value: str) -> str:
    try:
        url = validate_base_url(value)
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (parsed.path.rstrip("/").endswith("/supabase")
                or (parsed.hostname or "").endswith((".supabase.co", ".supabase.com"))):
            raise CloudAPIError("configuration")
        return url
    except CloudAPIError:
        raise EndpointSettingsError(
            "请填写可信的阿里云 HTTPS 服务地址，不要包含密码或查询参数。"
            "本版本已停用 Supabase 路线。"
        ) from None


@dataclass(frozen=True)
class EndpointSettings:
    mode: str = "cloud"
    selected_target: str = "aliyun"
    aliyun_url: str = DEFAULT_ALIYUN_ENDPOINT

    @property
    def base_url(self) -> str:
        return self.aliyun_url

    @property
    def endpoints(self) -> dict[str, str]:
        return {"aliyun": self.aliyun_url}

    def validated(self) -> EndpointSettings:
        if self.mode not in {"local", "cloud"} or self.selected_target not in TARGET_LABELS:
            raise EndpointSettingsError("连接配置的模式或路线无效；原设置已保留。")
        return replace(self, aliyun_url=validated_endpoint(self.aliyun_url))

    def with_connection(self, mode: str, base_url: str = "") -> EndpointSettings:
        if mode not in {"local", "cloud"}:
            raise EndpointSettingsError("连接模式无效。")
        result = replace(self, mode=mode)
        if not base_url:
            return result.validated()
        url = validated_endpoint(base_url)
        # Exact saved addresses take precedence over a route-like URL suffix.
        target = next((key for key, value in self.endpoints.items() if value == url), None)
        if target is None:
            target = next((key for key in TARGET_LABELS if url.endswith("/" + key)),
                          self.selected_target)
        return replace(result, selected_target=target, **{target + "_url": url}).validated()

    def as_dict(self) -> dict:
        checked = self.validated()
        return {"version": 2, "mode": checked.mode,
                "selected_target": checked.selected_target, "endpoints": checked.endpoints}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class EndpointSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            Path(path) if path is not None else legacy_user_data_dir() / "connection-settings.json"
        )

    def _safe_path(self) -> None:
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or path.is_junction():
                raise EndpointSettingsError("连接设置路径不安全，原文件未修改。")
        if self.path.exists() and not self.path.is_file():
            raise EndpointSettingsError("连接设置不是普通文件，原内容未修改。")

    def load(self) -> EndpointSettings:
        try:
            self._safe_path()
            if not self.path.exists():
                return EndpointSettings()
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(raw) > MAX_SETTINGS_BYTES:
                raise ValueError("too large")
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if (type(value) is not dict or set(value) != {
                "version", "mode", "selected_target", "endpoints"
            } or type(value["version"]) is not int or value["version"] not in {1, 2}):
                raise ValueError("invalid settings")
            urls = value["endpoints"]
            expected = {"aliyun", "supabase"} if value["version"] == 1 else {"aliyun"}
            if type(urls) is not dict or set(urls) != expected:
                raise ValueError("invalid routes")
            if value["version"] == 1:
                # User-requested retirement: read the legacy public settings but
                # never reconnect to that route, rewrite a file, or merge caches.
                if value["selected_target"] not in {"aliyun", "supabase"}:
                    raise ValueError("invalid legacy target")
                validate_base_url(urls["supabase"])
                target = "aliyun"
            else:
                target = value["selected_target"]
            return EndpointSettings(value["mode"], target, urls["aliyun"]).validated()
        except Exception:
            raise EndpointSettingsError(
                "无法安全读取已保存的连接设置；原文件已保留，请检查设置文件后重试。"
            ) from None

    def save(self, settings: EndpointSettings, *, expected: EndpointSettings | None = None) -> None:
        temporary = None
        try:
            payload = json.dumps(settings.as_dict(), ensure_ascii=False, indent=2).encode("utf-8")
            self._safe_path()
            # Do not overwrite externally changed or malformed existing settings.
            previous = self.load()
            if expected is not None and previous != expected:
                raise EndpointSettingsError("连接设置已在别处更新，请重新打开弹窗后再保存。")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._safe_path()
            descriptor, name = tempfile.mkstemp(
                prefix=".connection-settings-", dir=self.path.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_path()
            os.replace(temporary, self.path)
        except EndpointSettingsError:
            raise
        except Exception:
            raise EndpointSettingsError("连接设置未保存，请检查本地目录权限后重试。") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def save_connection(self, mode: str, base_url: str = "") -> EndpointSettings:
        """Call only after an explicit, successful mode change, never at startup."""
        previous = self.load()
        settings = previous.with_connection(mode, base_url if mode == "cloud" else "")
        self.save(settings, expected=previous)
        return settings
