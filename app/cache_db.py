"""SQLite cache for Nhanh conversations/messages/evaluations.

Schema, upsert helpers, and read queries used by the background sync worker
and the viewer. Single-process, multi-thread safe via WAL + write lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_write_lock = threading.Lock()
_connections: dict[tuple[int, str], sqlite3.Connection] = {}


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    page_id TEXT,
    channel TEXT,
    customer_name TEXT,
    updated_at INTEGER,
    last_message_at INTEGER,
    last_message_sender TEXT,
    summary_json TEXT,
    fetched_at INTEGER,
    sync_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_page ON conversations(page_id);
CREATE INDEX IF NOT EXISTS idx_conv_last_sender ON conversations(last_message_sender, last_message_at);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT,
    message_id TEXT,
    created_at INTEGER,
    sender_type TEXT,
    sender_name TEXT,
    content TEXT,
    attachments_json TEXT,
    raw_json TEXT,
    PRIMARY KEY (conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS evaluations (
    conversation_id TEXT PRIMARY KEY,
    ruleset_version TEXT,
    total_score REAL,
    max_score REAL,
    grade TEXT,
    blacklist_triggered INTEGER,
    result_json TEXT,
    evaluated_at INTEGER
);

CREATE TABLE IF NOT EXISTS sla_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    customer_message_at INTEGER,
    detected_at INTEGER,
    resolved_at INTEGER,
    business_minutes_at_detection INTEGER,
    status TEXT,
    UNIQUE(conversation_id, customer_message_at)
);
CREATE INDEX IF NOT EXISTS idx_sla_status ON sla_violations(status, detected_at DESC);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS lead_reviews (
    conversation_id TEXT PRIMARY KEY,
    shift_id TEXT,
    shift_date TEXT,
    app_score REAL,
    app_max_score REAL,
    app_blacklist INTEGER,
    lead_score REAL,
    lead_blacklist INTEGER,
    lead_comment TEXT,
    status TEXT,                  -- 'pending' | 'confirmed' | 'edited' | 'skipped'
    reviewed_at INTEGER,
    snapshot_json TEXT,
    last_message_at_at_review INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lr_shift ON lead_reviews(shift_date, shift_id, status);
CREATE INDEX IF NOT EXISTS idx_lr_status ON lead_reviews(status, reviewed_at DESC);
"""


_LEGACY_COLUMNS_TO_ADD = [
    ("conversations", "last_message_at", "INTEGER"),
    ("conversations", "last_message_sender", "TEXT"),
    ("conversations", "sync_source", "TEXT"),
]


def _connect(path: Path) -> sqlite3.Connection:
    key = (threading.get_ident(), str(path))
    conn = _connections.get(key)
    if conn is None:
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _connections[key] = conn
    return conn


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    with _write_lock:
        conn.executescript(SCHEMA)
        # Bring older DBs up to current schema without losing data.
        for table, column, coltype in _LEGACY_COLUMNS_TO_ADD:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_last_sender "
            "ON conversations(last_message_sender, last_message_at)"
        )


def _extract_summary(item: dict[str, Any]) -> dict[str, Any]:
    cid = str(item.get("id") or item.get("conversationId") or "")
    return {
        "id": cid,
        "page_id": str(item.get("pageId") or ""),
        "channel": str(item.get("channel") or ""),
        "customer_name": str(item.get("pageUserName") or item.get("customerName") or ""),
        "updated_at": int(item.get("updatedAt") or 0),
    }


def upsert_conversation(
    path: Path,
    item: dict[str, Any],
    *,
    sync_source: str | None = None,
    last_message_at: int | None = None,
    last_message_sender: str | None = None,
) -> None:
    fields = _extract_summary(item)
    if not fields["id"]:
        return
    conn = _connect(path)
    with _write_lock:
        conn.execute(
            """
            INSERT INTO conversations
                (id, page_id, channel, customer_name, updated_at,
                 last_message_at, last_message_sender,
                 summary_json, fetched_at, sync_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                page_id=excluded.page_id,
                channel=excluded.channel,
                customer_name=excluded.customer_name,
                updated_at=excluded.updated_at,
                last_message_at=COALESCE(excluded.last_message_at, conversations.last_message_at),
                last_message_sender=COALESCE(excluded.last_message_sender, conversations.last_message_sender),
                summary_json=excluded.summary_json,
                fetched_at=excluded.fetched_at,
                sync_source=COALESCE(excluded.sync_source, conversations.sync_source)
            """,
            (
                fields["id"],
                fields["page_id"],
                fields["channel"],
                fields["customer_name"],
                fields["updated_at"],
                last_message_at,
                last_message_sender,
                json.dumps(item, ensure_ascii=False),
                int(time.time()),
                sync_source,
            ),
        )


