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

from .config import DEFAULT_ALIYUN_ENDPOINT, DEFAULT_SUPABASE_ENDPOINT
from .paths import legacy_user_data_dir
from .services.cloud_client import CloudAPIError, validate_base_url

TARGET_LABELS = {"aliyun": "阿里云", "supabase": "Supabase"}
MAX_SETTINGS_BYTES = 16 * 1024


class EndpointSettingsError(ValueError):
    """Fixed user-facing text only; a malformed URL may contain pasted secrets."""


def validated_endpoint(value: str) -> str:
    try:
        return validate_base_url(value)
    except CloudAPIError:
        raise EndpointSettingsError(
            "服务地址无效：请填写有可信证书的 HTTPS 域名或公网 IP，"
            "不要包含用户名、密码、查询参数或片段。"
        ) from None


@dataclass(frozen=True)
class EndpointSettings:
    mode: str = "cloud"
    selected_target: str = "aliyun"
    aliyun_url: str = DEFAULT_ALIYUN_ENDPOINT
    supabase_url: str = DEFAULT_SUPABASE_ENDPOINT

    @property
    def base_url(self) -> str:
        return self.supabase_url if self.selected_target == "supabase" else self.aliyun_url

    @property
    def endpoints(self) -> dict[str, str]:
        return {"aliyun": self.aliyun_url, "supabase": self.supabase_url}

    def validated(self) -> EndpointSettings:
        if self.mode not in {"local", "cloud"} or self.selected_target not in TARGET_LABELS:
            raise EndpointSettingsError("连接配置的模式或路线无效；原设置已保留。")
        return replace(self, aliyun_url=validated_endpoint(self.aliyun_url),
                       supabase_url=validated_endpoint(self.supabase_url))

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
        return {"version": 1, "mode": checked.mode,
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
            } or type(value["version"]) is not int or value["version"] != 1):
                raise ValueError("invalid settings")
            urls = value["endpoints"]
            if type(urls) is not dict or set(urls) != set(TARGET_LABELS):
                raise ValueError("invalid routes")
            return EndpointSettings(value["mode"], value["selected_target"],
                                    urls["aliyun"], urls["supabase"]).validated()
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
