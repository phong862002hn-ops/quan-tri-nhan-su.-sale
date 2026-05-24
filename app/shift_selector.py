"""Select up to ~50 conversations per shift, tiered by score.

Plan reference: PLAN_SHIFT_REVIEW.md "Phương án D".

Tiers (defaults; override via env):
  - P1 (10): blacklist OR complaint rule triggered — must review
  - P2 (15): lowest scores (after excluding P1)
  - P3 (15): random sample from medium tier (>=50% and <75%)
  - P4 (10): random sample from high tier (>=85%)

Score percentage is computed against each conv's *effective* max_score (the
evaluator already accounts for skipped rules due to channel/conditional
filters in rules v1.3.0). Random selection is seeded by (date, shift_id) so
the page reloads stably for the same lead/day.
"""
from __future__ import annotations

import os
import random
from typing import Any

from app.shift import (
    get_conversation_shift,
    get_shift_by_id,
)


# Score tier thresholds, expressed as percentage of effective max_score.
LOW_THRESHOLD_PCT = 50.0
MEDIUM_THRESHOLD_PCT = 75.0
HIGH_THRESHOLD_PCT = 85.0


def get_quota_config() -> dict[str, int]:
    def _read(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except ValueError:
            return default
    return {
        "priority_1": _read("SHIFT_QUOTA_PRIORITY_1", 10),
        "priority_2": _read("SHIFT_QUOTA_PRIORITY_2", 15),
        "priority_3": _read("SHIFT_QUOTA_PRIORITY_3", 15),
        "priority_4": _read("SHIFT_QUOTA_PRIORITY_4", 10),
        "total":      _read("SHIFT_QUOTA_TOTAL", 50),
    }


def _score_percent(evaluation: dict[str, Any]) -> float:
    total = float(evaluation.get("total_score") or 0)
    mx = float(evaluation.get("max_score") or 0)
    if mx <= 0:
        return 0.0
    return (total / mx) * 100


def _finding_failed(evaluation: dict[str, Any], rule_id: str) -> bool:
    """Returns True if a rule with `rule_id` is present in `findings` and
    `passed=False` (or in `blacklist_findings`, which only stores failures)."""
    for finding in evaluation.get("findings") or []:
        if finding.get("rule_id") == rule_id and not finding.get("passed", True):
            return True
    for finding in evaluation.get("blacklist_findings") or []:
        if finding.get("rule_id") == rule_id:
            return True
    return False


def _is_blacklist(evaluation: dict[str, Any]) -> bool:
    return bool(evaluation.get("blacklist_triggered"))


def _is_complaint(evaluation: dict[str, Any]) -> bool:
    return (
        _finding_failed(evaluation, "complaint_flow")
        or _finding_failed(evaluation, "complaint_no_argument")
    )


def select_conversations_for_shift(
    conversations_with_eval: list[dict[str, Any]],
    shift_id: str,
    date: str,
    quota: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Tier the day's conversations into 4 priority buckets for one shift.

    `conversations_with_eval` is a list of dicts with shape:
        {"conversation": <Conversation>, "evaluation": <result dict>}

    The function does not deduplicate against prior reviews — that decision
    belongs in `lead_review.upsert_pending_review` (idempotent by conv_id).
    """
    quota = quota or get_quota_config()

    in_shift: list[dict[str, Any]] = []
    for item in conversations_with_eval:
        conv_shift = get_conversation_shift(item["conversation"])
        if conv_shift and conv_shift["id"] == shift_id:
            in_shift.append(item)

    total_in_shift = len(in_shift)
    rng = random.Random(f"{date}::{shift_id}")

    priority_1: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for item in in_shift:
        ev = item["evaluation"] or {}
        if _is_blacklist(ev) or _is_complaint(ev):
            priority_1.append(item)
        else:
            rest.append(item)
    # P1 has no quota cap: every blacklist/complaint conv must surface.

    rest.sort(key=lambda x: _score_percent(x["evaluation"] or {}))
    priority_2 = rest[: quota["priority_2"]]
    used_ids = {id(x) for x in priority_2}
    rest_after_p2 = [x for x in rest if id(x) not in used_ids]

    medium_pool = [
        x for x in rest_after_p2
        if LOW_THRESHOLD_PCT <= _score_percent(x["evaluation"] or {}) < MEDIUM_THRESHOLD_PCT
    ]
    rng.shuffle(medium_pool)
    priority_3 = medium_pool[: quota["priority_3"]]
    used_ids |= {id(x) for x in priority_3}

    high_pool = [
        x for x in rest_after_p2
        if _score_percent(x["evaluation"] or {}) >= HIGH_THRESHOLD_PCT
        and id(x) not in used_ids
    ]
    rng.shuffle(high_pool)
    priority_4 = high_pool[: quota["priority_4"]]

    total_selected = len(priority_1) + len(priority_2) + len(priority_3) + len(priority_4)

    return {
        "shift": get_shift_by_id(shift_id),
        "date": date,
        "total_in_shift": total_in_shift,
        "priority_1": priority_1,
        "priority_2": priority_2,
        "priority_3": priority_3,
        "priority_4": priority_4,
        "total_selected": total_selected,
        "is_complete": total_selected >= quota["total"],
        "quota": quota,
    }
