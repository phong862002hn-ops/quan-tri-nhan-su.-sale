from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas import ReviewNote


BASE_DIR = Path(__file__).resolve().parent.parent
REVIEWS_DIR = BASE_DIR / "data" / "reviews"


def _review_path(conversation_id: str) -> Path:
    safe_id = conversation_id.replace("/", "_").replace("\\", "_")
    return REVIEWS_DIR / f"{safe_id}.json"


def save_review(
    conversation_id: str,
    reviewer: str,
    note: str,
    score_override: float | None = None,
) -> ReviewNote:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review = ReviewNote(
        conversation_id=conversation_id,
        reviewer=reviewer,
        note=note,
        score_override=score_override,
        reviewed_at=reviewed_at,
    )
    path = _review_path(conversation_id)
    with path.open("w", encoding="utf-8") as f:
        json.dump(review.to_dict(), f, ensure_ascii=False, indent=2)
    return review


def load_review(conversation_id: str) -> ReviewNote | None:
    path = _review_path(conversation_id)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return ReviewNote.from_dict(json.load(f))


def load_all_reviews() -> dict[str, ReviewNote]:
    if not REVIEWS_DIR.exists():
        return {}
    reviews: dict[str, ReviewNote] = {}
    for path in REVIEWS_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                review = ReviewNote.from_dict(json.load(f))
            reviews[review.conversation_id] = review
        except (json.JSONDecodeError, KeyError):
            continue
    return reviews


def delete_review(conversation_id: str) -> bool:
    path = _review_path(conversation_id)
    if path.exists():
        path.unlink()
        return True
    return False
