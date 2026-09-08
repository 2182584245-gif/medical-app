"""Validated personal display settings; never a store for keys or passwords."""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..data.database import Database, timestamp_to_db, utc_now

DEFAULT_PREFERENCES = {
    "nickname": "",
    "preferred_name": "",
    "avatar_path": "",
    "avatar_data": "",
    "font_size": 20,
    "brightness": 100,
    "theme_color": "sage",
    "timezone": "Asia/Shanghai",
    "city": "",
    "latitude": None,
    "longitude": None,
    "ai_tone": "warm",
    "ai_complexity": "simple",
    "ai_model": "deepseek-v4-flash",
    "ai_provider": "deepseek_cloud",
    "ai_context_consent": False,
    "weather_consent": False,
    "ai_context_consent_decided": False,
}


class PreferencesService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, user_id: int) -> dict:
        with self.database.connect() as connection:
            if (
                connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
                is None
            ):
                raise ValueError("账户不存在")
            row = connection.execute(
                "SELECT preferences_json FROM user_preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
        saved = json.loads(row["preferences_json"]) if row else {}
        return {
            **DEFAULT_PREFERENCES,
            **{k: v for k, v in saved.items() if k in DEFAULT_PREFERENCES},
        }

    def update(self, user_id: int, patch: Mapping) -> dict:
        clean = self.validate(patch)
        # Read and write in the same transaction, so concurrent partial updates
        # cannot erase each other's fields. No network calls inside this block.
        with self.database.transaction() as connection:
            if (
                connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
                is None
            ):
                raise ValueError("账户不存在")
            row = connection.execute(
                "SELECT preferences_json FROM user_preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
            saved = json.loads(row["preferences_json"]) if row else {}
            merged = {**DEFAULT_PREFERENCES, **saved, **clean}
            connection.execute(
                "INSERT INTO user_preferences(user_id, preferences_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET preferences_json=excluded.preferences_json, "
                "updated_at=excluded.updated_at",
                (user_id, json.dumps(merged, ensure_ascii=False), timestamp_to_db(utc_now())),
            )
        return merged

    @staticmethod
    def validate(patch: Mapping) -> dict:
        if not isinstance(patch, Mapping) or set(patch) - set(DEFAULT_PREFERENCES):
            raise ValueError("设置中包含不支持的项目")
        clean = dict(patch)
        limits = {
            "nickname": 40,
            "preferred_name": 40,
            "city": 80,
            "avatar_path": 500,
            "avatar_data": 350_000,
            "ai_model": 120,
            "timezone": 80,
        }
        for key, limit in limits.items():
            if key in clean:
                if not isinstance(clean[key], str) or len(clean[key]) > limit:
                    raise ValueError(f"{key} 设置格式或长度不正确")
                clean[key] = clean[key].strip()
        for key, bounds in {"font_size": (16, 30), "brightness": (70, 120)}.items():
            if key in clean and (
                type(clean[key]) is not int or not bounds[0] <= clean[key] <= bounds[1]
            ):
                raise ValueError("字号或界面亮度超出可选范围")
        enums = {
            "theme_color": {"sage", "blue", "rose"},
            "ai_tone": {"warm", "concise", "formal"},
            "ai_complexity": {"simple", "standard", "detailed"},
            "ai_provider": {"local", "local_ollama", "ollama_cloud", "deepseek_cloud"},
        }
        for key, choices in enums.items():
            if key in clean and clean[key] not in choices:
                raise ValueError("请选择受支持的设置")
        for key in ("ai_context_consent", "ai_context_consent_decided", "weather_consent"):
            if key in clean and type(clean[key]) is not bool:
                raise ValueError("授权选项必须是开或关")
        for key, limit in (("latitude", 90), ("longitude", 180)):
            if (
                key in clean
                and clean[key] is not None
                and (type(clean[key]) not in (float, int) or not -limit <= clean[key] <= limit)
            ):
                raise ValueError("地区坐标不正确")
        if "timezone" in clean:
            try:
                ZoneInfo(clean["timezone"])
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("请选择有效时区") from None
        if clean.get("avatar_data"):
            try:
                if not clean["avatar_data"].startswith("data:image/png;base64,"):
                    raise ValueError
                data = base64.b64decode(clean["avatar_data"].split(",", 1)[1], validate=True)
                if (
                    len(data) < 33
                    or len(data) > 256_000
                    or data[:8] != b"\x89PNG\r\n\x1a\n"
                    or data[12:16] != b"IHDR"
                ):
                    raise ValueError
                width, height = struct.unpack(">II", data[16:24])
                if not 1 <= width <= 256 or not 1 <= height <= 256:
                    raise ValueError
            except (ValueError, struct.error):
                raise ValueError("头像需要使用应用转换后的不超过 256 像素 PNG 图片") from None
        return clean

    # Explicit RPC-style names prevent read-only settings reads being logged as writes.
    get_preferences = get
    update_preferences = update
