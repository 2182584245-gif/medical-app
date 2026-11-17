"""Application civil time is Beijing time; persisted instants remain UTC."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BEIJING_TIMEZONE = timezone(timedelta(hours=8), "北京时间")
_display_timezone = "Asia/Shanghai"


def set_display_timezone(name: str) -> None:
    """Desktop-only presentation preference; never changes the OS timezone."""
    ZoneInfo(name)
    global _display_timezone
    _display_timezone = name


def display_timezone_name() -> str:
    return _display_timezone


def display_timezone_label() -> str:
    return {
        "Asia/Shanghai": "北京时间",
        "Asia/Hong_Kong": "香港时间",
        "Asia/Tokyo": "东京时间",
        "Asia/Singapore": "新加坡时间",
        "Europe/London": "伦敦时间",
        "America/New_York": "纽约时间",
        "America/Los_Angeles": "洛杉矶时间",
        "Australia/Sydney": "悉尼时间",
        "UTC": "世界标准时间",
    }.get(_display_timezone, _display_timezone)


def display_timezone():
    # Beijing always works on minimal Linux backends without the optional tzdata wheel.
    return BEIJING_TIMEZONE if _display_timezone == "Asia/Shanghai" else ZoneInfo(_display_timezone)


def beijing_now() -> datetime:
    return datetime.now(display_timezone())


def beijing_today() -> date:
    return beijing_now().date()


def as_beijing(value: datetime | str) -> datetime:
    """Interpret civil input as Beijing time, preserving explicitly zoned instants."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("时间必须是日期时间或日期时间文字")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=BEIJING_TIMEZONE)
    return value.astimezone(BEIJING_TIMEZONE)


def format_beijing(value: object, empty: str = "") -> str:
    """Format stored UTC values and aware datetimes consistently in the UI."""
    if value is None or value == "":
        return empty
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    try:
        return as_beijing(value).astimezone(display_timezone()).strftime("%Y-%m-%d %H:%M")  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return str(value)
