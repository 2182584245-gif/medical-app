from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from .windows_dpapi import SecretProtectionError, protect_secret

type UserId = int | str


class SecretStore:
    """Session keys plus explicitly requested DPAPI persistence outside app data.

    Existing memory methods retain their session-only semantics. Persistent
    identities include the database/cloud namespace and stable account identity.
    A Windows-user default is deliberately shared as a fallback only; account
    replacements never change that default or another account's key.
    """

    def __init__(self, *, namespace: str = "local", storage_dir: Path | None = None) -> None:
        self._api_keys: dict[str, str] = {}
        if not namespace.strip():
            raise ValueError("密钥命名空间不能为空。")
        self.namespace = namespace.strip()
        self.storage_dir = storage_dir or (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / "HealthLife"
            / "private-credentials"
        )

    def set_api_key(
        self,
        user_id: UserId,
        api_key: str,
        provider: str = "ollama_cloud",
    ) -> None:
        account = self._account_name(user_id, provider)
        clean_key = api_key.strip()
        if not clean_key:
            raise ValueError("API Key 不能为空。")
        self._api_keys[account] = clean_key

    def get_api_key(
        self,
        user_id: UserId,
        provider: str = "ollama_cloud",
    ) -> str | None:
        return self._api_keys.get(self._account_name(user_id, provider))

    def delete_api_key(
        self,
        user_id: UserId,
        provider: str = "ollama_cloud",
    ) -> None:
        self._api_keys.pop(self._account_name(user_id, provider), None)

    def clear_api_keys(self) -> None:
        """Forget every API key held by this running process."""

        self._api_keys.clear()

    def save_api_key(
        self,
        user_id: UserId,
        api_key: str,
        provider: str = "ollama_cloud",
    ) -> None:
        self.set_api_key(user_id, api_key, provider)

    def save_persistent_api_key(
        self, user_id: UserId, api_key: str, provider: str = "deepseek_cloud"
    ) -> None:
        self._write_secret(self._persistent_account(user_id, provider), api_key)

    def get_persistent_api_key(
        self, user_id: UserId, provider: str = "deepseek_cloud"
    ) -> str | None:
        return self._read_secret(self._persistent_account(user_id, provider))

    def delete_persistent_api_key(self, user_id: UserId, provider: str = "deepseek_cloud") -> None:
        path = self._secret_path(self._persistent_account(user_id, provider))
        self._check_safe_path(path)
        path.unlink(missing_ok=True)

    def get_effective_api_key(
        self, user_id: UserId, provider: str = "deepseek_cloud"
    ) -> str | None:
        return (
            self.get_api_key(user_id, provider)
            or self.get_persistent_api_key(user_id, provider)
            or self.get_default_api_key(provider)
        )

    def set_default_api_key(self, api_key: str, provider: str = "deepseek_cloud") -> None:
        self._write_secret(self._default_account(provider), api_key)

    def get_default_api_key(self, provider: str = "deepseek_cloud") -> str | None:
        return self._read_secret(self._default_account(provider))

    def delete_default_api_key(self, provider: str = "deepseek_cloud") -> None:
        path = self._secret_path(self._default_account(provider))
        self._check_safe_path(path)
        path.unlink(missing_ok=True)

    def _persistent_account(self, user_id: UserId, provider: str) -> str:
        return json.dumps([self.namespace, self._account_name(user_id, provider)])

    def _default_account(self, provider: str) -> str:
        return json.dumps(["windows-user-default", self._account_name("default", provider)])

    def _secret_path(self, account: str) -> Path:
        digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
        return self.storage_dir / f"{digest}.dpapi"

    @staticmethod
    def _check_safe_path(path: Path) -> None:
        for entry in (path, *path.parents):
            if entry.exists() or entry.is_symlink():
                info = entry.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise SecretProtectionError("密钥目录不能使用链接或重解析点。")
        if path.exists() and not path.is_file():
            raise SecretProtectionError("密钥存储目标不是普通文件。")

    def _write_secret(self, account: str, key: str) -> None:
        clean_key = key.strip()
        if not clean_key or len(clean_key.encode("utf-8")) > 8_192:
            raise ValueError("API Key 不能为空，且不能超过 8192 字节。")
        raw = json.dumps({"account": account, "key": clean_key}, ensure_ascii=False).encode()
        encrypted = protect_secret(raw)
        destination = self._secret_path(account)
        temporary: str | None = None
        try:
            self._check_safe_path(destination)
            self.storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(
                prefix="key-", suffix=".tmp", dir=self.storage_dir
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_safe_path(destination)
            os.replace(temporary, destination)
            temporary = None
        except (OSError, ValueError):
            raise SecretProtectionError("加密密钥保存失败，未保存明文。") from None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    Path(temporary).unlink()

    def _read_secret(self, account: str) -> str | None:
        path = self._secret_path(account)
        self._check_safe_path(path)
        if not path.exists():
            return None
        try:
            if not 0 < path.stat().st_size <= 32_768:
                raise ValueError
            data = json.loads(protect_secret(path.read_bytes(), decrypt=True))
            if data.get("account") != account or not isinstance(data.get("key"), str):
                raise ValueError
            return data["key"] or None
        except (OSError, ValueError, TypeError, AttributeError):
            raise SecretProtectionError("加密密钥无法读取，请重新设置。") from None

    @staticmethod
    def _account_name(user_id: UserId, provider: str) -> str:
        identifier = str(user_id).strip()
        if not identifier:
            raise ValueError("用户 ID 不能为空。")
        provider_name = provider.strip().lower()
        if provider_name not in {"ollama_cloud", "deepseek_cloud"}:
            raise ValueError("不支持的云端 AI 提供方。")
        return f"local-user:{identifier}:{provider_name}:api-key"
