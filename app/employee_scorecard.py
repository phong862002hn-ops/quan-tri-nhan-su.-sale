from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.grading import grade_score
from app.training import RULE_SKILL_MAP, map_findings_to_skill_gaps, recommend_training_for_employee


def _normalize_result(evaluation_result: Any) -> dict[str, Any]:
    if hasattr(evaluation_result, "to_dict"):
        return evaluation_result.to_dict()
    return evaluation_result


def build_employee_scorecards(
    conversations: list[Any],
    evaluation_results: list[Any],
    training_modules: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    conversation_by_id = {conversation.external_id: conversation for conversation in conversations}
    grouped_results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    employee_info: dict[str, dict[str, str]] = {}

    for raw_result in evaluation_results:
        result = _normalize_result(raw_result)
        conversation = conversation_by_id.get(result["conversation_id"])
        if not conversation or not conversation.employee:
            continue
        employee_id = conversation.employee.id
        employee_info[employee_id] = {
            "employee_id": conversation.employee.id,
            "employee_name": conversation.employee.name,
        }
        grouped_results[employee_id].append(result)

    scorecards = []
    for employee_id, results in grouped_results.items():
        total_score = sum(float(item["total_score"]) for item in results)
        conversation_count = len(results)
        average_score = round(total_score / conversation_count, 2) if conversation_count else 0.0
        passed_rate = round(sum(1 for item in results if item["passed"]) / conversation_count, 2) if conversation_count else 0.0
        blacklist_count = sum(1 for item in results if item["blacklist_triggered"])

        skill_samples: dict[str, list[float]] = defaultdict(list)
        failed_skill_counts: dict[str, int] = defaultdict(int)

        for result in results:
            for finding in result.get("findings", []):
                skill = RULE_SKILL_MAP.get(finding["rule_id"])
                if not skill:
                    continue
                if float(finding["max_score"]) > 0:
                    ratio = (float(finding["score"]) / float(finding["max_score"])) * 100
                    skill_samples[skill].append(round(ratio, 2))
                if not finding["passed"]:
                    failed_skill_counts[skill] += 1

            for finding in result.get("blacklist_findings", []):
                skill = RULE_SKILL_MAP.get(finding["rule_id"])
                if not skill:
                    continue
                failed_skill_counts[skill] += 1
                skill_samples[skill].append(0.0)

        skill_scores = {
            skill: round(sum(samples) / len(samples), 2)
            for skill, samples in skill_samples.items()
            if samples
        }
        top_failed_skills = [
            {"skill": skill, "failed_count": count}
            for skill, count in sorted(failed_skill_counts.items(), key=lambda item: (-item[1], item[0]))
            if count > 0
        ]

        training_recommendations = recommend_training_for_employee(results, training_modules)
        scorecards.append(
            {
                "employee_id": employee_info[employee_id]["employee_id"],
                "employee_name": employee_info[employee_id]["employee_name"],
                "conversation_count": conversation_count,
                "average_score": average_score,
                "grade": grade_score(average_score, False),
                "passed_rate": passed_rate,
                "blacklist_count": blacklist_count,
                "skill_scores": skill_scores,
                "top_failed_skills": top_failed_skills,
                "training_recommendations": training_recommendations,
            }
        )

    return sorted(scorecards, key=lambda item: (item["average_score"], item["employee_name"]))
