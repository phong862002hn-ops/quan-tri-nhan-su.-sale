"""Business-hours calculator for SLA monitoring.

Compute the number of *business minutes* between two unix timestamps, where
business hours default to 08:00–23:00 in VN timezone (UTC+7). Configurable via
env vars `QA_BUSINESS_HOUR_START` and `QA_BUSINESS_HOUR_END`.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


VN_TZ = timezone(timedelta(hours=7))


def get_business_hours() -> tuple[int, int]:
    start = int(os.environ.get("QA_BUSINESS_HOUR_START", "8"))
    end = int(os.environ.get("QA_BUSINESS_HOUR_END", "23"))
    if not (0 <= start < end <= 24):
        # Invalid env config — fall back to defaults to keep app usable.
        return 8, 23
    return start, end


def business_minutes_between(start_ts: int, end_ts: int) -> int:
    if start_ts >= end_ts:
        return 0
    start_hour, end_hour = get_business_hours()

    total_minutes = 0
    current = datetime.fromtimestamp(start_ts, VN_TZ)
    end = datetime.fromtimestamp(end_ts, VN_TZ)

    # Safety cap: iterate at most ~3 years of days to avoid runaway loops on
    # malformed input.
    for _ in range(1100):
        if current >= end:
            break
        day_open = current.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        day_close = current.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        window_start = max(current, day_open)
        window_end = min(end, day_close)
        if window_start < window_end:
            total_minutes += int((window_end - window_start).total_seconds() // 60)
        next_day_open = (current + timedelta(days=1)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0,
        )
        current = next_day_open
    return total_minutes


def is_in_business_hours(ts: int) -> bool:
    start_hour, end_hour = get_business_hours()
    dt = datetime.fromtimestamp(ts, VN_TZ)
    return start_hour <= dt.hour < end_hour
