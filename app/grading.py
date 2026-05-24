"""Score → grade mapping.

Grades are anchored to percentage of `effective_max_score` (the conversation's
applicable max after rules skipped by channel/conditional filters), not the
absolute ruleset max. This keeps a Shopee conversation graded on 98 points
comparable to a Facebook one graded on 105.
"""
from __future__ import annotations


GRADE_THRESHOLDS = [
    (90.0, "Xuất sắc"),
    (75.0, "Tốt"),
    (50.0, "Trung bình"),
]


def grade_score(
    total_score: float,
    blacklist_triggered: bool,
    max_score: float | None = None,
) -> str:
    """Return the Vietnamese grade label.

    `max_score` is the conversation's *effective* max (skipped rules excluded).
    For backward compatibility callers may omit it, in which case the legacy
    behaviour (thresholds applied to absolute score) is used."""
    if blacklist_triggered:
        return "Không đạt"
    if max_score is None:
        percent = float(total_score)
    else:
        if max_score <= 0:
            return "Không đánh giá được"
        percent = (float(total_score) / float(max_score)) * 100
    for threshold, label in GRADE_THRESHOLDS:
        if percent >= threshold:
            return label
    return "Không đạt"
