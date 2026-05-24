"""SLA violation detection.

Scans a single conversation's messages and emits violations when a customer
message chain has been unanswered for longer than the threshold (default 60
business minutes, configurable via `QA_SLA_THRESHOLD_MINUTES`).

A "customer chain" is a run of consecutive customer messages with no employee
message in between. The chain starts at the first customer message and closes
when an employee message arrives (or stays open at end-of-list).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app import cache_db
from app.business_hours import business_minutes_between


def get_sla_threshold_minutes() -> int:
    raw = os.environ.get("QA_SLA_THRESHOLD_MINUTES", "60")
    try:
        value = int(raw)
    except ValueError:
        value = 60
    return max(1, value)


def detect_violations_for_conversation(
    db_path: Path,
    conversation_id: str,
    messages: list[dict[str, Any]],
    now: int | None = None,
) -> None:
    """Inspect messages and either record or resolve violations in the DB.

    `messages` is a list of dicts with keys `created_at` (unix seconds) and
    `sender_type` ('employee'|'customer'). Order does not matter — we sort
    internally.
    """
    if not messages:
        return
    now = now or int(time.time())
    threshold = get_sla_threshold_minutes()
    ordered = sorted(messages, key=lambda m: int(m.get("created_at") or 0))

    chain_start: int | None = None
    for msg in ordered:
        sender = str(msg.get("sender_type") or "")
        ts = int(msg.get("created_at") or 0)
        if not ts:
            continue
        if sender == "customer":
            if chain_start is None:
                chain_start = ts
        elif sender == "employee":
            if chain_start is not None:
                cache_db.resolve_sla_violation(
                    db_path, conversation_id, chain_start, resolved_at=ts,
                )
                chain_start = None

    if chain_start is not None:
        minutes = business_minutes_between(chain_start, now)
        if minutes > threshold:
            cache_db.record_sla_violation(
                db_path,
                conversation_id=conversation_id,
                customer_message_at=chain_start,
                detected_at=now,
                business_minutes_at_detection=minutes,
            )
