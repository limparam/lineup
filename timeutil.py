"""Работа со временем.

В базе все моменты времени хранятся в UTC в формате ``YYYY-MM-DDTHH:MM:SSZ``.
Администратор вводит локальное время мероприятия, оно переводится в UTC
через часовой пояс мероприятия (IANA, например ``Europe/Moscow``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_DURATION = timedelta(days=14)

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$"
)


class TimeError(ValueError):
    pass


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(UTC_FORMAT)


def parse_iso(value: str) -> datetime:
    """Парсит строку из БД."""
    return datetime.strptime(value, UTC_FORMAT).replace(tzinfo=UTC)


def iso_now() -> str:
    return to_iso(now_utc())


def validate_timezone(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise TimeError("Укажите часовой пояс мероприятия.")
    if len(name) > 64:
        raise TimeError("Некорректный часовой пояс.")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise TimeError(
            "Неизвестный часовой пояс. Используйте IANA-имя, например Europe/Moscow."
        ) from None
    return name


def parse_local(value: str, tz_name: str) -> datetime:
    """«2027-11-04T20:00» в поясе tz_name -> aware datetime в UTC."""
    if not isinstance(value, str):
        raise TimeError("Укажите дату и время.")
    match = _ISO_RE.match(value.strip())
    if not match:
        raise TimeError("Дата и время должны быть в формате ГГГГ-ММ-ДДTЧЧ:ММ.")
    year, month, day, hour, minute, second = match.groups()
    try:
        naive = datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second or 0),
        )
        local = naive.replace(tzinfo=ZoneInfo(tz_name))
        return local.astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError):
        raise TimeError("Некорректная дата или время.") from None


def to_local_input(dt: datetime, tz_name: str) -> str:
    """UTC -> строка для <input type="datetime-local">."""
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%dT%H:%M")


def human_range(starts_at: str, ends_at: str, tz_name: str) -> str:
    """Текстовая подпись диапазона (используется в письмах/README, не в UI)."""
    tz = ZoneInfo(tz_name)
    a = parse_iso(starts_at).astimezone(tz)
    b = parse_iso(ends_at).astimezone(tz)
    if a.date() == b.date():
        return f"{a:%d.%m.%Y %H:%M} — {b:%H:%M}"
    return f"{a:%d.%m.%Y %H:%M} — {b:%d.%m.%Y %H:%M}"


def overlap_warning(event_start: datetime, event_end: datetime,
                    perf_start: datetime, perf_end: datetime) -> str | None:
    """Проверяет, что сет лежит внутри мероприятия."""
    if perf_end <= perf_start:
        return "Конец выступления должен быть позже начала."
    if perf_start < event_start or perf_end > event_end:
        return "Выступление должно быть внутри времени мероприятия."
    return None


def add_minutes_local(value: str, minutes: int) -> str:
    """Сдвиг локальной строки без учёта пояса (для префилла формы)."""
    match = _ISO_RE.match(value or "")
    if not match:
        return value
    year, month, day, hour, minute, second = match.groups()
    dt = datetime(int(year), int(month), int(day), int(hour), int(minute),
                  int(second or 0)) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M")
