from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.grading import grade_score
from app.rule_engine import evaluate_rule
from app.rule_history import record_version
from app.schemas import CategoryScore, Conversation, EvaluationResult, Ruleset, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"


def evaluate_conversation(
    conversation: Conversation,
    ruleset: Ruleset | None = None,
) -> EvaluationResult:
    active_ruleset = ruleset or load_ruleset(RULESET_PATH)
    rules_version = record_version(active_ruleset)

    findings = []
    category_scores: list[CategoryScore] = []
    total_score = 0.0
    effective_max_score = 0.0

    for category in active_ruleset.categories:
        category_total = 0.0
        category_effective_max = 0.0
        for rule in category.rules:
            finding = evaluate_rule(conversation, rule)
            findings.append(finding)
            category_total += finding.score
            category_effective_max += finding.max_score  # skipped rules contribute 0
        category_scores.append(
            CategoryScore(
                category_id=category.id,
                name=category.name,
                score=round(category_total, 2),
                max_score=round(category_effective_max, 2),
            )
        )
        total_score += category_total
        effective_max_score += category_effective_max

    blacklist_findings = [
        evaluate_rule(conversation, rule)
        for rule in active_ruleset.blacklist
    ]
    blacklist_failed = [finding for finding in blacklist_findings if not finding.passed]
    blacklist_triggered = bool(blacklist_failed)

    total_score = round(total_score, 2)
    effective_max_score = round(effective_max_score, 2)
    if blacklist_triggered:
        total_score = min(total_score, 49.0)

    grade = grade_score(total_score, blacklist_triggered, max_score=effective_max_score)
    # Pass threshold: still 75% of the effective max, mirroring the grade map.
    passed = (not blacklist_triggered) and (
        effective_max_score > 0 and (total_score / effective_max_score) * 100 >= 75
    )

    return EvaluationResult(
        conversation_id=conversation.external_id,
        total_score=total_score,
        max_score=effective_max_score,
        grade=grade,
        passed=passed,
        blacklist_triggered=blacklist_triggered,
        category_scores=category_scores,
        findings=findings,
        blacklist_findings=blacklist_failed,
        rules_version=rules_version,
    )


def evaluate_conversation_dict(
    conversation_data: dict[str, Any],
    ruleset: Ruleset | None = None,
) -> dict[str, Any]:
    conversation = Conversation.from_dict(conversation_data)
    return evaluate_conversation(conversation, ruleset=ruleset).to_dict()


def _parse_sent_at_unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 10_000_000_000 else number
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp())


def _get_idle_threshold_seconds() -> float:
    raw = os.environ.get("QA_IDLE_THRESHOLD_HOURS", "24")
    try:
        return max(0.0, float(raw)) * 3600
    except ValueError:
        return 24 * 3600


def is_conversation_ready_for_grading(
    conversation: Conversation,
    now: float | None = None,
) -> bool:
    """Guard: only grade a conversation when (1) it has been idle for at least
    `QA_IDLE_THRESHOLD_HOURS` (default 24h) AND (2) the most recent message is
    from the customer (i.e. the sale's last action did not get a follow-up).
    """
    if not conversation.messages:
        return False
    last_msg = conversation.messages[-1]
    sent_unix = _parse_sent_at_unix(last_msg.sent_at)
    if sent_unix is None:
        return False
    if (now or time.time()) - sent_unix < _get_idle_threshold_seconds():
        return False
    return last_msg.sender_type == "customer"
