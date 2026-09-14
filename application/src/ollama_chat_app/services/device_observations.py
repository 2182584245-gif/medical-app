"""Versioned phone/wearable ingress contract, independent of any vendor SDK.

An authenticated caller supplies its own user scope to the existing draft
service. Payloads cannot choose another member. Screen inactivity is an
observation, never a sleep duration. No hardware is contacted by this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime


def observation_to_proposal(observation: Mapping) -> dict:
    allowed = {"schema_version", "kind", "source", "external_id", "start_at", "end_at", "values"}
    if (
        set(observation) - allowed
        or type(observation.get("schema_version")) is not int
        or observation.get("schema_version") != 1
    ):
        raise ValueError("设备记录协议或字段无效")
    source = observation.get("source")
    if source not in {"self_report", "wearable", "screen_time"}:
        raise ValueError("记录来源无效")
    kind = observation.get("kind")
    if kind not in {"activity", "sleep", "screen_inactivity"}:
        raise ValueError("不支持的设备记录类型")
    if (kind == "screen_inactivity") != (source == "screen_time"):
        raise ValueError("屏幕未使用时间只能保存为线索，不能转换为睡眠")
    start, end = (_aware(observation.get(field)) for field in ("start_at", "end_at"))
    minutes = (end - start).total_seconds() / 60
    if not 0 < minutes <= 24 * 60:
        raise ValueError("记录结束时间必须晚于开始时间，且跨度不超过 24 小时")
    identifier = observation.get("external_id", "")
    if not isinstance(identifier, str) or len(identifier) > 200:
        raise ValueError("外部记录编号无效")
    if kind == "screen_inactivity":
        return {
            "type": "insight",
            "content": (
                f"屏幕使用线索：{start.isoformat()} 至 {end.isoformat()} 未使用手机。"
                "这不代表已经睡着；请向用户核对入睡和起床时间。"
            ),
        }
    values = observation.get("values", {})
    if not isinstance(values, Mapping):
        raise ValueError("设备数据格式无效")
    details = {
        "input_source": source,
        "external_id": identifier,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
    }
    if kind == "sleep":
        if values:
            raise ValueError("本版睡眠接口只接收开始与结束时间，不推测睡眠分期")
        details["duration_hours"] = round(minutes / 60, 2)
        label = "用户自报作息" if source == "self_report" else "设备报告的睡眠区间（非医疗判断）"
    else:
        if set(values) - {"steps", "activity_type"}:
            raise ValueError("活动接口包含不支持的指标")
        steps = values.get("steps")
        if steps is not None:
            if type(steps) is not int or not 0 <= steps <= 200000:
                raise ValueError("步数无效")
            details["steps"] = steps
        activity_type = values.get("activity_type", "活动")
        if not isinstance(activity_type, str) or not 1 <= len(activity_type) <= 40:
            raise ValueError("活动类型无效")
        details["duration_minutes"] = round(minutes)
        label = activity_type
    return {
        "type": "life_record",
        "category": kind,
        "occurred_at": end.isoformat(),
        "content": f"{label}：{start.isoformat()} 至 {end.isoformat()}",
        "details": details,
    }


def _aware(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须是带时区的 ISO 8601 字符串")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return result
