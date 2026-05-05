from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RULE_SKILL_MAP = {
    "opening_greeting": "attitude_style",
    "polite_language": "attitude_style",
    "max_emoji_per_message": "attitude_style",
    "emoji_limit": "attitude_style",
    "ask_hair_history": "hair_analysis",
    "hair_history_questions": "hair_analysis",
    "request_current_hair_photo": "hair_analysis",
    "request_current_photo": "hair_analysis",
    "no_current_hair_photo": "hair_analysis",
    "explain_color_result": "product_consulting",
    "color_consulting": "product_consulting",
    "send_feedback_image": "product_consulting",
    "feedback_reference": "product_consulting",
    "technical_instruction": "technical_guidance",
    "technical_guidance": "technical_guidance",
    "sop_discovery": "sop_compliance",
    "sop_solution": "sop_compliance",
    "sop_commitment": "sop_compliance",
    "sop_close_order": "sop_compliance",
    "sop_closing": "closing_skill",
    "employee_closing": "closing_skill",
    "customer_unanswered": "closing_skill",
    "sop_aftercare": "aftercare",
    "complaint_flow": "complaint_handling",
    "complaint_no_argument": "complaint_handling",
    "incorrect_product_info": "product_knowledge",
    "wrong_product_info": "product_knowledge",
    "negative_tone": "attitude_style",
}

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _normalize_result(evaluation_result: Any) -> dict[str, Any]:
    if hasattr(evaluation_result, "to_dict"):
        return evaluation_result.to_dict()
    return evaluation_result


def load_training_modules(path: str | Path) -> dict[str, dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as file:
        modules = json.load(file)
    return {item["skill"]: item for item in modules}


def map_findings_to_skill_gaps(evaluation_result: Any) -> list[dict[str, Any]]:
    result = _normalize_result(evaluation_result)
    conversation_id = result["conversation_id"]
    gaps: list[dict[str, Any]] = []

    for finding in result.get("findings", []):
        if finding.get("passed"):
            continue
        skill = RULE_SKILL_MAP.get(finding["rule_id"])
        if not skill:
            continue
        gaps.append(
            {
                "skill": skill,
                "rule_id": finding["rule_id"],
                "rule_name": finding["rule_name"],
                "severity": finding["severity"],
                "reason": finding["explanation"],
                "blacklist": False,
                "conversation_id": conversation_id,
            }
        )

    for finding in result.get("blacklist_findings", []):
        skill = RULE_SKILL_MAP.get(finding["rule_id"])
        if not skill:
            continue
        gaps.append(
            {
                "skill": skill,
                "rule_id": finding["rule_id"],
                "rule_name": finding["rule_name"],
                "severity": finding["severity"],
                "reason": finding["explanation"],
                "blacklist": True,
                "conversation_id": conversation_id,
            }
        )
    return gaps


def _build_recommendation(
    skill: str,
    gaps: list[dict[str, Any]],
    training_modules: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    module = training_modules.get(skill)
    if not module:
        return None

    fail_count = len(gaps)
    has_critical = any(item["severity"] == "critical" or item["blacklist"] for item in gaps)
    priority = "high" if has_critical or fail_count >= 2 else "medium"
    unique_rules = sorted({item["rule_name"] for item in gaps})
    reason = f"Fail {fail_count} lần ở skill {skill}: " + ", ".join(unique_rules[:4])
    related_conversation_ids = sorted({item["conversation_id"] for item in gaps})
    return {
        "skill": skill,
        "priority": priority,
        "reason": reason,
        "recommended_module_id": module["id"],
        "recommended_module_title": module["title"],
        "sample_phrases": module.get("sample_phrases", []),
        "practice_tasks": module.get("practice_tasks", []),
        "related_conversation_ids": related_conversation_ids,
    }


def recommend_training_for_evaluation(
    evaluation_result: Any,
    training_modules: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    gaps = map_findings_to_skill_gaps(evaluation_result)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in gaps:
        grouped.setdefault(item["skill"], []).append(item)

    recommendations = []
    for skill, items in grouped.items():
        recommendation = _build_recommendation(skill, items, training_modules)
        if recommendation:
            recommendations.append(recommendation)
    return sorted(recommendations, key=lambda item: (PRIORITY_ORDER[item["priority"]], item["skill"]))


def recommend_training_for_employee(
    employee_results: list[Any],
    training_modules: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in employee_results:
        for gap in map_findings_to_skill_gaps(result):
            grouped.setdefault(gap["skill"], []).append(gap)

    recommendations = []
    for skill, items in grouped.items():
        recommendation = _build_recommendation(skill, items, training_modules)
        if recommendation:
            recommendations.append(recommendation)
    return sorted(
        recommendations,
        key=lambda item: (PRIORITY_ORDER[item["priority"]], -len(item["related_conversation_ids"]), item["skill"]),
    )
