from __future__ import annotations

import calendar
from datetime import datetime


def add_months(stamp: datetime, months: int) -> datetime:
    month_index = stamp.month - 1 + months
    year = stamp.year + month_index // 12
    month = month_index % 12 + 1
    day = min(stamp.day, calendar.monthrange(year, month)[1])
    return stamp.replace(year=year, month=month, day=day)


def walk_forward_windows(
    start: datetime,
    end: datetime,
    months: int = 6,
) -> tuple[tuple[datetime, datetime], ...]:
    if months < 1:
        raise ValueError("window months must be positive")
    if end < start:
        raise ValueError("walk-forward end is before start")
    if end == start:
        return ()
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = add_months(cursor, months)
        if nxt > end:
            nxt = end
        windows.append((cursor, nxt))
        cursor = nxt
    return tuple(windows)


def in_half_open(stamp: datetime, start: datetime, end: datetime) -> bool:
    return start <= stamp < end


def month_span(start: datetime, end: datetime) -> int | None:
    if end < start:
        return None
    for months in range(0, 241):
        if add_months(start, months) == end:
            return months
    return None
