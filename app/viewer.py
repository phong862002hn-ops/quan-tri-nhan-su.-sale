from __future__ import annotations

from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
from urllib.parse import parse_qs, urlparse

from app.employee_scorecard import build_employee_scorecards
from app.evaluator import evaluate_conversation
from app.nhanh_adapter import build_conversation
from app.nhanh_client import (
    NhanhClientError,
    extract_items,
    list_conversations as nhanh_list_conversations,
    list_messages as nhanh_list_messages,
    load_config as load_nhanh_config,
)
from app.schemas import Conversation, load_conversations, load_ruleset
from app.training import load_training_modules


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"
TRAINING_PATH = BASE_DIR / "data" / "training_modules.json"
NHANH_CONFIG_PATH = BASE_DIR / "data" / "nhanh_config.local.json"
NHANH_CACHE_DB = BASE_DIR / "data" / "nhanh_cache.db"


import os as _os
import threading as _threading

_light_worker = None
_heavy_worker = None
_worker_lock = _threading.Lock()


def _resolve_interval_seconds(env_var: str, default_hours: float) -> int:
    raw = _os.environ.get(env_var, str(default_hours))
    try:
        hours = float(raw)
    except ValueError:
        hours = default_hours
    return max(60, int(hours * 3600))


def _ensure_workers(ruleset):
    """Lazy init of the light + heavy sync workers. Returns (light, heavy)."""
    global _light_worker, _heavy_worker
    if _light_worker is not None and _heavy_worker is not None:
        return _light_worker, _heavy_worker
    if not NHANH_CONFIG_PATH.exists():
        return None, None
    with _worker_lock:
        if _light_worker is not None and _heavy_worker is not None:
            return _light_worker, _heavy_worker
        try:
            from app import cache_db
            from app.sync_worker import LightSyncWorker, HeavySyncWorker
            cache_db.init_db(NHANH_CACHE_DB)
            config = load_nhanh_config(NHANH_CONFIG_PATH)
            version = ruleset.compute_version()
            if _heavy_worker is None:
                _heavy_worker = HeavySyncWorker(
                    config=config, ruleset=ruleset, db_path=NHANH_CACHE_DB,
                    ruleset_version=version,
                    interval_s=_resolve_interval_seconds("NHANH_HEAVY_SYNC_HOURS", 4),
                )
                _heavy_worker.start()
            if _light_worker is None:
                light_minutes_raw = _os.environ.get("NHANH_LIGHT_SYNC_MINUTES", "10")
                try:
                    light_minutes = float(light_minutes_raw)
                except ValueError:
                    light_minutes = 10.0
                _light_worker = LightSyncWorker(
                    config=config, ruleset=ruleset, db_path=NHANH_CACHE_DB,
                    ruleset_version=version,
                    interval_s=max(60, int(light_minutes * 60)),
                )
                _light_worker.start()
        except Exception as exc:
            import sys as _sys
            print(f"[viewer] Failed to start sync workers: {exc}", file=_sys.stderr)
            return None, None
    return _light_worker, _heavy_worker


# Back-compat: callers from earlier plan returned a single worker. Keep the
# helper available, returning the light worker as the "primary" surface.
def _ensure_sync_worker(ruleset):
    light, _ = _ensure_workers(ruleset)
    return light


def parse_display_datetime(value: str | None) -> str:
    if not value:
        return "-"
    if isinstance(value, (int, float)):
        number = int(value)
        if number > 10_000_000_000:
            number = number // 1000
        return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def format_score(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def load_dashboard_data(ruleset=None) -> dict:
    active_ruleset = ruleset or load_ruleset(RULESET_PATH)
    conversations = load_conversations(CONVERSATIONS_PATH)
    training_modules = load_training_modules(TRAINING_PATH)
    cases = []
    evaluation_results = []
    for conversation in conversations:
        result = evaluate_conversation(conversation, active_ruleset).to_dict()
        evaluation_results.append(result)
        cases.append({"conversation": conversation, "result": result})

    scorecards = build_employee_scorecards(conversations, evaluation_results, training_modules)
    top_training_employees = sorted(
        scorecards,
        key=lambda item: (-len(item["training_recommendations"]), item["average_score"], item["employee_name"]),
    )

    weak_skill_totals: dict[str, int] = {}
    all_recommendations = []
    for scorecard in scorecards:
        for skill in scorecard["top_failed_skills"]:
            weak_skill_totals[skill["skill"]] = weak_skill_totals.get(skill["skill"], 0) + skill["failed_count"]
        for recommendation in scorecard["training_recommendations"]:
            enriched = dict(recommendation)
            enriched["employee_id"] = scorecard["employee_id"]
            enriched["employee_name"] = scorecard["employee_name"]
            all_recommendations.append(enriched)

    top_weak_skills = [
        {"skill": skill, "failed_count": count}
        for skill, count in sorted(weak_skill_totals.items(), key=lambda item: (-item[1], item[0]))
    ]
    all_recommendations.sort(key=lambda item: (item["priority"] != "high", item["employee_name"], item["skill"]))

    return {
        "cases": cases,
        "evaluation_results": evaluation_results,
        "scorecards": scorecards,
        "top_training_employees": top_training_employees,
        "top_weak_skills": top_weak_skills,
        "training_recommendations": all_recommendations,
    }


def _conversation_age_days(summary_item: dict) -> float | None:
    import time
    upd = summary_item.get("updatedAt")
    if not upd:
        return None
    try:
        return (time.time() - int(upd)) / 86400.0
    except (TypeError, ValueError):
        return None


def _has_messages(payload: dict) -> bool:
    """Nhanh trả `code: 0, messages: "No data!"` cho Shopee/TikTok (không hỗ trợ)
    hoặc hội thoại không có tin. Phân biệt với hội thoại Facebook thật sự rỗng."""
    if not isinstance(payload, dict):
        return False
    if payload.get("code") == 0:
        return False
    items = payload.get("items")
    if isinstance(items, list) and items:
        return True
    data = payload.get("data")
    if isinstance(data, list) and data:
        return True
    if isinstance(data, dict):
        for key in ("messages", "conversation", "items"):
            value = data.get(key)
            if isinstance(value, list) and value:
                return True
    return False


DATE_FILTERS = {
    "today": ("Hôm nay", 0, 0),
    "yesterday": ("Hôm qua", 1, 1),
    "2days": ("Hôm nay + hôm qua", 0, 1),
    "7days": ("7 ngày", 0, 7),
    "30days": ("30 ngày", 0, 30),
    "all": ("Tất cả", None, None),
}


def _date_window_unix(filter_key: str) -> tuple[int | None, int | None]:
    """Return (from_ts, to_ts) inclusive in unix seconds, in VN tz."""
    from datetime import datetime, timezone, timedelta
    if filter_key not in DATE_FILTERS or filter_key == "all":
        return (None, None)
    _, days_back_to, days_back_from = DATE_FILTERS[filter_key]
    VN = timezone(timedelta(hours=7))
    now = datetime.now(VN)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=days_back_from)
    end = today_start - timedelta(days=days_back_to) + timedelta(days=1)
    return (int(start.timestamp()), int(end.timestamp()))


def load_nhanh_data(
    ruleset,
    live_conversation_id: str | None = None,
    date_filter: str = "7days",
    state_filter: str = "all",
) -> dict:
    if not NHANH_CONFIG_PATH.exists():
        return {
            "enabled": False,
            "error": None,
            "recent": [],
            "live_case": None,
            "live_error": None,
            "date_filter": date_filter,
            "state_filter": state_filter,
            "total_fetched": 0,
            "sync_status": None,
            "sla_violations": [],
            "state_counts": {"all": 0, "graded": 0, "active": 0, "alert": 0},
        }

    from app import cache_db
    light, heavy = _ensure_workers(ruleset)
    light_status = light.get_status() if light else None
    heavy_status = heavy.get_status() if heavy else None

    recent_items = cache_db.list_recent_conversations(
        NHANH_CACHE_DB,
        date_filter=date_filter,
        channel_filter=None,
        state_filter=state_filter,
        limit=200,
    )
    sla_violations = cache_db.list_active_sla_violations(NHANH_CACHE_DB)
    state_counts = cache_db.count_by_state(NHANH_CACHE_DB)

    live_case = None
    live_error = None
    if live_conversation_id:
        live_case, live_error = _stale_while_revalidate_live_case(
            live_conversation_id, ruleset,
        )

    # Only surface an error if the worker that produced it has NOT had a
    # successful sync since. A stale error from 4h ago when light succeeded
    # 10min ago is just noise.
    import time as _time
    error_message = None
    for status in (light_status, heavy_status):
        if not status:
            continue
        err = status.get("last_error")
        if not err:
            continue
        err_ts = status.get("last_error_ts") or 0
        ok_ts = status.get("last_sync_ts") or 0
        if ok_ts and ok_ts >= err_ts:
            continue  # newer successful sync supersedes the error
        error_message = err
        break

    # Banner consumes a single merged status. Heavy reports per-conv progress
    # during backfill; light just toggles is_syncing.
    merged_status = dict(light_status) if light_status else {}
    if heavy_status:
        if heavy_status.get("progress"):
            merged_status["progress"] = heavy_status["progress"]
        if heavy_status.get("is_syncing"):
            merged_status["is_syncing"] = True
        merged_status["is_first_run"] = (
            (heavy_status.get("conversation_count") or 0) == 0
            and heavy_status.get("last_sync_ts") is None
        )

    return {
        "enabled": True,
        "error": error_message,
        "recent": recent_items,
        "live_case": live_case,
        "live_error": live_error,
        "date_filter": date_filter,
        "state_filter": state_filter,
        "total_fetched": len(recent_items),
        "sync_status": merged_status or None,
        "light_sync_status": light_status,
        "heavy_sync_status": heavy_status,
        "sla_violations": sla_violations,
        "state_counts": state_counts,
    }


def _stale_while_revalidate_live_case(
    conversation_id: str,
    ruleset,
) -> tuple[dict | None, str | None]:
    """Refresh a single conversation from Nhanh on demand, then evaluate.
    Falls back to whatever is in the cache if the refresh fails."""
    from app import cache_db

    config = None
    refresh_error: str | None = None
    summary_item = cache_db.get_conversation_summary(NHANH_CACHE_DB, conversation_id) or {}

    try:
        config = load_nhanh_config(NHANH_CONFIG_PATH)
    except Exception as exc:
        refresh_error = f"Khong doc duoc config Nhanh: {exc}"

    from app.sync_worker import _is_system_notification

    def _strip_notifications(payload: dict) -> dict:
        items = extract_items(payload, "messages", "items")
        clean = [it for it in items if not _is_system_notification(it)]
        out = dict(payload)
        out["data"] = clean
        return out

    if config is not None:
        try:
            messages_payload = nhanh_list_messages(config, conversation_id=conversation_id, size=50)
            if _has_messages(messages_payload):
                items = extract_items(messages_payload, "messages", "items")
                cache_db.upsert_messages(NHANH_CACHE_DB, conversation_id, items)
                clean_payload = _strip_notifications(messages_payload)
                conversation = build_conversation(
                    conversation_id, clean_payload, summary_item=summary_item or None,
                )
                result = evaluate_conversation(conversation, ruleset).to_dict()
                cache_db.upsert_evaluation(
                    NHANH_CACHE_DB, conversation_id, ruleset.compute_version(), result,
                )
                return {"conversation": conversation, "result": result}, None
            # Empty payload = Nhanh has nothing live for this conv (stale > 24d, etc).
            return _empty_message_explanation(conversation_id, summary_item)
        except NhanhClientError as exc:
            refresh_error = str(exc)

    # Refresh failed — try to render from whatever messages are still in cache.
    cached_messages = cache_db.get_messages(NHANH_CACHE_DB, conversation_id)
    if cached_messages:
        payload = _strip_notifications({"data": cached_messages})
        try:
            conversation = build_conversation(
                conversation_id, payload, summary_item=summary_item or None,
            )
            result = evaluate_conversation(conversation, ruleset).to_dict()
            warning = None
            if refresh_error:
                warning = f"Khong refresh duoc tu Nhanh ({refresh_error}). Dang dung data cache."
            return {"conversation": conversation, "result": result}, warning
        except Exception as exc:
            return None, f"Khong dung duoc data cache cho {conversation_id}: {exc}"

    return None, refresh_error or "Khong co data cho hoi thoai nay."


