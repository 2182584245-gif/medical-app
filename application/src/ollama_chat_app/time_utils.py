"""Application civil time is Beijing time; persisted instants remain UTC."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

BEIJING_TIMEZONE = timezone(timedelta(hours=8), "北京时间")


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


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
        return as_beijing(value).strftime("%Y-%m-%d %H:%M")  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return str(value)
