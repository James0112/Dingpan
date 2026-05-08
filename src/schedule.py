from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedTime:
    hour: int
    minute: int
    second: int


def parse_clock_time(value: str) -> ParsedTime:
    parts = value.strip().split(":")
    if len(parts) not in {2, 3}:
        raise ScheduleError("Clock time must be HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ScheduleError("Clock time contains non-numeric parts") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ScheduleError("Clock time is out of range")
    return ParsedTime(hour=hour, minute=minute, second=second)


def normalize_clock_time(value: str) -> str:
    parsed = parse_clock_time(value)
    return f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}"


def has_reached_clock_time(*, now_utc: datetime, timezone_name: str, clock_time: str) -> bool:
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    target = parse_clock_time(clock_time)
    current_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    target_seconds = target.hour * 3600 + target.minute * 60 + target.second
    return current_seconds >= target_seconds