def _empty_message_explanation(conversation_id: str, summary_item: dict) -> tuple[None, str]:
    from app.nhanh_adapter import channel_label
    ch_label = channel_label(summary_item.get("channel"), str(summary_item.get("pageId") or ""))
    ch_text = f" (channel: {ch_label})" if ch_label and ch_label != "Unknown" else ""
    age_days = _conversation_age_days(summary_item or {})
    age_text = f", tuổi ~{age_days:.0f} ngày" if age_days is not None else ""
    if age_days is not None and age_days > 24:
        return None, (
            f"Hội thoại {conversation_id}{ch_text}{age_text} đã quá hạn lưu của Nhanh "
            "(~24 ngày). Nhanh chỉ giữ full transcript trong khoảng 24 ngày gần nhất."
        )
    return None, (
        f"Nhanh chưa có dữ liệu tin nhắn cho hội thoại {conversation_id}{ch_text}{age_text}. "
        "Thử mở conv trong Nhanh dashboard 1 lần để trigger sync, rồi reload trang."
    )


def get_case_tone(result: dict) -> tuple[str, str]:
    if result["blacklist_triggered"]:
        return ("critical", "Không đạt")
    score = float(result["total_score"])
    if score >= 90:
        return ("great", "Xuất sắc")
    if score >= 75:
        return ("good", "Tốt")
    if score >= 50:
        return ("warn", "Trung bình")
    return ("critical", "Không đạt")


def _rationale_block(item: dict) -> str:
    rationale = item.get("rationale") or ""
    if not rationale:
        return ""
    return (
        f'<details class="rationale-block">'
        f'<summary>Tại sao quy định này quan trọng?</summary>'
        f'<p>{escape(rationale)}</p></details>'
    )


def render_findings(result: dict) -> str:
    failed = [item for item in result["findings"] if not item["passed"]]
    blacklist = result["blacklist_findings"]
    lines = []
    for item in failed[:8]:
        lines.append(
            f'<div class="warning-line"><span class="warning-dot amber"></span>'
            f'<strong>{escape(item["rule_name"])}</strong>: {escape(item["explanation"])}'
            f'{_rationale_block(item)}</div>'
        )
    for item in blacklist:
        lines.append(
            f'<div class="warning-line"><span class="warning-dot red"></span>'
            f'<strong>{escape(item["rule_name"])}</strong>: {escape(item["explanation"])}'
            f'{_rationale_block(item)}</div>'
        )
    if not lines:
        lines.append('<div class="warning-line"><span class="warning-dot green"></span>Không có cảnh báo nào.</div>')
    return "".join(lines)


def render_transcript(conversation: Conversation) -> str:
    blocks = []
    for message in conversation.messages:
        role = "NV" if message.sender_type == "employee" else "KH"
        role_class = "employee" if message.sender_type == "employee" else "customer"
        time_label = parse_display_datetime(message.sent_at)[-5:]
        attachments = ""
        if message.attachments:
            attachment_blocks = []
            for item in message.attachments:
                attachment_type = (item.type or "").lower()
                attachment_name = escape(item.name or "attachment")
                if attachment_type == "image" and item.url:
                    image_url = escape(item.url, quote=True)
                    attachment_blocks.append(
                        f"""
                        <a class="attachment-image-link" href="{image_url}" target="_blank" rel="noreferrer">
                            <img class="attachment-image" src="{image_url}" alt="{attachment_name}">
                        </a>
                        """
                    )
                else:
                    attachment_blocks.append(
                        f'<div class="attachment-note">[{escape(item.type.upper())}] {attachment_name}</div>'
                    )
            attachments = "".join(attachment_blocks)
        blocks.append(
            f"""
            <div class="message-row {role_class}">
                <div class="message-meta">
                    <span class="role-pill {role_class}">{role}</span>
                    <span class="time-pill">{escape(time_label)}</span>
                </div>
                <div class="message-text">{escape(message.text)}</div>
                {attachments}
            </div>
            """
        )
    return "".join(blocks)


def render_case(case: dict, anchor_id: str | None = None, extra_class: str = "") -> str:
    conversation: Conversation = case["conversation"]
    result: dict = case["result"]
    tone_class, tone_label = get_case_tone(result)
    category_cards = []
    for category in result["category_scores"]:
        category_cards.append(
            f"""
            <div class="metric-card">
                <div class="metric-label">{escape(category["name"])}</div>
                <div class="metric-value">{escape(format_score(category["score"]))}/{escape(format_score(category["max_score"]))}</div>
            </div>
            """
        )

    started_at = conversation.messages[0].sent_at if conversation.messages else None
    ended_at = conversation.messages[-1].sent_at if conversation.messages else None
    employee_name = conversation.employee.name if conversation.employee else "Unknown Employee"
    article_id = f' id="{escape(anchor_id, quote=True)}"' if anchor_id else ""
    return f"""
    <article class="case-thread {escape(extra_class)}"{article_id}>
        <section class="review-card">
            <div class="review-head {tone_class}">
                <div class="headline">
                    <span class="status-orb {tone_class}"></span>
                    <strong>{escape(conversation.external_id)}</strong>
                    <span class="head-sep">·</span>
                    <span>{escape(employee_name)}</span>
                    <span class="head-sep">·</span>
                    <span>{escape(conversation.channel.title())}</span>
                </div>
                <div class="mini-badge {tone_class}">{escape(tone_label)}</div>
            </div>

            <div class="review-body">
                <div class="time-block">
                    <div class="label-row">⏱ Thời gian</div>
                    <div class="time-range">{escape(parse_display_datetime(started_at))} → {escape(parse_display_datetime(ended_at))}</div>
                </div>

                <div class="score-grid">
                    <div class="score-card">
                        <div class="score-label">Grade</div>
                        <div class="score-main">{escape(result["grade"])}</div>
                        <div class="score-sub">{'PASS' if result['passed'] else 'REVIEW'}</div>
                    </div>
                    <div class="score-card">
                        <div class="score-label">Tổng điểm</div>
                        <div class="score-main">{escape(format_score(result["total_score"]))}/{escape(format_score(result["max_score"]))}</div>
                        <div class="score-sub">Blacklist: {'Có' if result['blacklist_triggered'] else 'Không'}</div>
                    </div>
                    {"".join(category_cards)}
                </div>

                <div class="summary-box">
                    <strong>Tổng quan:</strong> {escape(f"{len([item for item in result['findings'] if not item['passed']])} rule fail, {len(result['blacklist_findings'])} blacklist trigger.")}
                </div>

                <div class="warning-box">
                    {render_findings(result)}
                </div>
            </div>
        </section>

        <section class="transcript-card">
            <div class="transcript-head">
                <div class="transcript-title">Nội dung hội thoại — #{escape(conversation.external_id)}</div>
                <div class="transcript-meta">{escape(employee_name)} · {escape(conversation.channel.title())} · {escape(str(len(conversation.messages)))} tin nhắn</div>
            </div>
            <div class="transcript-body">
                {render_transcript(conversation)}
            </div>
        </section>
    </article>
    """


def render_scorecards(scorecards: list[dict]) -> str:
    if not scorecards:
        return '<div class="panel-body">Chưa có employee scorecard.</div>'
    blocks = []
    for item in scorecards:
        top_skill = item["top_failed_skills"][0]["skill"] if item["top_failed_skills"] else "-"
        blocks.append(
            f"""
            <div class="scorecard-item">
                <div class="scorecard-head">
                    <strong>{escape(item["employee_name"])}</strong>
                    <span class="tiny-pill">{escape(item["grade"])}</span>
                </div>
                <div class="scorecard-stats">
                    <div>Average: <strong>{escape(format_score(item["average_score"]))}</strong></div>
                    <div>Passed rate: <strong>{escape(format_score(item["passed_rate"] * 100))}%</strong></div>
                    <div>Blacklist: <strong>{escape(str(item["blacklist_count"]))}</strong></div>
                    <div>Top weak skill: <strong>{escape(top_skill)}</strong></div>
                </div>
            </div>
            """
        )
    return "".join(blocks)