def get_conversation_row(path: Path, conversation_id: str) -> dict[str, Any] | None:
    conn = _connect(path)
    row = conn.execute(
        """
        SELECT id, page_id, channel, customer_name, updated_at,
               last_message_at, last_message_sender, summary_json,
               fetched_at, sync_source
        FROM conversations WHERE id = ?
        """,
        (conversation_id,),
    ).fetchone()
    if not row:
        return None
    return {k: row[k] for k in row.keys()}


def upsert_messages(path: Path, conversation_id: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    conn = _connect(path)
    rows = []
    for item in items:
        mid = str(item.get("id") or item.get("messageId") or "")
        if not mid:
            continue
        rows.append(
            (
                conversation_id,
                mid,
                int(item.get("createdAt") or 0),
                str(item.get("senderType") or ""),
                str(item.get("senderName") or item.get("fromName") or ""),
                str(item.get("message") or item.get("content") or item.get("text") or ""),
                json.dumps(item.get("attachments") or [], ensure_ascii=False),
                json.dumps(item, ensure_ascii=False),
            )
        )
    if not rows:
        return
    with _write_lock:
        conn.executemany(
            """
            INSERT INTO messages
                (conversation_id, message_id, created_at, sender_type, sender_name,
                 content, attachments_json, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, message_id) DO UPDATE SET
                created_at=excluded.created_at,
                sender_type=excluded.sender_type,
                sender_name=excluded.sender_name,
                content=excluded.content,
                attachments_json=excluded.attachments_json,
                raw_json=excluded.raw_json
            """,
            rows,
        )


def upsert_evaluation(
    path: Path,
    conversation_id: str,
    ruleset_version: str,
    result: dict[str, Any],
) -> None:
    conn = _connect(path)
    with _write_lock:
        conn.execute(
            """
            INSERT INTO evaluations
                (conversation_id, ruleset_version, total_score, max_score,
                 grade, blacklist_triggered, result_json, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                ruleset_version=excluded.ruleset_version,
                total_score=excluded.total_score,
                max_score=excluded.max_score,
                grade=excluded.grade,
                blacklist_triggered=excluded.blacklist_triggered,
                result_json=excluded.result_json,
                evaluated_at=excluded.evaluated_at
            """,
            (
                conversation_id,
                ruleset_version,
                float(result.get("total_score") or 0),
                float(result.get("max_score") or 0),
                str(result.get("grade") or ""),
                1 if result.get("blacklist_triggered") else 0,
                json.dumps(result, ensure_ascii=False),
                int(time.time()),
            ),
        )


def _date_window(date_filter: str) -> tuple[int | None, int | None]:
    if date_filter == "all":
        return (None, None)
    presets = {
        "today": (0, 0),
        "yesterday": (1, 1),
        "2days": (0, 1),
        "7days": (0, 7),
        "30days": (0, 30),
    }
    if date_filter not in presets:
        return (None, None)
    days_back_to, days_back_from = presets[date_filter]
    vn = timezone(timedelta(hours=7))
    now = datetime.now(vn)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=days_back_from)
    end = today_start - timedelta(days=days_back_to) + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def list_recent_conversations(
    path: Path,
    date_filter: str = "7days",
    channel_filter: str | None = None,
    state_filter: str = "all",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """state_filter: 'all' | 'graded' | 'active'.
    - 'graded': only conversations with an evaluation row.
    - 'active': conversations without an evaluation row (in-flight or last
      message from sale — not yet eligible per the grading guard).
    """
    conn = _connect(path)
    sql = """
        SELECT c.id, c.page_id, c.channel, c.customer_name, c.updated_at,
               c.last_message_at, c.last_message_sender, c.summary_json,
               e.ruleset_version, e.total_score, e.max_score, e.grade,
               e.blacklist_triggered, e.result_json, e.evaluated_at
        FROM conversations c
        LEFT JOIN evaluations e ON e.conversation_id = c.id
        WHERE 1=1
    """
    params: list[Any] = []
    from_ts, to_ts = _date_window(date_filter)
    if from_ts is not None and to_ts is not None:
        sql += " AND c.updated_at >= ? AND c.updated_at < ?"
        params.extend([from_ts, to_ts])
    if channel_filter:
        sql += " AND c.channel = ?"
        params.append(channel_filter)
    if state_filter == "graded":
        sql += " AND e.evaluated_at IS NOT NULL"
    elif state_filter == "active":
        sql += " AND e.evaluated_at IS NULL"
    sql += " ORDER BY c.updated_at DESC LIMIT ?"
    params.append(limit)
    items = []
    for row in conn.execute(sql, params):
        summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        qa_summary = None
        if row["evaluated_at"]:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            failed_count = len([f for f in result.get("findings", []) if not f.get("passed")])
            qa_summary = {
                "total_score": row["total_score"],
                "max_score": row["max_score"],
                "grade": row["grade"],
                "failed_rule_count": failed_count,
                "blacklist_triggered": bool(row["blacklist_triggered"]),
                "blacklist_count": len(result.get("blacklist_findings", [])),
            }
        merged = dict(summary)
        merged["id"] = row["id"]
        merged["qa_summary"] = qa_summary
        merged["last_message_at"] = row["last_message_at"]
        merged["last_message_sender"] = row["last_message_sender"]
        items.append(merged)
    return items


def list_conversations_with_evaluations(
    path: Path,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    """Return conversations whose last_message_at falls in [start_ts, end_ts),
    paired with their evaluation (if any). Returned shape matches what
    shift_selector expects: {'conversation': {...}, 'evaluation': {...}}.

    The "conversation" dict is built from the summary_json plus a `messages`
    list reconstructed from the messages table (sender_type + sent_at ISO),
    enough for the shift classifier to find the latest message timestamp.
    """
    conn = _connect(path)
    rows = conn.execute(
        """
        SELECT c.id, c.page_id, c.channel, c.customer_name, c.updated_at,
               c.last_message_at, c.last_message_sender, c.summary_json,
               e.result_json
        FROM conversations c
        LEFT JOIN evaluations e ON e.conversation_id = c.id
        WHERE c.last_message_at IS NOT NULL
          AND c.last_message_at >= ?
          AND c.last_message_at < ?
        ORDER BY c.last_message_at DESC
        """,
        (start_ts, end_ts),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        evaluation = json.loads(row["result_json"]) if row["result_json"] else None
        # Minimal Conversation-shaped dict for shift classifier. We don't
        # rehydrate full messages — only the last_message_at is needed.
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        vn = _tz(_td(hours=7))
        last_iso = _dt.fromtimestamp(int(row["last_message_at"]), vn).isoformat()
        conv_dict = {
            "external_id": row["id"],
            "channel": summary.get("channel") or row["channel"],
            "metadata": {"page_id": row["page_id"]},
            "messages": [{
                "id": "_synthetic_last",
                "sender_type": row["last_message_sender"] or "",
                "text": "",
                "attachments": [],
                "sent_at": last_iso,
            }],
            "customer_name": row["customer_name"] or summary.get("pageUserName"),
            "summary": summary,
            "last_message_at": row["last_message_at"],
        }
        out.append({"conversation": conv_dict, "evaluation": evaluation})
    return out


def count_by_state(path: Path) -> dict[str, int]:
    conn = _connect(path)
    graded = conn.execute(
        "SELECT COUNT(*) AS n FROM conversations c "
        "INNER JOIN evaluations e ON e.conversation_id = c.id"
    ).fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
    alerts = conn.execute(
        "SELECT COUNT(*) AS n FROM sla_violations WHERE status = 'open'"
    ).fetchone()["n"]
    return {
        "graded": int(graded),
        "active": int(total) - int(graded),
        "all": int(total),
        "alert": int(alerts),
    }


# ---------------- SLA violations ----------------

def record_sla_violation(
    path: Path,
    conversation_id: str,
    customer_message_at: int,
    detected_at: int,
    business_minutes_at_detection: int,
) -> None:
    conn = _connect(path)
    with _write_lock:
        conn.execute(
            """
            INSERT INTO sla_violations
                (conversation_id, customer_message_at, detected_at,
                 resolved_at, business_minutes_at_detection, status)
            VALUES (?, ?, ?, NULL, ?, 'open')
            ON CONFLICT(conversation_id, customer_message_at) DO UPDATE SET
                business_minutes_at_detection=excluded.business_minutes_at_detection
                WHERE status='open'
            """,
            (conversation_id, customer_message_at, detected_at, business_minutes_at_detection),
        )


def resolve_sla_violation(
    path: Path,
    conversation_id: str,
    customer_message_at: int,
    resolved_at: int,
) -> None:
    conn = _connect(path)
    with _write_lock:
        conn.execute(
            """
            UPDATE sla_violations
            SET status='resolved', resolved_at=?
            WHERE conversation_id=? AND customer_message_at=? AND status='open'
            """,
            (resolved_at, conversation_id, customer_message_at),
        )


def list_active_sla_violations(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    conn = _connect(path)
    rows = conn.execute(
        """
        SELECT v.id, v.conversation_id, v.customer_message_at, v.detected_at,
               v.business_minutes_at_detection, v.status,
               c.customer_name, c.channel, c.page_id, c.summary_json
        FROM sla_violations v
        LEFT JOIN conversations c ON c.id = v.conversation_id
        WHERE v.status = 'open'
        ORDER BY v.detected_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        out.append({
            "violation_id": row["id"],
            "conversation_id": row["conversation_id"],
            "customer_message_at": row["customer_message_at"],
            "detected_at": row["detected_at"],
            "business_minutes_at_detection": row["business_minutes_at_detection"],
            "customer_name": row["customer_name"] or summary.get("pageUserName") or "Khách",
            "channel": row["channel"] or summary.get("channel"),
            "page_id": row["page_id"] or summary.get("pageId"),
            "last_message": summary.get("lastMessage") or "",
        })
    return out


def get_conversation_summary(path: Path, conversation_id: str) -> dict[str, Any] | None:
    conn = _connect(path)
    row = conn.execute(
        "SELECT summary_json FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if not row or not row["summary_json"]:
        return None
    return json.loads(row["summary_json"])


def get_messages(path: Path, conversation_id: str) -> list[dict[str, Any]]:
    conn = _connect(path)
    rows = conn.execute(
        """
        SELECT raw_json FROM messages
        WHERE conversation_id = ?
        ORDER BY created_at ASC
        """,
        (conversation_id,),
    ).fetchall()
    return [json.loads(row["raw_json"]) for row in rows if row["raw_json"]]


def get_evaluation(
    path: Path,
    conversation_id: str,
    ruleset_version: str | None = None,
) -> dict[str, Any] | None:
    conn = _connect(path)
    if ruleset_version:
        row = conn.execute(
            "SELECT result_json FROM evaluations WHERE conversation_id = ? AND ruleset_version = ?",
            (conversation_id, ruleset_version),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT result_json FROM evaluations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    if not row or not row["result_json"]:
        return None
    return json.loads(row["result_json"])


def conversation_count(path: Path) -> int:
    conn = _connect(path)
    row = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()
    return int(row["n"]) if row else 0


def set_sync_state(path: Path, key: str, value: str | None) -> None:
    conn = _connect(path)
    with _write_lock:
        if value is None:
            conn.execute("DELETE FROM sync_state WHERE key = ?", (key,))
        else:
            conn.execute(
                """
                INSERT INTO sync_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )


def get_sync_state(path: Path, key: str) -> str | None:
    conn = _connect(path)
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def all_sync_state(path: Path) -> dict[str, str]:
    conn = _connect(path)
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM sync_state")}
