"""Lead review persistence on top of the shared SQLite cache.

The `lead_reviews` table is declared in `cache_db.SCHEMA`; this module holds
the read/write helpers. Each row tracks one (conversation, shift) tuple and
moves through statuses:

  pending → confirmed | edited | skipped

A re-review can promote from confirmed/edited back to pending by overwriting
with the new status. The plan's "no double review across days" rule is
enforced by `upsert_pending_review` skipping if a row already exists.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app import cache_db


ALLOWED_STATUSES = {"pending", "confirmed", "edited", "skipped"}


def upsert_pending_review(
    db_path: Path,
    conversation_id: str,
    shift_id: str,
    shift_date: str,
    app_score: float,
    app_max_score: float,
    app_blacklist: bool,
    snapshot: dict[str, Any] | None,
    last_message_at: int | None,
) -> None:
    """Create a 'pending' row if none exists for this conversation. Idempotent:
    callers can re-invoke without overwriting a lead's completed review."""
    conn = cache_db._connect(db_path)
    with cache_db._write_lock:
        row = conn.execute(
            "SELECT status FROM lead_reviews WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is not None:
            return  # already tracked — do not overwrite review state
        conn.execute(
            """
            INSERT INTO lead_reviews
                (conversation_id, shift_id, shift_date,
                 app_score, app_max_score, app_blacklist,
                 lead_score, lead_blacklist, lead_comment,
                 status, reviewed_at, snapshot_json, last_message_at_at_review)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'pending', NULL, ?, ?)
            """,
            (
                conversation_id,
                shift_id,
                shift_date,
                float(app_score or 0),
                float(app_max_score or 0),
                1 if app_blacklist else 0,
                json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
                int(last_message_at) if last_message_at else None,
            ),
        )


def submit_lead_review(
    db_path: Path,
    conversation_id: str,
    lead_score: float | None,
    lead_blacklist: bool,
    lead_comment: str,
    status: str,
) -> None:
    """Persist the lead's decision. `status` must be one of {confirmed, edited,
    skipped}. For 'confirmed' the caller is expected to have copied
    `lead_score = app_score`; for 'edited' lead_score can differ."""
    if status not in {"confirmed", "edited", "skipped"}:
        raise ValueError(f"Invalid status for submit: {status!r}")
    conn = cache_db._connect(db_path)
    with cache_db._write_lock:
        conn.execute(
            """
            UPDATE lead_reviews
            SET lead_score = ?,
                lead_blacklist = ?,
                lead_comment = ?,
                status = ?,
                reviewed_at = ?
            WHERE conversation_id = ?
            """,
            (
                None if lead_score is None else float(lead_score),
                1 if lead_blacklist else 0,
                lead_comment or "",
                status,
                int(time.time()),
                conversation_id,
            ),
        )


def get_review(db_path: Path, conversation_id: str) -> dict[str, Any] | None:
    conn = cache_db._connect(db_path)
    row = conn.execute(
        """
        SELECT conversation_id, shift_id, shift_date, app_score, app_max_score,
               app_blacklist, lead_score, lead_blacklist, lead_comment, status,
               reviewed_at, snapshot_json, last_message_at_at_review
        FROM lead_reviews WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    if not row:
        return None
    return {k: row[k] for k in row.keys()}


def list_reviews_for_shift(
    db_path: Path,
    shift_date: str,
    shift_id: str,
) -> list[dict[str, Any]]:
    conn = cache_db._connect(db_path)
    rows = conn.execute(
        """
        SELECT conversation_id, status, lead_score, app_score, app_max_score,
               reviewed_at, last_message_at_at_review
        FROM lead_reviews
        WHERE shift_date = ? AND shift_id = ?
        """,
        (shift_date, shift_id),
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def get_shift_progress(
    db_path: Path,
    shift_date: str,
    shift_id: str,
) -> dict[str, Any]:
    conn = cache_db._connect(db_path)
    counts = {"pending": 0, "confirmed": 0, "edited": 0, "skipped": 0}
    total = 0
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM lead_reviews WHERE shift_date = ? AND shift_id = ? GROUP BY status",
        (shift_date, shift_id),
    ):
        if row["status"] in counts:
            counts[row["status"]] = int(row["n"])
        total += int(row["n"])
    done = counts["confirmed"] + counts["edited"] + counts["skipped"]
    percent = int(round(done * 100 / total)) if total else 0
    return {
        "total": total,
        "pending": counts["pending"],
        "confirmed": counts["confirmed"],
        "edited": counts["edited"],
        "skipped": counts["skipped"],
        "done": done,
        "progress_percent": percent,
    }


def get_recent_shifts_summary(
    db_path: Path,
    limit_days: int = 14,
) -> list[dict[str, Any]]:
    """One row per (date, shift_id) sorted newest first, for the dashboard
    history strip."""
    conn = cache_db._connect(db_path)
    rows = conn.execute(
        """
        SELECT shift_date, shift_id, status, COUNT(*) AS n
        FROM lead_reviews
        GROUP BY shift_date, shift_id, status
        ORDER BY shift_date DESC
        """,
    ).fetchall()
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["shift_date"], r["shift_id"])
        bucket = by_key.setdefault(key, {
            "shift_date": r["shift_date"],
            "shift_id": r["shift_id"],
            "total": 0,
            "pending": 0,
            "confirmed": 0,
            "edited": 0,
            "skipped": 0,
        })
        bucket["total"] += int(r["n"])
        if r["status"] in bucket:
            bucket[r["status"]] = int(r["n"])
    out = list(by_key.values())
    out.sort(key=lambda x: (x["shift_date"], x["shift_id"]), reverse=True)
    return out[:limit_days * 3]
