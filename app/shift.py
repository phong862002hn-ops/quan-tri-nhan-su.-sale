"""Shift classification for lead review workflow.

Three fixed shifts in VN timezone:
  - morning   08:00 – 13:00
  - afternoon 13:00 – 18:00
  - evening   18:00 – 23:00

A conversation belongs to the shift containing its most recent message.
Messages outside 08:00–23:00 (overnight 23:00–08:00) return None — those
conversations don't get reviewed under any shift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


VN_TZ = timezone(timedelta(hours=7))


SHIFT_DEFINITIONS: list[dict[str, Any]] = [
    {"id": "morning",   "name": "Ca sáng",   "start_hour": 8,  "end_hour": 13},
    {"id": "afternoon", "name": "Ca chiều",  "start_hour": 13, "end_hour": 18},
    {"id": "evening",   "name": "Ca tối",    "start_hour": 18, "end_hour": 23},
]


def get_shift_for_timestamp(ts: int | None) -> dict | None:
    if not ts:
        return None
    dt = datetime.fromtimestamp(int(ts), VN_TZ)
    hour = dt.hour
    for shift in SHIFT_DEFINITIONS:
        if shift["start_hour"] <= hour < shift["end_hour"]:
            return shift
    return None


def get_shift_by_id(shift_id: str) -> dict | None:
    for shift in SHIFT_DEFINITIONS:
        if shift["id"] == shift_id:
            return shift
    return None


def _parse_sent_at_to_unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n // 1000 if n > 10_000_000_000 else n
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp())


def get_conversation_last_message_unix(conversation: Any) -> int | None:
    """Return the unix timestamp of the latest message in a Conversation.

    Accepts either an `app.schemas.Conversation` (with `messages` list of
    `Message` objects holding `sent_at: str|None`) or a plain dict with the
    same shape. Notification messages have already been filtered upstream by
    sync_worker before they reach evaluation/storage."""
    if conversation is None:
        return None
    messages = getattr(conversation, "messages", None)
    if messages is None and isinstance(conversation, dict):
        messages = conversation.get("messages")
    if not messages:
        return None
    candidates: list[int] = []
    for msg in messages:
        sent_at = getattr(msg, "sent_at", None) if not isinstance(msg, dict) else msg.get("sent_at")
        ts = _parse_sent_at_to_unix(sent_at)
        if ts is not None:
            candidates.append(ts)
    return max(candidates) if candidates else None


def get_conversation_shift(conversation: Any) -> dict | None:
    return get_shift_for_timestamp(get_conversation_last_message_unix(conversation))


def get_shift_date_range(date: str, shift_id: str) -> tuple[int, int]:
    """Inclusive start, exclusive end (unix seconds) for the given date+shift.
    `date` is `YYYY-MM-DD` in VN timezone."""
    shift = get_shift_by_id(shift_id)
    if shift is None:
        raise ValueError(f"Unknown shift_id: {shift_id}")
    year, month, day = (int(part) for part in date.split("-"))
    start = datetime(year, month, day, shift["start_hour"], 0, 0, tzinfo=VN_TZ)
    end = datetime(year, month, day, shift["end_hour"], 0, 0, tzinfo=VN_TZ)
    return int(start.timestamp()), int(end.timestamp())


def today_iso() -> str:
    return datetime.now(VN_TZ).strftime("%Y-%m-%d")
