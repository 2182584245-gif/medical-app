"""Versioned, bounded developer configuration; never contains API credentials."""

from __future__ import annotations

import copy
import json

FEATURE_LABELS = {
    "today": "今日记录",
    "ai": "AI 助手",
    "statistics": "档案与统计",
    "services": "会员服务",
    "commerce": "商品与购物车",
    "appointments": "上门预约",
}
DEFAULT_SETTINGS = {
    "ai": {
        "model": "deepseek-v4-flash",
        "complexity": "simple",
        "system_prompt": "",
        "tone": "warm",
        "enabled": True,
        "max_tokens": 2048,
        "skills": [],
        "knowledge": [],
        "daily_tips": [],
    },
    "features": dict.fromkeys(FEATURE_LABELS, True),
    "runtime": {
        "announcement": "",
        "support_contact": "",
        "maintenance_message": "",
        "maintenance_enabled": False,
        "client_timeout_seconds": 60,
    },
}


class DeveloperError(RuntimeError):
    """A fixed/safe message suitable for the developer interface."""


def default_snapshot() -> dict:
    return {"revision": 0, "settings": copy.deepcopy(DEFAULT_SETTINGS)}


def validate_settings(value: object) -> dict:
    """Allow configuration data only, never code, paths, DSNs or executable tools."""

    def text(item, limit):
        if not isinstance(item, str) or len(item) > limit or "\x00" in item:
            raise DeveloperError("文字内容超长或格式不正确。")

    if not isinstance(value, dict) or set(value) != {"ai", "features", "runtime"}:
        raise DeveloperError("设置必须包含 AI、开放功能和服务运行三个部分。")
    ai, features, runtime = (value[name] for name in ("ai", "features", "runtime"))
    if not all(isinstance(part, dict) for part in (ai, features, runtime)):
        raise DeveloperError("设置格式不正确。")
    if set(ai) != set(DEFAULT_SETTINGS["ai"]):
        raise DeveloperError("AI 设置包含不支持的字段。")
    if ai["model"] not in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        raise DeveloperError("请选择应用支持的 DeepSeek 模型。")
    if ai["complexity"] not in {"simple", "standard", "detailed"}:
        raise DeveloperError("回答复杂度不正确。")
    if ai["tone"] not in {"warm", "concise", "formal"} or type(ai["enabled"]) is not bool:
        raise DeveloperError("AI 语气或启用状态不正确。")
    if type(ai["max_tokens"]) is not int or not 256 <= ai["max_tokens"] <= 2048:
        raise DeveloperError("回答长度上限应为 256 至 2048 token。")
    text(ai["system_prompt"], 12000)
    for key, maximum, fields in (
        ("skills", 20, {"name": 100, "instructions": 6000}),
        ("knowledge", 50, {"title": 200, "content": 12000, "source": 1000}),
    ):
        rows = ai[key]
        if not isinstance(rows, list) or len(rows) > maximum:
            raise DeveloperError("技能或知识条目数量超过上限。")
        for row in rows:
            extras = {"triggers"} if key == "skills" else {"reviewed", "keywords"}
            if not isinstance(row, dict) or set(row) != {*fields, "enabled", *extras}:
                raise DeveloperError("技能或知识条目格式不正确。")
            if type(row["enabled"]) is not bool:
                raise DeveloperError("条目启用状态必须是是或否。")
            for field, limit in fields.items():
                text(row[field], limit)
            tags = row["triggers" if key == "skills" else "keywords"]
            if not isinstance(tags, list) or len(tags) > 20:
                raise DeveloperError("关键词最多 20 项。")
            for tag in tags:
                text(tag, 100)
            if key == "knowledge" and type(row["reviewed"]) is not bool:
                raise DeveloperError("专业知识需要明确审核状态。")
    if not isinstance(ai["daily_tips"], list) or len(ai["daily_tips"]) > 20:
        raise DeveloperError("每日提示最多 20 条。")
    for tip in ai["daily_tips"]:
        text(tip, 500)
    if set(features) != set(FEATURE_LABELS) or any(type(x) is not bool for x in features.values()):
        raise DeveloperError("开放功能必须使用已知板块及是或否的状态。")
    if set(runtime) != set(DEFAULT_SETTINGS["runtime"]):
        raise DeveloperError("运行设置包含不允许远程更改的项目。")
    for key, limit in (
        ("announcement", 2000),
        ("support_contact", 300),
        ("maintenance_message", 2000),
    ):
        text(runtime[key], limit)
    if type(runtime["maintenance_enabled"]) is not bool:
        raise DeveloperError("维护状态格式不正确。")
    timeout = runtime["client_timeout_seconds"]
    if type(timeout) is not int or not 30 <= timeout <= 120:
        raise DeveloperError("连接等待时长应为 30 至 120 秒。")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 256 * 1024:
        raise DeveloperError("设置总大小不能超过 256 KB。")
    return copy.deepcopy(value)


def safe_snapshot(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get("revision")) is not int:
        raise DeveloperError("配置版本格式不正确，未采用未知设置。")
    if value["revision"] < 0:
        raise DeveloperError("配置版本不正确。")
    return {"revision": value["revision"], "settings": validate_settings(value.get("settings"))}
