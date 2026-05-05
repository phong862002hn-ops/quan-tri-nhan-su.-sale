from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def compute_response_times(conversation: Any) -> dict[str, Any]:
    """
    Tính thời gian phản hồi của nhân viên từ timestamps trong hội thoại.

    Trả về:
      avg_response_seconds: trung bình thời gian từ khi khách nhắn đến khi nhân viên reply
      max_response_seconds: thời gian phản hồi dài nhất
      unanswered_customer_messages: số tin nhắn của khách chưa được nhân viên reply
      response_count: số lần nhân viên đã reply sau khách
    """
    messages = conversation.messages if hasattr(conversation, "messages") else []
    if not messages:
        return _empty_rt()

    gaps: list[float] = []
    unanswered = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.sender_type != "customer":
            i += 1
            continue
        customer_ts = _parse_ts(msg.sent_at)
        # Tìm tin nhắn nhân viên tiếp theo
        found = False
        for j in range(i + 1, len(messages)):
            if messages[j].sender_type == "employee":
                emp_ts = _parse_ts(messages[j].sent_at)
                if customer_ts and emp_ts and emp_ts >= customer_ts:
                    gaps.append((emp_ts - customer_ts).total_seconds())
                found = True
                i = j
                break
        if not found:
            unanswered += 1
            i += 1

    if not gaps:
        return {
            "avg_response_seconds": None,
            "max_response_seconds": None,
            "unanswered_customer_messages": unanswered,
            "response_count": 0,
        }

    return {
        "avg_response_seconds": round(sum(gaps) / len(gaps), 1),
        "max_response_seconds": round(max(gaps), 1),
        "unanswered_customer_messages": unanswered,
        "response_count": len(gaps),
    }


def _empty_rt() -> dict[str, Any]:
    return {
        "avg_response_seconds": None,
        "max_response_seconds": None,
        "unanswered_customer_messages": 0,
        "response_count": 0,
    }


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"
