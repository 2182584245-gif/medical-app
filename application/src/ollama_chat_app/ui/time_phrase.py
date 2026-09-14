"""Small local date/time speech parser; no AI, network or automatic writes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DIGITS = {
    char: str(value)
    for value, chars in enumerate(("零〇", "一", "二两", "三", "四", "五", "六", "七", "八", "九"))
    for char in chars
}


def _number(value):
    if "十" in value:
        left, right = value.split("十")
        return str(int(_DIGITS.get(left, left) or 1) * 10 + int(_DIGITS.get(right, right) or 0))
    return "".join(_DIGITS.get(char, char) for char in value)


def parse_time_phrase(text: str, base: datetime, *, date_only=False) -> datetime:
    text = re.sub(r"\s+", "", str(text)).replace("：", ":").replace("号", "日")
    if not text or len(text) > 160:
        raise ValueError("请简短说明日期和时间，例如明天下午三点半。")
    numeric = re.sub(
        r"[零〇一二两三四五六七八九十]+(?=[年月日点时分])",
        lambda match: _number(match.group()),
        text,
    )
    value = base.replace(second=0, microsecond=0)
    found = False
    date_match = re.search(r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})日?", numeric)
    if date_match:
        year, month, day = date_match.groups()
        value = value.replace(year=int(year or base.year), month=int(month), day=int(day))
        found = True
    else:
        relative = next(
            (
                (word, days)
                for word, days in (
                    ("大后天", 3),
                    ("后天", 2),
                    ("明天", 1),
                    ("昨天", -1),
                    ("今天", 0),
                )
                if word in text
            ),
            None,
        )
        week = re.search(r"(下周|本周|这周|周|星期)([一二三四五六日天])", text)
        if relative:
            value += timedelta(days=relative[1])
            found = True
        elif week:
            weekday = "一二三四五六日天".index(week[2])
            weekday = min(weekday, 6)
            offset = weekday - base.weekday()
            if week[1] == "下周" or week[1] in {"周", "星期"} and offset < 0:
                offset += 7
            value += timedelta(days=offset)
            found = True
    time_match = re.search(r"(\d{1,2})(?:点|时|:)(?:(\d{1,2})(?:分)?)?", numeric)
    if time_match:
        if date_only:
            raise ValueError("这里仅填写日期，请不要包含时分。")
        hour, minute = int(time_match[1]), int(time_match[2] or 0)
        if "半" in numeric:
            minute = 30
        if any(word in text for word in ("下午", "晚上", "今晚")):
            if not 1 <= hour <= 12:
                raise ValueError("下午或晚上的时间请使用一点到十二点。")
            hour = hour % 12 + 12
        elif any(word in text for word in ("上午", "早上", "凌晨")):
            if not 0 <= hour <= 12:
                raise ValueError("上午或凌晨的时间请使用零点到十二点。")
            hour = hour % 12
        elif "中午" in text and 1 <= hour < 11:
            hour += 12
        value = value.replace(hour=hour, minute=minute)
        found = True
    if not found:
        raise ValueError("未识别到明确日期或时间，请说年月日和时分，或直接使用下拉框。")
    weekday_text = re.search(r"(?:星期|周)([一二三四五六日天])", text)
    if date_match and weekday_text:
        expected = min("一二三四五六日天".index(weekday_text[1]), 6)
        if value.weekday() != expected:
            raise ValueError("所说的日期与星期不一致，请核对日期；星期由日期自动计算。")
    return value
