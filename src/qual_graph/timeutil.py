"""时间工具：统一使用 Asia/Shanghai 的日历日。"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

SHANGHAI_TZ = timezone(timedelta(hours=8))


def today() -> date:
    return datetime.now(SHANGHAI_TZ).date()


def to_date(value: str | date | datetime) -> date:
    """把 ISO-8601 字符串或日期对象归一为 date。

    带时区的时间戳按 Asia/Shanghai 折算到日历日，保证“今天是否有效”
    的判断与场馆运营口径一致。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        return value.astimezone(SHANGHAI_TZ).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("日期不能为空")
    if len(text) > 10:
        try:
            return to_date(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            pass
    return date.fromisoformat(text[:10])


def iso(value: date) -> str:
    return value.isoformat()
