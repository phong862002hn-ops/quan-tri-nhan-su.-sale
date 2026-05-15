from __future__ import annotations

from pathlib import Path
from typing import Any

from app.grading import grade_score
from app.rule_engine import evaluate_rule
from app.schemas import CategoryScore, Conversation, EvaluationResult, Ruleset, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"


def evaluate_conversation(
    conversation: Conversation,
    ruleset: Ruleset | None = None,
) -> EvaluationResult:
    active_ruleset = ruleset or load_ruleset(RULESET_PATH)

    findings = []
    category_scores: list[CategoryScore] = []
    total_score = 0.0

    for category in active_ruleset.categories:
        category_total = 0.0
        for rule in category.rules:
            finding = evaluate_rule(conversation, rule)
            findings.append(finding)
            category_total += finding.score
        category_scores.append(
            CategoryScore(
                category_id=category.id,
                name=category.name,
                score=round(category_total, 2),
                max_score=category.max_score,
            )
        )
        total_score += category_total

    blacklist_findings = [
        evaluate_rule(conversation, rule)
        for rule in active_ruleset.blacklist
    ]
    blacklist_failed = [finding for finding in blacklist_findings if not finding.passed]
    blacklist_triggered = bool(blacklist_failed)

    total_score = round(total_score, 2)
    if blacklist_triggered:
        total_score = min(total_score, 49.0)

    grade = grade_score(total_score, blacklist_triggered)
    passed = (not blacklist_triggered) and total_score >= 75

    return EvaluationResult(
        conversation_id=conversation.external_id,
        total_score=total_score,
        max_score=active_ruleset.max_score,
        grade=grade,
        passed=passed,
        blacklist_triggered=blacklist_triggered,
        category_scores=category_scores,
        findings=findings,
        blacklist_findings=blacklist_failed,
    )


def evaluate_conversation_dict(
    conversation_data: dict[str, Any],
    ruleset: Ruleset | None = None,
) -> dict[str, Any]:
    conversation = Conversation.from_dict(conversation_data)
    return evaluate_conversation(conversation, ruleset=ruleset).to_dict()