def render_top_training_employees(scorecards: list[dict]) -> str:
    rows = []
    for item in scorecards[:5]:
        rows.append(
            f"<tr><td>{escape(item['employee_name'])}</td><td>{escape(str(len(item['training_recommendations'])))}</td><td>{escape(format_score(item['average_score']))}</td><td>{escape(item['grade'])}</td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="4">Chưa có dữ liệu.</td></tr>'


def render_top_weak_skills(skills: list[dict]) -> str:
    rows = []
    for item in skills[:8]:
        rows.append(
            f"<tr><td>{escape(item['skill'])}</td><td>{escape(str(item['failed_count']))}</td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="2">Chưa có dữ liệu.</td></tr>'


def render_training_recommendations(recommendations: list[dict]) -> str:
    if not recommendations:
        return '<div class="panel-body">Không có training recommendation.</div>'
    items = []
    for recommendation in recommendations[:12]:
        sample_phrase = recommendation["sample_phrases"][0] if recommendation["sample_phrases"] else "-"
        practice_task = recommendation["practice_tasks"][0] if recommendation["practice_tasks"] else "-"
        items.append(
            f"""
            <div class="training-item">
                <div class="training-head">
                    <strong>{escape(recommendation["employee_name"])}</strong>
                    <span class="tiny-pill {escape(recommendation['priority'])}">{escape(recommendation["priority"].upper())}</span>
                </div>
                <div class="training-body">
                    <div><strong>Skill:</strong> {escape(recommendation["skill"])}</div>
                    <div><strong>Module:</strong> {escape(recommendation["recommended_module_title"])}</div>
                    <div><strong>Reason:</strong> {escape(recommendation["reason"])}</div>
                    <div><strong>Sample phrase:</strong> {escape(sample_phrase)}</div>
                    <div><strong>Practice:</strong> {escape(practice_task)}</div>
                    <div><strong>Conversations:</strong> {escape(", ".join(recommendation["related_conversation_ids"]))}</div>
                </div>
            </div>
            """
        )
    return "".join(items)


def render_nhanh_recent_items(items: list[dict], selected_conversation_id: str | None) -> str:
    if not items:
        return '<div class="panel-body">Chua co hoi thoai Nhanh nao tra ve.</div>'
    rows = []
    from app.nhanh_adapter import channel_label
    for item in items:
        conversation_id = str(item.get("id") or "-")
        customer_name = str(item.get("pageUserName") or "-")
        updated_at = parse_display_datetime(item.get("updatedAt"))
        has_reply = bool(item.get("hasReply"))
        has_phone = bool(item.get("hasPhone"))
        last_message = str(item.get("lastMessage") or "-").strip()
        status_text = "Da phan hoi" if has_reply else "Chua phan hoi"
        phone_text = "Co SDT" if has_phone else "Chua co SDT"
        ch_label = channel_label(item.get("channel"), str(item.get("pageId") or ""))
        ch_class = ch_label.lower()
        selected_class = " selected" if selected_conversation_id == conversation_id else ""
        qa_summary = item.get("qa_summary") or {}
        fetch_error = item.get("fetch_error")
        qa_html = ""
        if qa_summary:
            blacklist_text = (
                f"Blacklist {qa_summary['blacklist_count']}"
                if qa_summary["blacklist_triggered"]
                else "Blacklist 0"
            )
            qa_html = f"""
                <div class="nhanh-qa-row">
                    <span class="nhanh-qa-pill score">{escape(format_score(qa_summary["total_score"]))}/{escape(format_score(qa_summary["max_score"]))}</span>
                    <span class="nhanh-qa-pill">{escape(qa_summary["grade"])}</span>
                    <span class="nhanh-qa-pill">{escape(str(qa_summary["failed_rule_count"]))} rule fail</span>
                    <span class="nhanh-qa-pill {'blacklist' if qa_summary['blacklist_triggered'] else ''}">{escape(blacklist_text)}</span>
                </div>
            """
        elif fetch_error:
            qa_html = f'<div class="nhanh-qa-row"><span class="nhanh-qa-pill unavailable">Không lấy được tin nhắn</span></div>'
        cta_text = "View tin nhan" if not fetch_error else "Không hỗ trợ"
        link_class = "nhanh-link" + selected_class + (" disabled" if fetch_error else "")
        error_html = f'<div class="nhanh-fetch-error">{escape(fetch_error)}</div>' if fetch_error else ""
        rows.append(
            f"""
            <a class="{link_class}" href="/dashboard?live_conversation_id={escape(conversation_id)}#live-review">
                <div class="nhanh-link-head">
                    <strong>{escape(customer_name)}</strong>
                    <span class="nhanh-link-cta">{escape(cta_text)}</span>
                </div>
                <div class="nhanh-link-id">{escape(conversation_id)}</div>
                <div class="nhanh-link-meta">
                    <span class="channel-chip ch-{escape(ch_class)}">{escape(ch_label)}</span>
                    <span>{escape(updated_at)}</span>
                    <span>{escape(status_text)}</span>
                    <span>{escape(phone_text)}</span>
                </div>
                {qa_html}
                <div class="nhanh-link-preview">{escape(last_message[:140] + ('...' if len(last_message) > 140 else ''))}</div>
                {error_html}
            </a>
            """
        )
    return "".join(rows)


def render_live_case(case: dict | None) -> str:
    if not case:
        return """
        <section id="live-review" class="panel live-review-panel" style="margin-bottom: 22px;">
            <div class="panel-head">
                <h2>Live Conversation</h2>
            </div>
            <div class="panel-body live-review-body">
                <div class="live-empty-state">
                    Chua mo hoi thoai nao. Bam <strong>View tin nhan</strong> trong danh sach Nhanh de xem transcript va ket qua cham.
                </div>
            </div>
        </section>
        """
    conversation: Conversation = case["conversation"]
    return f"""
    <section id="live-review" class="panel live-review-panel" style="margin-bottom: 22px;">
        <div class="panel-head">
            <h2>Live Conversation</h2>
        </div>
        <div class="panel-body live-review-body">
            <div class="live-review-note">
                Dang xem truc tiep hoi thoai <code>{escape(conversation.external_id)}</code>.
            </div>
            {render_case(case, anchor_id="live-case", extra_class="live-case-thread")}
        </div>
    </section>
    """


def render_channel_filter(items: list[dict], active: str | None, date_filter: str = "7days") -> str:
    from app.nhanh_adapter import channel_label
    from collections import Counter
    counts: Counter = Counter()
    for item in items:
        counts[channel_label(item.get("channel"), str(item.get("pageId") or ""))] += 1
    total = sum(counts.values())
    chips = [("Tất cả", "", total)]
    for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        chips.append((label, label, n))
    html_parts = []
    base_qs = f"date={escape(date_filter)}" if date_filter else ""
    for label, value, n in chips:
        is_active = (active or "") == value
        cls_extra = " active" if is_active else ""
        qs = base_qs
        if value:
            qs = (qs + "&" if qs else "") + f"channel={escape(value)}"
        href = f"/dashboard?{qs}" if qs else "/dashboard"
        html_parts.append(
            f'<a class="channel-filter-chip{cls_extra}" href="{href}">{escape(label)} <span class="cf-count">{n}</span></a>'
        )
    return '<div class="channel-filter-bar"><span style="align-self:center;font-size:12px;color:#4a5568;font-weight:600;margin-right:4px">Channel:</span>' + "".join(html_parts) + '</div>'


def render_date_filter(active: str, channel_filter: str | None) -> str:
    chips = []
    for key, (label, _, _) in DATE_FILTERS.items():
        is_active = key == active
        cls = " active" if is_active else ""
        qs = f"?date={key}"
        if channel_filter:
            qs += f"&channel={escape(channel_filter)}"
        chips.append(f'<a class="channel-filter-chip{cls}" href="/dashboard{qs}">{escape(label)}</a>')
    return '<div class="channel-filter-bar" style="margin-top:4px"><span style="align-self:center;font-size:12px;color:#4a5568;font-weight:600;margin-right:4px">Khoảng thời gian:</span>' + "".join(chips) + '</div>'


def _friendly_nhanh_error(raw: str) -> str:
    """Translate noisy network stack traces into something a non-engineer can
    read. Keeps the original kind of failure but drops the Python plumbing."""
    if not raw:
        return ""
    text = str(raw)
    lower = text.lower()
    if "timed out" in lower or "timeout" in lower:
        return "Nhanh API tạm chậm — đang dùng dữ liệu trong cache."
    if "connectionpool" in lower or "max retries" in lower or "connection refused" in lower:
        return "Không kết nối được Nhanh API — kiểm tra mạng. Dữ liệu cache vẫn dùng được."
    if "http 401" in lower or "http 403" in lower:
        return "Access token Nhanh đã hết hạn hoặc sai. Vào data/nhanh_config.local.json cập nhật lại."
    if "http 429" in lower:
        return "Nhanh đang giới hạn truy cập (rate limit). Thử lại sau vài phút."
    # Strip extremely long stacks, keep first ~180 chars.
    return text if len(text) <= 180 else (text[:180] + "…")


def _format_relative_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 0:
        return "vừa xong"
    if seconds < 60:
        return f"{seconds}s trước"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s trước"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours < 24:
        return f"{hours}h {minutes:02d}m trước"
    days = hours // 24
    return f"{days}d {hours % 24:02d}h trước"


def render_sync_banner(sync_status: dict | None) -> str:
    if not sync_status:
        return ""
    import time as _time
    now = _time.time()
    is_syncing = bool(sync_status.get("is_syncing"))
    last_sync_ts = sync_status.get("last_sync_ts")
    last_error = sync_status.get("last_error")
    last_error_ts = sync_status.get("last_error_ts")
    conv_count = sync_status.get("conversation_count") or 0
    progress = sync_status.get("progress") or {}
    processed = int(progress.get("processed") or 0)
    total = int(progress.get("total") or 0)

    if is_syncing and sync_status.get("is_first_run"):
        progress_text = f"{processed}/{total}" if total else "đang khởi tạo..."
        body = (
            f"🔄 Đang đồng bộ lần đầu (backfill 7 ngày)... "
            f"Có thể mất 5-10 phút. Tiến độ: <strong>{escape(progress_text)}</strong> hội thoại."
        )
        tone = "info"
    elif is_syncing:
        progress_text = f"({processed}/{total})" if total else ""
        last_text = _format_relative_seconds(now - last_sync_ts) if last_sync_ts else "chưa có lần nào"
        body = f"⟳ Đang đồng bộ... {escape(progress_text)} · Đồng bộ lần cuối: <strong>{escape(last_text)}</strong>"
        tone = "info"
    elif last_error:
        when = _format_relative_seconds(now - last_error_ts) if last_error_ts else "?"
        body = (
            f"⚠ {escape(_friendly_nhanh_error(str(last_error)))} · "
            f"Lần thử cuối: <strong>{escape(when)}</strong>"
        )
        tone = "error"
    else:
        when = _format_relative_seconds(now - last_sync_ts) if last_sync_ts else "chưa có lần nào"
        body = (
            f"Đồng bộ lần cuối: <strong>{escape(when)}</strong> · "
            f"{conv_count} hội thoại trong cache"
        )
        tone = "ok"

    bg = {"info": "#eef6ff", "error": "#fff2f0", "ok": "#f1faf1"}.get(tone, "#f4f4f4")
    border = {"info": "#7ab1ff", "error": "#ff9a90", "ok": "#9bd6a3"}.get(tone, "#ddd")
    return f"""
    <div id="sync-banner" class="sync-banner" data-syncing="{1 if is_syncing else 0}"
         style="background:{bg}; border:1px solid {border}; border-radius:8px;
                padding:10px 14px; margin:0 0 12px; display:flex;
                justify-content:space-between; align-items:center; gap:12px;">
        <div class="sync-banner-text">{body}</div>
        <div class="sync-banner-actions">
            <button type="button" id="sync-now-btn" onclick="triggerSyncNow()"
                    style="padding:6px 12px; cursor:pointer;">Đồng bộ ngay</button>
        </div>
    </div>
    <script>
    (function() {{
        const banner = document.getElementById('sync-banner');
        if (!banner) return;
        const isSyncing = banner.dataset.syncing === '1';

        window.triggerSyncNow = async function() {{
            const btn = document.getElementById('sync-now-btn');
            if (btn) {{ btn.disabled = true; btn.textContent = 'Đang trigger...'; }}
            try {{
                const r = await fetch('/api/refresh', {{ method: 'POST' }});
                const data = await r.json();
                if (!data.ok && data.reason === 'sync_in_progress') {{
                    if (btn) btn.textContent = 'Đang sync...';
                }}
                pollStatus();
            }} catch (e) {{
                if (btn) {{ btn.disabled = false; btn.textContent = 'Đồng bộ ngay'; }}
            }}
        }};

        async function pollStatus() {{
            try {{
                const r = await fetch('/api/sync-status');
                const status = await r.json();
                updateBanner(status);
                if (status.is_syncing) {{
                    setTimeout(pollStatus, 5000);
                }} else {{
                    // Sync finished — reload to show fresh data.
                    setTimeout(() => window.location.reload(), 800);
                }}
            }} catch (e) {{}}
        }}

        function updateBanner(s) {{
            const textEl = banner.querySelector('.sync-banner-text');
            if (!textEl) return;
            if (s.is_syncing) {{
                const p = s.progress || {{}};
                const pt = (p.total ? `(${{p.processed}}/${{p.total}})` : '');
                textEl.innerHTML = `⟳ Đang đồng bộ... ${{pt}}`;
                banner.dataset.syncing = '1';
            }}
        }}

        if (isSyncing) {{ setTimeout(pollStatus, 5000); }}
    }})();
    </script>
    """


def render_nhanh_panel(nhanh_data: dict, live_conversation_id: str | None, channel_filter: str | None = None, date_filter: str = "7days") -> str:
    if not nhanh_data["enabled"]:
        return ""
    from app.nhanh_adapter import channel_label
    recent_items = [dict(item) for item in nhanh_data["recent"]]
    date_filter_html = render_date_filter(date_filter, channel_filter)
    channel_filter_html = render_channel_filter(recent_items, channel_filter, date_filter)
    if channel_filter:
        recent_items = [
            item for item in recent_items
            if channel_label(item.get("channel"), str(item.get("pageId") or "")) == channel_filter
        ]
    filter_html = date_filter_html + channel_filter_html
    date_label = DATE_FILTERS.get(date_filter, ("?", None, None))[0]
    summary_html = (
        f'<div class="nhanh-note">Đang xem: <strong>{escape(date_label)}</strong> • '
        f'{len(recent_items)} hội thoại hiển thị / {nhanh_data.get("total_fetched", 0)} trong cache.</div>'
    )
    if not recent_items:
        summary_html += '<div class="nhanh-fetch-error" style="margin:10px 0">Không có hội thoại nào trong khoảng này. Đổi sang khoảng dài hơn hoặc bấm "Đồng bộ ngay".</div>'
    # Background sync errors are already shown in the banner above — don't echo
    # them again here. Only show the live-refresh error, since that's specific
    # to the conv the user just clicked on.
    error_html = ""
    if nhanh_data["live_error"]:
        error_html += f'<div class="nhanh-error">{escape(_friendly_nhanh_error(nhanh_data["live_error"]))}</div>'
    banner_html = render_sync_banner(nhanh_data.get("sync_status"))
    return f"""
    <section class="panel nhanh-panel" style="margin-bottom: 22px;">
        <div class="panel-head"><h2>Nhanh Live Review</h2></div>
        <div class="panel-body">
            {banner_html}
            <form class="nhanh-form" method="get" action="/dashboard">
                <input
                    type="text"
                    name="live_conversation_id"
                    value="{escape(live_conversation_id or '')}"
                    placeholder="Nhap conversationId tu Nhanh"
                >
                <button type="submit">Load Live Conversation</button>
            </form>
            <div class="nhanh-note">Dashboard đọc từ cache SQLite (<code>data/nhanh_cache.db</code>) cho tốc độ. Background worker sync mỗi 4h, hoặc bấm "Đồng bộ ngay".</div>
            {filter_html}
            {summary_html}
            {error_html}
            <div class="nhanh-list">
                {render_nhanh_recent_items(recent_items, live_conversation_id)}
            </div>
        </div>
    </section>
    """


def render_nav(active_view: str) -> str:
    live_class = "active" if active_view == "live" else ""
    team_class = "active" if active_view == "team" else ""
    review_class = "active" if active_view == "review" else ""
    return f"""
    <nav class="top-nav">
        <a class="top-nav-link {live_class}" href="/dashboard">Conversation Review</a>
        <a class="top-nav-link {team_class}" href="/dashboard/team">Team Coaching</a>
        <a class="top-nav-link {review_class}" href="/review">Lead Review</a>
    </nav>
    """


def render_team_sections(dashboard: dict) -> str:
    scorecards_html = render_scorecards(dashboard["scorecards"])
    top_training_html = render_top_training_employees(dashboard["top_training_employees"])
    weak_skills_html = render_top_weak_skills(dashboard["top_weak_skills"])
    training_html = render_training_recommendations(dashboard["training_recommendations"])
    return f"""
    <section class="overview-grid">
        <div class="panel">
            <div class="panel-head"><h2>Employee Scorecards</h2></div>
            <div class="panel-body scorecard-list">{scorecards_html}</div>
        </div>
        <div class="panel">
            <div class="panel-head"><h2>Top Nhân Viên Cần Training</h2></div>
            <div class="panel-body">
                <table>
                    <thead><tr><th>Nhân viên</th><th>Reco</th><th>Avg</th><th>Grade</th></tr></thead>
                    <tbody>{top_training_html}</tbody>
                </table>
            </div>
        </div>
        <div class="panel">
            <div class="panel-head"><h2>Top Kỹ Năng Yếu</h2></div>
            <div class="panel-body">
                <table>
                    <thead><tr><th>Skill</th><th>Failed</th></tr></thead>
                    <tbody>{weak_skills_html}</tbody>
                </table>
            </div>
        </div>
    </section>

    <section class="panel" style="margin-bottom: 22px;">
        <div class="panel-head"><h2>Training Recommendations</h2></div>
        <div class="panel-body training-list">{training_html}</div>
    </section>
    """


def render_state_tabs(state_filter: str, counts: dict, date_filter: str) -> str:
    """Three-tab navigation: 🟡 Đang diễn ra / ✅ Đã chấm / 🔴 Alert SLA."""
    def link(state: str, emoji: str, label: str, count: int) -> str:
        active = "active" if state_filter == state else ""
        href = f"/dashboard?state={state}&date={escape(date_filter, quote=True)}"
        return (
            f'<a class="state-tab {active}" href="{href}" '
            f'style="display:inline-flex;align-items:center;gap:6px;padding:8px 14px;'
            f'border-radius:8px;text-decoration:none;color:inherit;'
            f'background:{"#0a7f83" if active else "#f4f4f4"};'
            f'color:{"#fff" if active else "#333"};margin-right:8px;">'
            f'{emoji} {escape(label)} <span style="background:rgba(0,0,0,.12);'
            f'border-radius:10px;padding:0 8px;font-size:12px;">{count}</span></a>'
        )
    return f"""
    <div class="state-tabs" style="margin:12px 0;">
        {link("active", "🟡", "Đang diễn ra", counts.get("active", 0))}
        {link("graded", "✅", "Đã chấm", counts.get("graded", 0))}
        {link("alert", "🔴", "Alert SLA", counts.get("alert", 0))}
        {link("all", "📋", "Tất cả", counts.get("all", 0))}
    </div>
    """


def render_alert_panel(nhanh_data: dict) -> str:
    violations = nhanh_data.get("sla_violations") or []
    if not violations:
        return (
            '<div class="panel" style="margin-bottom:22px;">'
            '<div class="panel-head"><h2>🔴 Alert SLA</h2></div>'
            '<div class="panel-body"><p style="color:#666;">Hiện không có vi phạm SLA nào. '
            'Hệ thống sẽ flag khi sale chưa reply tin của khách trong 1h (giờ làm 8h-23h).</p></div>'
            '</div>'
        )
    import time as _time
    from app.nhanh_adapter import channel_label
    rows = []
    now = _time.time()
    for violation in violations:
        ch = channel_label(violation.get("channel"), str(violation.get("page_id") or ""))
        minutes = int(violation.get("business_minutes_at_detection") or 0)
        detected_ago = _format_relative_seconds(now - (violation.get("detected_at") or now))
        last_msg = (violation.get("last_message") or "").strip()
        last_msg_html = (
            f'<div style="color:#555;font-size:13px;margin-top:6px;">'
            f'Tin cuối: "{escape(last_msg[:160])}"</div>'
        ) if last_msg else ""
        rows.append(f"""
        <div class="alert-card" style="border:1px solid #ff9a90;background:#fff2f0;
             padding:12px 14px;border-radius:8px;margin-bottom:10px;">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
                <div>
                    <strong>{escape(str(violation.get('customer_name') or 'Khách'))}</strong>
                    <span style="color:#666;"> · {escape(ch)}</span>
                </div>
                <div style="color:#c00;font-weight:600;">
                    Chờ <strong>{minutes}</strong> phút (giờ làm)
                </div>
            </div>
            {last_msg_html}
            <div style="margin-top:8px;display:flex;justify-content:space-between;align-items:center;font-size:13px;">
                <span style="color:#888;">Phát hiện {escape(detected_ago)}</span>
                <a href="/dashboard?live_conversation_id={escape(str(violation['conversation_id']), quote=True)}"
                   style="background:#c73641;color:#fff;padding:6px 12px;border-radius:6px;text-decoration:none;">
                    Xem & Phản hồi
                </a>
            </div>
        </div>
        """)
    return f"""
    <section class="panel" style="margin-bottom:22px;">
        <div class="panel-head"><h2>🔴 Alert SLA ({len(violations)})</h2></div>
        <div class="panel-body">{''.join(rows)}</div>
    </section>
    """


def render_dashboard(
    page: str = "live",
    live_conversation_id: str | None = None,
    channel_filter: str | None = None,
    date_filter: str = "7days",
    state_filter: str = "all",
) -> str:
    ruleset = load_ruleset(RULESET_PATH)
    dashboard = load_dashboard_data(ruleset)
    effective_state = state_filter if state_filter in {"all", "active", "graded", "alert"} else "all"
    # For the SQL state filter we treat 'alert' as 'all' (the alert tab gets a
    # dedicated section above the list; we still show the list below it).
    sql_state = "all" if effective_state == "alert" else effective_state
    nhanh_data = load_nhanh_data(
        ruleset,
        live_conversation_id=live_conversation_id,
        date_filter=date_filter,
        state_filter=sql_state,
    )
    nhanh_html = render_nhanh_panel(nhanh_data, live_conversation_id, channel_filter=channel_filter, date_filter=date_filter)
    live_case_html = render_live_case(nhanh_data["live_case"])
    nav_html = render_nav(page)
    if page == "team":
        page_title = "Team Coaching"
        page_description = "Trang này chỉ tập trung vào scorecard nhân viên, kỹ năng yếu và training recommendation."
        hero_pills = """
                <div class="hero-pill">View: <code>/dashboard/team</code></div>
                <div class="hero-pill">Mục tiêu: coaching và quản trị chất lượng theo nhân viên</div>
        """
        body_html = render_team_sections(dashboard)
    else:
        page_title = "Conversation Review"
        page_description = "Trang này chỉ tập trung vào review hội thoại, transcript và kết quả chấm từng case."
        hero_pills = """
                <div class="hero-pill">View: <code>/dashboard</code></div>
                <div class="hero-pill">JSON API: <code>/api/results</code></div>
                <div class="hero-pill">Nguồn dữ liệu: SQLite cache (light 10', heavy 4h)</div>
        """
        tabs_html = render_state_tabs(
            effective_state, nhanh_data.get("state_counts") or {}, date_filter,
        )
        alert_html = render_alert_panel(nhanh_data) if effective_state == "alert" else ""
        body_html = f"""
        {tabs_html}
        {alert_html}
        {nhanh_html}
        {live_case_html}
        """
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Message QA Local Viewer</title>
    <style>
        :root {{
            --bg: #eceef2;
            --panel: #ffffff;
            --ink: #222934;
            --muted: #68707d;
            --accent: #0a7f83;
            --good: #0b7a4b;
            --warn: #b97800;
            --bad: #c73641;
            --line: #d8dde6;
            --shadow: 0 14px 36px rgba(34, 41, 52, 0.08);
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background:
                radial-gradient(circle at top left, rgba(10, 127, 131, 0.16), transparent 20%),
                linear-gradient(180deg, #f7f8fa 0%, var(--bg) 100%);
            color: var(--ink);
        }}
        .wrap {{
            max-width: 1360px;
            margin: 0 auto;
            padding: 28px 16px 64px;
        }}
        .hero {{
            display: grid;
            gap: 8px;
            margin-bottom: 18px;
        }}
        .top-nav {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 18px;
        }}
        .top-nav-link {{
            text-decoration: none;
            padding: 10px 14px;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.82);
            color: var(--muted);
            font-weight: 700;
        }}
        .top-nav-link.active {{
            background: #0a7f83;
            border-color: #0a7f83;
            color: #fff;
        }}
        h1 {{
            margin: 0;
            font-size: clamp(28px, 3vw, 42px);
            line-height: 1;
        }}
        h2 {{
            margin: 0;
            font-size: 22px;
        }}
        p {{
            margin: 0;
            color: var(--muted);
            max-width: 860px;
            font-size: 15px;
        }}
        .hero-strip {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 8px;
        }}
        .hero-pill {{
            background: rgba(255, 255, 255, 0.82);
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 10px 14px;
            font-size: 13px;
            color: var(--muted);
            backdrop-filter: blur(8px);
        }}
        .overview-grid {{
            display: grid;
            grid-template-columns: 1.5fr 1fr 1fr;
            gap: 16px;
            margin-bottom: 22px;
        }}
        .panel {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 20px;
            box-shadow: var(--shadow);
            overflow: hidden;
        }}
        .panel-head {{
            padding: 18px 20px;
            border-bottom: 1px solid var(--line);
            background: linear-gradient(135deg, #eef9f8, #f8fcfc);
        }}
        .panel-body {{
            padding: 18px 20px;
        }}
        .nhanh-form {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }}
        .nhanh-form input {{
            flex: 1 1 340px;
            min-width: 240px;
            border: 1px solid #cfd8e3;
            border-radius: 12px;
            padding: 12px 14px;
            font: inherit;
        }}
        .nhanh-form button {{
            border: none;
            border-radius: 12px;
            padding: 12px 16px;
            background: #0a7f83;
            color: #fff;
            font: inherit;
            font-weight: 700;
            cursor: pointer;
        }}
        .nhanh-note {{
            color: #556171;
            font-size: 14px;
            margin-bottom: 12px;
        }}
        .nhanh-error {{
            margin-bottom: 12px;
            padding: 12px 14px;
            border-radius: 12px;
            background: #fff0f1;
            color: #b4232d;
            border: 1px solid #f2c8cd;
        }}
        .nhanh-list {{
            display: grid;
            gap: 8px;
        }}
        .nhanh-link {{
            display: grid;
            gap: 8px;
            text-decoration: none;
            color: inherit;
            background: #f8fafc;
            border: 1px solid #e6ebf2;
            border-radius: 14px;
            padding: 12px 14px;
        }}
        .nhanh-link:hover {{
            border-color: #9fd7d2;
            background: #f4fbfb;
        }}
        .nhanh-link.selected {{
            border-color: #0a7f83;
            background: #eef9f8;
            box-shadow: inset 0 0 0 1px rgba(10, 127, 131, 0.12);
        }}
        .nhanh-link-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }}
        .nhanh-link-id {{
            color: #425066;
            font-size: 13px;
            word-break: break-all;
        }}
        .nhanh-link-meta {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            color: #68707d;
            font-size: 13px;
        }}
        .nhanh-qa-row {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .nhanh-qa-pill {{
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: 5px 10px;
            background: #eef2f7;
            color: #425066;
            font-size: 12px;
            font-weight: 700;
        }}
        .nhanh-qa-pill.score {{
            background: #e7f7ee;
            color: #0d7a43;
        }}
        .nhanh-qa-pill.blacklist {{
            background: #fde8ea;
            color: #b4232d;
        }}
        .nhanh-link-preview {{
            color: #283548;
            font-size: 14px;
            line-height: 1.5;
        }}
        .nhanh-link-cta {{
            color: #0a7f83;
            font-weight: 700;
            white-space: nowrap;
        }}
        .live-review-panel {{
            scroll-margin-top: 16px;
        }}
        .live-review-body {{
            background: linear-gradient(180deg, #fbfdfd 0%, #f6fafb 100%);
        }}
        .live-review-note {{
            margin-bottom: 14px;
            color: #556171;
            font-size: 14px;
        }}
        .live-empty-state {{
            padding: 24px 20px;
            border: 1px dashed #bfd6de;
            border-radius: 16px;
            background: #fcfefe;
            color: #556171;
            line-height: 1.6;
        }}
        .live-case-thread {{
            border: 1px solid #c7e8e4;
            box-shadow: 0 18px 40px rgba(10, 127, 131, 0.12);
        }}
        .scorecard-list, .training-list {{
            display: grid;
            gap: 12px;
        }}
        .scorecard-item, .training-item {{
            background: #f8fafc;
            border: 1px solid #e6ebf2;
            border-radius: 16px;
            padding: 14px;
        }}
        .scorecard-head, .training-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 10px;
        }}
        .scorecard-stats, .training-body {{
            display: grid;
            gap: 6px;
            color: #354050;
            font-size: 14px;
            line-height: 1.5;
        }}
        .tiny-pill {{
            border-radius: 999px;
            padding: 5px 9px;
            font-size: 11px;
            font-weight: 700;
            background: #eef2f7;
            color: #4f5968;
        }}
        .tiny-pill.high {{
            background: #fde8ea;
            color: #b4232d;
        }}
        .tiny-pill.medium {{
            background: #fff3d1;
            color: #9c6a00;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            text-align: left;
            padding: 10px 0;
            border-bottom: 1px solid #edf1f6;
            font-size: 14px;
        }}
        th {{
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }}
        .case-list {{
            display: grid;
            gap: 18px;
        }}
        .case-thread {{
            background: rgba(255, 255, 255, 0.55);
            border: 1px solid rgba(216, 221, 230, 0.9);
            border-radius: 22px;
            box-shadow: var(--shadow);
            overflow: hidden;
        }}
        .review-card, .transcript-card {{
            background: var(--panel);
        }}
        .review-card {{
            border-bottom: 1px solid var(--line);
        }}
        .review-head, .transcript-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 18px 22px;
        }}
        .review-head.great {{ background: linear-gradient(135deg, #ffe9e9, #fff6f6); }}
        .review-head.good {{ background: linear-gradient(135deg, #ecf6eb, #f8fff7); }}
        .review-head.warn {{ background: linear-gradient(135deg, #fff0d4, #fff8ea); }}
        .review-head.critical {{ background: linear-gradient(135deg, #ffe4e7, #fff4f5); color: var(--bad); }}
        .transcript-head {{
            background: linear-gradient(135deg, #d9f4f2, #eefcfa);
            border-top: 1px solid rgba(10, 127, 131, 0.08);
            border-bottom: 1px solid var(--line);
        }}
        .headline {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: clamp(18px, 2vw, 32px);
            font-weight: 700;
            flex-wrap: wrap;
        }}
        .head-sep {{ color: #9aa3ad; }}
        .status-orb {{
            width: 18px;
            height: 18px;
            border-radius: 50%;
            display: inline-block;
            box-shadow: inset 0 0 0 2px #202732;
        }}
        .status-orb.great {{ background: #f14d59; }}
        .status-orb.good {{ background: #25a164; }}
        .status-orb.warn {{ background: #ffd027; }}
        .status-orb.critical {{ background: #ef3340; }}
        .mini-badge {{
            border-radius: 999px;
            padding: 8px 12px;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.04em;
            white-space: nowrap;
        }}
        .mini-badge.great, .mini-badge.critical {{ background: rgba(199, 54, 65, 0.1); color: var(--bad); }}
        .mini-badge.good {{ background: rgba(11, 122, 75, 0.1); color: var(--good); }}
        .mini-badge.warn {{ background: rgba(185, 120, 0, 0.12); color: var(--warn); }}
        .review-body, .transcript-body {{ padding: 18px 22px 22px; }}
        .time-block {{ padding-bottom: 16px; border-bottom: 1px solid var(--line); }}
        .label-row {{ font-size: 14px; font-weight: 700; margin-bottom: 6px; }}
        .time-range {{ font-size: 18px; color: #354050; }}
        .score-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            padding: 18px 0;
            border-bottom: 1px solid var(--line);
        }}
        .score-card, .metric-card {{
            background: #f8fafc;
            border: 1px solid #e6ebf2;
            border-radius: 16px;
            padding: 14px 14px 12px;
            min-height: 94px;
        }}
        .score-label, .metric-label {{ font-size: 13px; color: var(--muted); margin-bottom: 8px; }}
        .score-main {{ font-size: 28px; font-weight: 800; line-height: 1.05; }}
        .score-sub {{ margin-top: 6px; color: var(--muted); font-size: 13px; }}
        .metric-value {{ font-size: 24px; font-weight: 800; }}
        .summary-box {{
            margin-top: 18px;
            padding: 14px 16px;
            background: #f7f8fb;
            border-left: 4px solid var(--accent);
            border-radius: 12px;
            line-height: 1.6;
        }}
        .warning-box {{
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--line);
            display: grid;
            gap: 10px;
        }}
        .warning-line {{
            display: flex;
            align-items: flex-start;
            gap: 10px;
            font-size: 15px;
            line-height: 1.5;
        }}
        .warning-dot {{
            width: 14px;
            height: 14px;
            border-radius: 50%;
            flex: 0 0 14px;
            margin-top: 4px;
            box-shadow: inset 0 0 0 1px #2b3341;
        }}
        .warning-dot.green {{ background: #2fb66c; }}
        .warning-dot.amber {{ background: #ffd027; }}
        .warning-dot.red {{ background: #ef3340; }}
        .channel-chip {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
            color: #fff;
            background: #888;
            letter-spacing: 0.3px;
        }}
        .channel-chip.ch-facebook {{ background: #1877f2; }}
        .channel-chip.ch-instagram {{ background: linear-gradient(135deg,#f58529,#dd2a7b,#8134af); }}
        .channel-chip.ch-shopee {{ background: #ee4d2d; }}
        .channel-chip.ch-tiktok {{ background: #010101; }}
        .channel-chip.ch-zalo {{ background: #0068ff; }}
        .channel-chip.ch-lazada {{ background: #1a47b7; }}
        .channel-chip.ch-tiki {{ background: #189eff; }}
        .channel-chip.ch-sendo {{ background: #d0021b; }}
        .channel-chip.ch-website {{ background: #4a5568; }}
        .channel-filter-bar {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 10px 0 14px 0;
        }}
        .channel-filter-chip {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 16px;
            background: #edf2f7;
            color: #2d3748;
            font-size: 13px;
            font-weight: 500;
            text-decoration: none;
            border: 1px solid transparent;
            transition: all 0.15s ease;
        }}
        .channel-filter-chip:hover {{
            background: #e2e8f0;
        }}
        .channel-filter-chip.active {{
            background: #2b6cb0;
            color: #fff;
            border-color: #2b6cb0;
        }}
        .channel-filter-chip .cf-count {{
            background: rgba(0,0,0,0.08);
            padding: 1px 7px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
        }}
        .channel-filter-chip.active .cf-count {{
            background: rgba(255,255,255,0.25);
        }}
        .nhanh-link.disabled {{
            opacity: 0.6;
            background: #f7fafc;
        }}
        .nhanh-link.disabled .nhanh-link-cta {{
            background: #cbd5e0;
            color: #4a5568;
        }}
        .nhanh-qa-pill.unavailable {{
            background: #fef3c7;
            color: #92400e;
        }}
        .nhanh-fetch-error {{
            margin-top: 8px;
            padding: 8px 10px;
            background: #fef3c7;
            border-left: 3px solid #f59e0b;
            border-radius: 4px;
            font-size: 12px;
            color: #92400e;
        }}
        .rationale-block {{
            margin: 6px 0 2px 18px;
            font-size: 12px;
            color: #4a5568;
        }}
        .rationale-block summary {{
            cursor: pointer;
            color: #2b6cb0;
        }}
        .rationale-block p {{
            margin: 6px 0 0 0;
            padding: 8px 10px;
            background: #f7fafc;
            border-left: 3px solid #2b6cb0;
            border-radius: 4px;
        }}
        .transcript-title {{
            font-size: clamp(20px, 2vw, 30px);
            font-weight: 800;
            color: #096d72;
        }}
        .transcript-meta {{ font-size: 14px; color: var(--muted); }}
        .message-row {{
            padding: 14px 0;
            border-bottom: 1px solid #edf1f6;
        }}
        .message-row:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .message-meta {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
        }}
        .role-pill, .time-pill {{
            border-radius: 999px;
            padding: 4px 10px;
            font-size: 12px;
            font-weight: 700;
        }}
        .role-pill.employee {{ background: #e9f6ff; color: #116187; }}
        .role-pill.customer {{ background: #fef1e2; color: #965b07; }}
        .time-pill {{ background: #f0f3f7; color: #647080; }}
        .message-text {{
            font-size: 16px;
            line-height: 1.7;
            color: #293241;
        }}
        .attachment-note {{
            margin-top: 8px;
            color: #0a7f83;
            font-size: 13px;
            font-weight: 700;
        }}
        .attachment-image-link {{
            display: inline-block;
            margin-top: 10px;
            text-decoration: none;
        }}
        .attachment-image {{
            display: block;
            max-width: min(320px, 100%);
            max-height: 320px;
            border-radius: 14px;
            border: 1px solid #d7e2ee;
            box-shadow: 0 10px 24px rgba(34, 41, 52, 0.12);
            object-fit: cover;
            background: #f4f7fb;
        }}
        @media (max-width: 1080px) {{
            .overview-grid {{ grid-template-columns: 1fr; }}
        }}
        @media (max-width: 920px) {{
            .review-head, .transcript-head {{ flex-direction: column; align-items: flex-start; }}
            .score-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .time-range {{ font-size: 16px; }}
        }}
        @media (max-width: 640px) {{
            .wrap {{ padding: 20px 12px 40px; }}
            .score-grid {{ grid-template-columns: 1fr; }}
            .headline {{ font-size: 20px; }}
            .transcript-title {{ font-size: 22px; }}
        }}
    </style>
</head>
<body>
    <div class="wrap">
        {nav_html}
        <section class="hero">
            <h1>{page_title}</h1>
            <p>{page_description}</p>
            <div class="hero-strip">
                {hero_pills}
            </div>
        </section>

        {body_html}
    </div>
    <script>
        (function () {{
            const hasLiveCase = {str(bool(nhanh_data["live_case"] and page == "live")).lower()};
            if (!hasLiveCase) return;
            if (!window.location.search.includes("live_conversation_id=")) return;
            const liveReview = document.getElementById("live-review");
            if (!liveReview) return;
            requestAnimationFrame(() => {{
                liveReview.scrollIntoView({{ behavior: "smooth", block: "start" }});
            }});
        }})();
    </script>
</body>
</html>"""


def render_nhanh_callback(query: dict[str, list[str]]) -> str:
    access_code = query.get("accessCode", [""])[0]
    business_id = query.get("businessId", [""])[0]
    all_params = json.dumps({key: values[0] if len(values) == 1 else values for key, values in query.items()}, ensure_ascii=False, indent=2)
    status_text = "Đã nhận accessCode từ Nhanh." if access_code else "Chưa thấy accessCode trong URL callback."
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Nhanh OAuth Callback</title>
    <style>
        body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: #f4f7fb;
            color: #1f2937;
        }}
        .wrap {{
            max-width: 920px;
            margin: 0 auto;
            padding: 40px 16px;
        }}
        .panel {{
            background: #fff;
            border: 1px solid #dbe3ee;
            border-radius: 18px;
            padding: 24px;
            box-shadow: 0 14px 36px rgba(17, 24, 39, 0.08);
        }}
        .status {{
            display: inline-block;
            padding: 8px 12px;
            border-radius: 999px;
            font-weight: 700;
            background: {"#e7f7ee" if access_code else "#fff4d6"};
            color: {"#0d7a43" if access_code else "#8a6500"};
            margin-bottom: 16px;
        }}
        code, pre {{
            font-family: Consolas, monospace;
            background: #f7f9fc;
            border: 1px solid #e3eaf3;
            border-radius: 12px;
        }}
        code {{
            padding: 2px 6px;
        }}
        pre {{
            padding: 14px;
            overflow: auto;
        }}
        h1, h2 {{
            margin-top: 0;
        }}
        .grid {{
            display: grid;
            gap: 12px;
            margin: 18px 0;
        }}
        .item {{
            background: #f9fbfd;
            border: 1px solid #e6edf5;
            border-radius: 14px;
            padding: 14px;
        }}
    </style>
</head>
<body>
    <div class="wrap">
        <div class="panel">
            <div class="status">{escape(status_text)}</div>
            <h1>Nhanh OAuth Callback</h1>
            <p>Endpoint này dùng để nhận trình duyệt quay về từ Nhanh sau khi user cấp quyền cho app.</p>
            <div class="grid">
                <div class="item"><strong>accessCode:</strong> <code>{escape(access_code or "-")}</code></div>
                <div class="item"><strong>businessId:</strong> <code>{escape(business_id or "-")}</code></div>
                <div class="item"><strong>Callback URL:</strong> <code>/integrations/nhanh/oauth/callback</code></div>
            </div>
            <h2>Query Params nhận được</h2>
            <pre>{escape(all_params)}</pre>
            <p>Bước tiếp theo: dùng <code>accessCode</code> này để gọi API đổi ra <code>accessToken</code> theo tài liệu Nhanh.</p>
        </div>
    </div>
</body>
</html>"""


# ============================================================
# Lead Review workflow (PLAN_SHIFT_REVIEW.md)
# ============================================================

def _build_review_page(body_html: str, *, title: str = "Lead Review") -> str:
    """Lightweight HTML wrapper reusing the existing dashboard styling."""
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
        body {{ font-family: "Segoe UI", Tahoma, sans-serif; background: #f7f8fa;
                color: #222934; margin: 0; padding: 24px; }}
        .wrap {{ max-width: 1200px; margin: 0 auto; }}
        h1, h2 {{ color: #0a7f83; }}
        .top-nav {{ margin-bottom: 18px; }}
        .top-nav-link {{ display: inline-block; padding: 8px 16px; margin-right: 8px;
                          border-radius: 999px; text-decoration: none; color: #444;
                          background: #fff; border: 1px solid #d8dde6; }}
        .top-nav-link.active {{ background: #0a7f83; color: #fff; border-color: #0a7f83; }}
        .shift-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
        .shift-card {{ background: #fff; border: 1px solid #d8dde6; border-radius: 12px;
                        padding: 18px; }}
        .shift-card h3 {{ margin: 0 0 6px; }}
        .progress-bar {{ background: #eee; border-radius: 999px; overflow: hidden;
                          height: 10px; margin: 10px 0; }}
        .progress-bar > div {{ background: #0a7f83; height: 100%; }}
        .priority-block {{ background: #fff; border: 1px solid #d8dde6; border-radius: 12px;
                            padding: 16px; margin-bottom: 16px; }}
        .priority-block h3 {{ margin-top: 0; }}
        .conv-row {{ display: flex; justify-content: space-between; gap: 12px;
                       padding: 10px 0; border-bottom: 1px solid #f0f0f0; }}
        .conv-row:last-child {{ border: 0; }}
        .conv-row a {{ color: #0a7f83; text-decoration: none; font-weight: 600; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
                   font-size: 12px; font-weight: 600; }}
        .badge-pending {{ background: #fff3cd; color: #856404; }}
        .badge-confirmed {{ background: #d4edda; color: #155724; }}
        .badge-edited {{ background: #cce5ff; color: #004085; }}
        .badge-skipped {{ background: #e2e3e5; color: #383d41; }}
        .badge-blacklist {{ background: #f8d7da; color: #721c24; }}
        .review-form {{ background: #fff; border: 1px solid #d8dde6; border-radius: 12px;
                         padding: 16px; }}
        .review-form label {{ display: block; margin: 8px 0 4px; font-weight: 600; }}
        .review-form input[type=number], .review-form textarea {{
            width: 100%; padding: 6px 10px; border: 1px solid #ccc; border-radius: 6px; }}
        .review-form button {{ background: #0a7f83; color: #fff; border: 0;
                                padding: 10px 18px; border-radius: 8px; cursor: pointer;
                                margin-right: 8px; }}
        .review-form button.secondary {{ background: #888; }}
        .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
        .transcript {{ background: #fff; border: 1px solid #d8dde6; border-radius: 12px;
                        padding: 14px; max-height: 540px; overflow-y: auto; }}
        .msg {{ margin-bottom: 10px; padding: 8px 10px; border-radius: 8px; }}
        .msg.customer {{ background: #f1f3f5; }}
        .msg.employee {{ background: #e6f5f4; }}
        .msg .meta {{ font-size: 11px; color: #888; margin-bottom: 4px; }}
        .findings {{ font-size: 13px; }}
        .findings .ok {{ color: #1a8a3a; }}
        .findings .bad {{ color: #c73641; }}
    </style>
</head>
<body>
    <div class="wrap">
        {render_nav("review")}
        {body_html}
    </div>
</body>
</html>"""


def _badge_for_status(status: str | None) -> str:
    label_map = {
        "pending": ("Chờ review", "badge-pending"),
        "confirmed": ("✓ Đã confirm", "badge-confirmed"),
        "edited": ("✎ Đã chỉnh", "badge-edited"),
        "skipped": ("⊘ Skip", "badge-skipped"),
    }
    label, cls = label_map.get(status or "pending", ("Chờ review", "badge-pending"))
    return f'<span class="badge {cls}">{escape(label)}</span>'


def _ensure_shift_pendings(
    db_path: Path,
    selection: dict,
) -> None:
    """Upsert pending entries for every conv in the selection. Idempotent —
    `upsert_pending_review` keeps existing rows untouched."""
    from app import lead_review as _lr
    shift_id = selection["shift"]["id"]
    date = selection["date"]
    for bucket in ("priority_1", "priority_2", "priority_3", "priority_4"):
        for item in selection[bucket]:
            conv = item["conversation"]
            ev = item["evaluation"] or {}
            _lr.upsert_pending_review(
                db_path,
                conversation_id=conv["external_id"],
                shift_id=shift_id,
                shift_date=date,
                app_score=float(ev.get("total_score") or 0),
                app_max_score=float(ev.get("max_score") or 0),
                app_blacklist=bool(ev.get("blacklist_triggered")),
                snapshot=ev,
                last_message_at=conv.get("last_message_at"),
            )


def _today_iso() -> str:
    from app.shift import today_iso as _t
    return _t()


def _load_selection(date: str, shift_id: str) -> dict:
    """Build the shift selection from cached convs + evaluations.

    Convs that the grading guard rejected (idle < 24h or last msg from sale)
    have no row in the evaluations table. The shift selector and shift list
    need *some* score to bucket and display, so we evaluate them live here.
    This is bounded by the shift's hours of conversations (~30-50 max), well
    under what the dashboard already does for live conv refresh."""
    from app import cache_db
    from app.evaluator import evaluate_conversation
    from app.shift import get_shift_date_range
    from app.shift_selector import select_conversations_for_shift
    cache_db.init_db(NHANH_CACHE_DB)
    ruleset = load_ruleset(RULESET_PATH)
    _ensure_workers(ruleset)
    start, end = get_shift_date_range(date, shift_id)
    items = cache_db.list_conversations_with_evaluations(NHANH_CACHE_DB, start, end)
    for item in items:
        if item.get("evaluation"):
            continue
        conv_id = item["conversation"]["external_id"]
        conv = _build_conv_from_cache(conv_id)
        if conv is None:
            continue
        try:
            item["evaluation"] = evaluate_conversation(conv, ruleset).to_dict()
        except Exception:
            continue
    return select_conversations_for_shift(items, shift_id, date)


def render_review_dashboard(date: str) -> str:
    from app.shift import SHIFT_DEFINITIONS
    from app import lead_review as _lr

    cards = []
    for shift in SHIFT_DEFINITIONS:
        # Make sure today's pendings exist so the progress counter reflects
        # the full quota even before the lead opens the shift page.
        selection = _load_selection(date, shift["id"])
        _ensure_shift_pendings(NHANH_CACHE_DB, selection)
        progress = _lr.get_shift_progress(NHANH_CACHE_DB, date, shift["id"])
        total = max(progress["total"], 1)
        bar_pct = int(progress["done"] * 100 / total)
        cards.append(f"""
        <div class="shift-card">
            <h3>{escape(shift["name"])}</h3>
            <div style="color:#666; font-size: 13px;">
                {shift["start_hour"]}h–{shift["end_hour"]}h ·
                {progress["done"]}/{progress["total"]} đã review
            </div>
            <div class="progress-bar"><div style="width:{bar_pct}%"></div></div>
            <a class="top-nav-link active" href="/review/shift?date={escape(date)}&shift={shift['id']}">Vào chấm →</a>
        </div>
        """)

    history = _lr.get_recent_shifts_summary(NHANH_CACHE_DB, limit_days=7)
    history_html = []
    for row in history:
        if row["shift_date"] == date:
            continue
        done = row["confirmed"] + row["edited"] + row["skipped"]
        history_html.append(
            f'<li>{escape(row["shift_date"])} · {escape(row["shift_id"])}: '
            f'{done}/{row["total"]} '
            f'({row["pending"]} pending)</li>'
        )
    history_block = (
        '<ul style="line-height:1.7">' + "".join(history_html) + "</ul>"
        if history_html else '<p style="color:#888">Chưa có lịch sử review.</p>'
    )

    body = f"""
    <h1>Lead Review Dashboard</h1>
    <p>Ngày: <strong>{escape(date)}</strong></p>
    <div class="shift-grid">{''.join(cards)}</div>
    <h2 style="margin-top:24px;">Lịch sử ngày trước</h2>
    {history_block}
    """
    return _build_review_page(body, title=f"Lead Review – {date}")


def _format_score(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{int(v)}" if v.is_integer() else f"{v:.1f}"


def render_review_shift_page(date: str, shift_id: str) -> str:
    from app.shift import get_shift_by_id
    from app import lead_review as _lr

    shift = get_shift_by_id(shift_id)
    if shift is None:
        return _build_review_page(
            "<p style='color:#c00'>Không tìm thấy ca này.</p>",
            title="Lead Review",
        )

    selection = _load_selection(date, shift_id)
    _ensure_shift_pendings(NHANH_CACHE_DB, selection)
    # Build a map of conv_id → review row so we can show the status badge.
    review_rows = {
        r["conversation_id"]: r
        for r in _lr.list_reviews_for_shift(NHANH_CACHE_DB, date, shift_id)
    }
    progress = _lr.get_shift_progress(NHANH_CACHE_DB, date, shift_id)

    priority_labels = {
        "priority_1": ("🔴 Priority 1 — Blacklist & Complaint", "MUST review"),
        "priority_2": ("🟡 Priority 2 — Điểm thấp", "Sale cần cải thiện"),
        "priority_3": ("🟢 Priority 3 — Trung bình", "Random sample"),
        "priority_4": ("⭐ Priority 4 — Điểm cao", "Training material"),
    }

    blocks = []
    for key, (heading, subtitle) in priority_labels.items():
        items = selection[key]
        rows_html = []
        for item in items:
            conv = item["conversation"]
            ev = item["evaluation"] or {}
            score = ev.get("total_score") or 0
            mx = ev.get("max_score") or 0
            review = review_rows.get(conv["external_id"]) or {"status": "pending"}
            is_blacklist = ev.get("blacklist_triggered")
            blacklist_badge = (
                '<span class="badge badge-blacklist">Blacklist</span>'
                if is_blacklist else ""
            )
            customer = conv.get("customer_name") or conv.get("summary", {}).get("pageUserName") or "Khách"
            channel = conv.get("channel") or "—"
            lead_score_chip = ""
            if review.get("status") in {"confirmed", "edited"} and review.get("lead_score") is not None:
                tone = "#0a7f83" if review["status"] == "confirmed" else "#1c4fa6"
                lead_score_chip = (
                    f' <span style="color:{tone}; font-weight:600;">'
                    f'→ Lead: {_format_score(review["lead_score"])}đ</span>'
                )
            rows_html.append(f"""
            <div class="conv-row">
                <div>
                    <a href="/review/conversation?id={escape(conv['external_id'], quote=True)}&date={escape(date)}&shift={shift_id}">{escape(conv['external_id'])}</a>
                    <span style="color:#666"> · {escape(str(customer))} · {escape(str(channel))}</span>
                    <div style="font-size:12px; color:#888;">App: {_format_score(score)}/{_format_score(mx)}{lead_score_chip} {blacklist_badge}</div>
                </div>
                <div>{_badge_for_status(review.get("status"))}</div>
            </div>
            """)
        if not rows_html:
            rows_html.append('<div style="color:#888; padding:8px 0;">— Không có hội thoại nào —</div>')
        blocks.append(f"""
        <div class="priority-block">
            <h3>{heading} <span style="font-size:13px; color:#888; font-weight:normal">({len(items)})</span></h3>
            <div style="color:#666; font-size:13px; margin-bottom:8px;">{escape(subtitle)}</div>
            {''.join(rows_html)}
        </div>
        """)

    incomplete_note = ""
    if not selection["is_complete"]:
        incomplete_note = (
            f'<div style="background:#fff3cd; border:1px solid #ffeeba; padding:10px;'
            f' border-radius:8px; margin-bottom:16px;">'
            f'⚠ Ca này chỉ có {selection["total_in_shift"]} hội thoại — '
            f'không đủ {selection["quota"]["total"]} slot. Đang hiển thị tất cả.'
            f'</div>'
        )

    body = f"""
    <a href="/review" style="color:#0a7f83">← Quay lại dashboard</a>
    <h1>{escape(shift["name"])} – {escape(date)}</h1>
    <p>Ca {shift['start_hour']}h–{shift['end_hour']}h · Đã review: <strong>{progress['done']}/{progress['total']}</strong> ·
       Còn lại: <strong>{progress['pending']}</strong></p>
    {incomplete_note}
    {''.join(blocks)}
    """
    return _build_review_page(body, title=f"Ca {shift['name']} {date}")


def _build_conv_from_cache(conv_id: str) -> dict | None:
    """Hydrate a full Conversation-like dict (with all messages) from cache."""
    from app import cache_db
    summary = cache_db.get_conversation_summary(NHANH_CACHE_DB, conv_id) or {}
    if not summary:
        return None
    msgs_raw = cache_db.get_messages(NHANH_CACHE_DB, conv_id)
    # Skip system notifications mirroring sync_worker filtering.
    from app.sync_worker import _is_system_notification
    msgs_raw = [m for m in msgs_raw if not _is_system_notification(m)]
    # Reconstruct via nhanh_adapter to share the same mapping logic.
    from app.nhanh_adapter import build_conversation
    payload = {"data": msgs_raw}
    try:
        conv = build_conversation(conv_id, payload, summary_item=summary)
    except Exception:
        return None
    return conv


def render_review_conversation_page(conv_id: str, date: str, shift_id: str) -> str:
    from app import cache_db, lead_review as _lr

    review = _lr.get_review(NHANH_CACHE_DB, conv_id)
    conv = _build_conv_from_cache(conv_id)
    if conv is None:
        return _build_review_page(
            f"<p style='color:#c00'>Không tìm thấy hội thoại {escape(conv_id)} trong cache.</p>",
            title="Lead Review",
        )

    # Re-evaluate live so the lead sees the current ruleset.
    from app.evaluator import evaluate_conversation
    from app.schemas import load_ruleset as _lr_loader
    ruleset = _lr_loader(RULESET_PATH)
    result = evaluate_conversation(conv, ruleset).to_dict()

    # Transcript
    msg_blocks = []
    for m in conv.messages:
        role = "NV" if m.sender_type == "employee" else "KH"
        cls = "employee" if m.sender_type == "employee" else "customer"
        time_label = parse_display_datetime(m.sent_at)
        msg_blocks.append(f"""
        <div class="msg {cls}">
            <div class="meta">{escape(role)} · {escape(time_label)}</div>
            <div>{escape(m.text)}</div>
        </div>
        """)

    # Findings
    findings_html = []
    for f in result["findings"]:
        if f.get("skipped"):
            continue
        cls = "ok" if f["passed"] else "bad"
        sign = "✓" if f["passed"] else "✗"
        findings_html.append(
            f'<div class="findings"><span class="{cls}">{sign}</span> '
            f'<strong>{escape(f["rule_name"])}</strong> '
            f'({_format_score(f["score"])}/{_format_score(f["max_score"])}) — '
            f'{escape(f.get("explanation") or "")}</div>'
        )

    blacklist_html = ""
    if result["blacklist_findings"]:
        items = "".join(
            f'<li><strong>{escape(f["rule_name"])}</strong>: {escape(f.get("explanation") or "")}</li>'
            for f in result["blacklist_findings"]
        )
        blacklist_html = f'<div style="color:#c00; margin:8px 0;">⚠ Blacklist trigger:<ul>{items}</ul></div>'

    app_score = result["total_score"]
    app_max = result["max_score"]
    app_grade = result["grade"]
    review_status = (review or {}).get("status", "pending")
    lead_score_existing = (review or {}).get("lead_score")
    lead_comment_existing = (review or {}).get("lead_comment") or ""

    # Build a prominent "đã chấm" banner so the lead sees their previous
    # decision survived the reload.
    saved_banner = ""
    if review_status in {"confirmed", "edited", "skipped"} and review is not None:
        from datetime import datetime as _dt2
        reviewed_at = review.get("reviewed_at")
        when = _dt2.fromtimestamp(reviewed_at).strftime("%H:%M %d/%m") if reviewed_at else "—"
        lead_score_text = (
            f"<strong>{_format_score(lead_score_existing)}đ</strong>"
            if lead_score_existing is not None else "<em>không chấm điểm</em>"
        )
        saved_banner = f"""
        <div style="background:#e6f5f4; border:1px solid #0a7f83; border-radius:8px;
                    padding:12px 16px; margin:12px 0;">
            ✓ Lead đã review: {_badge_for_status(review_status)} · {lead_score_text} ·
            <span style="color:#666">lưu lúc {when}</span>
        </div>
        """

    body = f"""
    <a href="/review/shift?date={escape(date)}&shift={shift_id}" style="color:#0a7f83">← Quay lại ca</a>
    <h1>Conversation {escape(conv_id)}</h1>
    <p style="color:#666;">{escape(conv.channel or '')} · {len(conv.messages)} tin nhắn (đã lọc notifications)</p>
    {saved_banner}

    <div class="two-col">
        <div class="transcript">
            <h3>Transcript</h3>
            {''.join(msg_blocks) or '<p style="color:#888">Hội thoại rỗng.</p>'}
        </div>

        <div>
            <div class="review-form" style="margin-bottom:14px;">
                <h3>App chấm</h3>
                <p><strong>{_format_score(app_score)}/{_format_score(app_max)}</strong> · {escape(app_grade)} {_badge_for_status(review_status)}</p>
                {blacklist_html}
                <div style="max-height:240px; overflow-y:auto; border:1px solid #eee; border-radius:6px; padding:8px;">
                    {''.join(findings_html) or '<div style="color:#888">Không có rule fail.</div>'}
                </div>
            </div>

            <form class="review-form" method="post" action="/api/review/submit"
                  onsubmit="return submitReview(event, this)">
                <h3>Lead chấm</h3>
                <input type="hidden" name="conversation_id" value="{escape(conv_id, quote=True)}">
                <input type="hidden" name="date" value="{escape(date, quote=True)}">
                <input type="hidden" name="shift_id" value="{escape(shift_id, quote=True)}">

                <label><input type="radio" name="decision" value="confirmed" checked> Confirm điểm app ({_format_score(app_score)}đ)</label>
                <label><input type="radio" name="decision" value="edited"> Sửa điểm:
                    <input type="number" step="0.5" name="lead_score" value="{_format_score(lead_score_existing or app_score)}" style="width:100px;display:inline-block">
                </label>
                <label><input type="checkbox" name="lead_blacklist" value="1"> Flag Blacklist</label>
                <label><input type="radio" name="decision" value="skipped"> Skip (review sau)</label>

                <label>Ghi chú:</label>
                <textarea name="lead_comment" rows="3" placeholder="Lý do chỉnh điểm hoặc ghi chú coaching...">{escape(lead_comment_existing)}</textarea>

                <button type="submit">Submit</button>
                <a class="top-nav-link" href="/review/shift?date={escape(date)}&shift={shift_id}">Huỷ</a>
            </form>
        </div>
    </div>

    <div id="review-toast" style="display:none; position:fixed; top:80px; right:20px;
         background:#0a7f83; color:#fff; padding:14px 20px; border-radius:8px;
         box-shadow:0 4px 16px rgba(0,0,0,0.2); z-index:9999; font-weight:600;"></div>

    <script>
    function showToast(message, tone) {{
        const el = document.getElementById('review-toast');
        el.textContent = message;
        el.style.background = tone === 'error' ? '#c73641' : '#0a7f83';
        el.style.display = 'block';
        setTimeout(() => {{ el.style.display = 'none'; }}, 3500);
    }}

    async function submitReview(ev, form) {{
        ev.preventDefault();
        const submitBtn = form.querySelector('button[type=submit]');
        if (submitBtn) {{ submitBtn.disabled = true; submitBtn.textContent = 'Đang lưu...'; }}
        const fd = new FormData(form);
        const data = {{
            conversation_id: fd.get('conversation_id'),
            date: fd.get('date'),
            shift_id: fd.get('shift_id'),
            decision: fd.get('decision'),
            lead_score: fd.get('lead_score'),
            lead_blacklist: fd.get('lead_blacklist') === '1',
            lead_comment: fd.get('lead_comment') || '',
        }};
        try {{
            const resp = await fetch('/api/review/submit', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(data),
            }});
            const out = await resp.json();
            if (out.ok) {{
                const statusLabel = {{
                    confirmed: 'Đã confirm',
                    edited:    'Đã chỉnh điểm',
                    skipped:   'Đã skip',
                }}[out.status] || out.status;
                const scoreText = (out.lead_score !== null && out.lead_score !== undefined)
                    ? ` · Lead chấm: ${{out.lead_score}}đ` : '';
                showToast(`✓ ${{statusLabel}}${{scoreText}}. Đang reload...`, 'ok');
                // Reload conv detail page so the form & badge reflect saved state.
                setTimeout(() => window.location.reload(), 800);
            }} else {{
                showToast('Lưu thất bại: ' + (out.error || 'unknown'), 'error');
                if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit'; }}
            }}
        }} catch (err) {{
            showToast('Lỗi mạng: ' + err.message, 'error');
            if (submitBtn) {{ submitBtn.disabled = false; submitBtn.textContent = 'Submit'; }}
        }}
        return false;
    }}
    </script>
    """
    return _build_review_page(body, title=f"Review {conv_id}")


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "MessageQALocalViewer/0.2"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in {"/", "/dashboard"}:
            live_conversation_id = query.get("live_conversation_id", [None])[0]
            channel_filter = query.get("channel", [None])[0]
            date_filter = query.get("date", ["7days"])[0]
            state_filter = query.get("state", ["all"])[0]
            self.send_html(render_dashboard(
                page="live",
                live_conversation_id=live_conversation_id,
                channel_filter=channel_filter,
                date_filter=date_filter,
                state_filter=state_filter,
            ))
            return
        if parsed.path == "/dashboard/team":
            self.send_html(render_dashboard(page="team"))
            return
        if parsed.path == "/api/results":
            live_conversation_id = query.get("live_conversation_id", [None])[0]
            ruleset = load_ruleset(RULESET_PATH)
            dashboard = load_dashboard_data(ruleset)
            nhanh_data = load_nhanh_data(ruleset, live_conversation_id=live_conversation_id)
            payload = {
                "evaluation_results": dashboard["evaluation_results"],
                "scorecards": dashboard["scorecards"],
                "top_weak_skills": dashboard["top_weak_skills"],
                "training_recommendations": dashboard["training_recommendations"],
                "nhanh_recent": nhanh_data["recent"],
                "nhanh_live_case": (
                    {
                        "conversation_id": nhanh_data["live_case"]["conversation"].external_id,
                        "message_count": len(nhanh_data["live_case"]["conversation"].messages),
                        "result": nhanh_data["live_case"]["result"],
                    }
                    if nhanh_data["live_case"]
                    else None
                ),
            }
            self.send_json(payload)
            return
        if parsed.path == "/review":
            date = query.get("date", [_today_iso()])[0]
            self.send_html(render_review_dashboard(date))
            return
        if parsed.path == "/review/shift":
            date = query.get("date", [_today_iso()])[0]
            shift_id = query.get("shift", ["morning"])[0]
            self.send_html(render_review_shift_page(date, shift_id))
            return
        if parsed.path == "/review/conversation":
            conv_id = query.get("id", [""])[0]
            date = query.get("date", [_today_iso()])[0]
            shift_id = query.get("shift", ["morning"])[0]
            self.send_html(render_review_conversation_page(conv_id, date, shift_id))
            return
        if parsed.path == "/api/sync-status":
            light, heavy = _ensure_workers(load_ruleset(RULESET_PATH))
            if light is None and heavy is None:
                self.send_json({"enabled": False})
                return
            light_status = light.get_status() if light else {}
            heavy_status = heavy.get_status() if heavy else {}
            # Banner JS expects top-level `is_syncing`/`progress`; treat either
            # worker active as "syncing" and merge progress from heavy (only
            # heavy publishes per-conv progress).
            self.send_json({
                **light_status,
                "is_syncing": bool(light_status.get("is_syncing")) or bool(heavy_status.get("is_syncing")),
                "progress": heavy_status.get("progress"),
                "light": light_status,
                "heavy": heavy_status,
            })
            return
        if parsed.path == "/integrations/nhanh/oauth/callback":
            self.send_html(render_nhanh_callback(parse_qs(parsed.query)))
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/review/submit":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                self.send_json({"ok": False, "error": f"bad json: {exc}"})
                return
            conv_id = str(payload.get("conversation_id") or "")
            decision = str(payload.get("decision") or "")
            if not conv_id or decision not in {"confirmed", "edited", "skipped"}:
                self.send_json({"ok": False, "error": "missing conversation_id or invalid decision"})
                return
            from app import lead_review as _lr
            from app.evaluator import evaluate_conversation
            # Always evaluate live so the lead's "confirm" copies the same
            # score they're looking at on screen — the DB-cached app_score
            # might be stale (was 0 when the pending row was first created).
            live_eval: dict | None = None
            conv = _build_conv_from_cache(conv_id)
            if conv is not None:
                ruleset = load_ruleset(RULESET_PATH)
                live_eval = evaluate_conversation(conv, ruleset).to_dict()
            existing = _lr.get_review(NHANH_CACHE_DB, conv_id)
            if existing is None and live_eval is not None:
                # Race: lead opened the form before the shift page initialised
                # the pending row. Create one on the fly.
                _lr.upsert_pending_review(
                    NHANH_CACHE_DB,
                    conversation_id=conv_id,
                    shift_id=str(payload.get("shift_id") or "morning"),
                    shift_date=str(payload.get("date") or _today_iso()),
                    app_score=float(live_eval["total_score"]),
                    app_max_score=float(live_eval["max_score"]),
                    app_blacklist=bool(live_eval["blacklist_triggered"]),
                    snapshot=live_eval,
                    last_message_at=None,
                )
                existing = _lr.get_review(NHANH_CACHE_DB, conv_id)
            if existing is None:
                self.send_json({"ok": False, "error": "conv not in cache"})
                return
            if decision == "confirmed":
                # Prefer the just-computed live score; fall back to DB if the
                # conv somehow couldn't be hydrated.
                lead_score = (
                    float(live_eval["total_score"]) if live_eval else float(existing["app_score"] or 0)
                )
            elif decision == "edited":
                try:
                    lead_score = float(payload.get("lead_score"))
                except (TypeError, ValueError):
                    self.send_json({"ok": False, "error": "lead_score required for 'edited'"})
                    return
            else:  # skipped
                lead_score = None
            _lr.submit_lead_review(
                NHANH_CACHE_DB,
                conversation_id=conv_id,
                lead_score=lead_score,
                lead_blacklist=bool(payload.get("lead_blacklist")),
                lead_comment=str(payload.get("lead_comment") or ""),
                status=decision,
            )
            self.send_json({
                "ok": True,
                "status": decision,
                "lead_score": lead_score,
                "app_score": float(live_eval["total_score"]) if live_eval else None,
                "max_score": float(live_eval["max_score"]) if live_eval else None,
            })
            return
        if parsed.path in {"/api/refresh", "/api/refresh-now"}:
            light, heavy = _ensure_workers(load_ruleset(RULESET_PATH))
            if light is None and heavy is None:
                self.send_json({"ok": False, "reason": "nhanh_config_missing"})
                return
            triggered_light = light.trigger_now() if light else False
            triggered_heavy = heavy.trigger_now() if heavy else False
            if triggered_light or triggered_heavy:
                self.send_json({
                    "ok": True,
                    "triggered_light": triggered_light,
                    "triggered_heavy": triggered_heavy,
                })
            else:
                self.send_json({"ok": False, "reason": "sync_in_progress"})
            return
        if parsed.path == "/integrations/nhanh/webhooks":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            payload = {
                "ok": True,
                "message": "Webhook received locally.",
                "received_bytes": len(body),
            }
            self.send_json(payload)
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _local_lan_ip() -> str | None:
    """Best-effort LAN IP discovery so we can print a clickable URL for LAN
    clients. Returns None if no non-loopback address is available."""
    import socket as _socket
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def run(host: str | None = None, port: int | None = None) -> None:
    bind_host = host or _os.environ.get("VIEWER_HOST", "0.0.0.0")
    try:
        bind_port = port or int(_os.environ.get("VIEWER_PORT", "8080"))
    except ValueError:
        bind_port = 8080
    server = ThreadingHTTPServer((bind_host, bind_port), ViewerHandler)
    print(f"Viewer running at http://{bind_host}:{bind_port}/dashboard")
    if bind_host in {"0.0.0.0", "::"}:
        lan = _local_lan_ip()
        print(f"  - Local:   http://127.0.0.1:{bind_port}/dashboard")
        if lan:
            print(f"  - Network: http://{lan}:{bind_port}/dashboard")
        print("  ⚠ Server is exposed on LAN. No auth — only run inside a trusted network.")
    server.serve_forever()


if __name__ == "__main__":
    run()
