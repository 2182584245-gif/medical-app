from __future__ import annotations

type UserId = int | str


class SecretStore:
    """Keep user-owned cloud API keys only for the current app session.

    A new ``SecretStore`` is created every time the application starts. Keys are
    deliberately never written to SQLite, logs, files, or the Windows credential
    manager. This avoids carrying a key across imported databases and keeps the
    frozen executable free of credential-enumeration backends.
    """

    def __init__(self) -> None:
        self._api_keys: dict[str, str] = {}

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

    @staticmethod
    def _account_name(user_id: UserId, provider: str) -> str:
        identifier = str(user_id).strip()
        if not identifier:
            raise ValueError("用户 ID 不能为空。")
        provider_name = provider.strip().lower()
        if provider_name not in {"ollama_cloud", "deepseek_cloud"}:
            raise ValueError("不支持的云端 AI 提供方。")
        return f"local-user:{identifier}:{provider_name}:api-key"
