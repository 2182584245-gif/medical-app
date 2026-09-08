"""Daily values preserve missing numeric observations instead of inventing zeros."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from ..time_utils import as_beijing, display_timezone

# key, display name, unit, aggregation method
PARAMETERS = {
    "diet": [
        ("count", "记录次数", "次", "sum"),
        ("calories_kcal", "热量", "千卡", "sum"),
        ("amount_g", "食物重量", "克", "sum"),
    ],
    "water": [("amount_ml", "饮水量", "毫升", "sum"), ("count", "记录次数", "次", "sum")],
    "activity": [
        ("duration_minutes", "运动时长", "分钟", "sum"),
        ("energy_kcal", "运动耗能", "千卡", "sum"),
        ("steps", "步数", "步", "sum"),
        ("count", "记录次数", "次", "sum"),
    ],
    "sleep": [
        ("duration_hours", "睡眠时长", "小时", "mean"),
        ("quality", "主观质量", "分", "mean"),
        ("count", "记录次数", "次", "sum"),
    ],
    "environment": [
        ("temperature_c", "室内温度", "℃", "mean"),
        ("humidity_percent", "室内湿度", "%", "mean"),
        ("ventilation_minutes", "通风时长", "分钟", "sum"),
        ("count", "记录次数", "次", "sum"),
    ],
}


def daily_series(records, category, parameter, start_date, days):
    definition = next((entry for entry in PARAMETERS[category] if entry[0] == parameter), None)
    if definition is None:
        raise ValueError("不支持的统计参数")
    samples = defaultdict(list)
    for record in records:
        if record.get("category") != category:
            continue
        day = as_beijing(record["occurred_at"]).astimezone(display_timezone()).date()
        if parameter == "count":
            samples[day].append(1)
        else:
            value = (record.get("details") or {}).get(parameter)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                samples[day].append(float(value))
    result = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        values = samples[day]
        value = (
            (sum(values) / len(values) if definition[3] == "mean" else sum(values))
            if values
            else None
        )
        if parameter == "count" and value is None:
            value = 0
        result.append({"date": day.isoformat(), "value": value, "samples": len(values)})
    return result
