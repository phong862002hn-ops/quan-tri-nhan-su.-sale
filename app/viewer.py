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


def load_nhanh_data(ruleset, live_conversation_id: str | None = None) -> dict:
    if not NHANH_CONFIG_PATH.exists():
        return {
            "enabled": False,
            "error": None,
            "recent": [],
            "live_case": None,
            "live_error": None,
        }

    try:
        config = load_nhanh_config(NHANH_CONFIG_PATH)
    except Exception as exc:
        return {
            "enabled": True,
            "error": f"Khong doc duoc config Nhanh: {exc}",
            "recent": [],
            "live_case": None,
            "live_error": None,
        }

    recent_items: list[dict] = []
    error_message: str | None = None
    try:
        summary_payload = nhanh_list_conversations(config, size=8, conversation_type=2)
        recent_items = extract_items(summary_payload, "conversations", "conversation", "items")
    except NhanhClientError as exc:
        error_message = str(exc)

    enriched_recent_items: list[dict] = []
    for item in recent_items:
        enriched_item = dict(item)
        conversation_id = str(item.get("id") or "")
        try:
            messages_payload = nhanh_list_messages(config, conversation_id=conversation_id, size=50)
            conversation = build_conversation(
                conversation_id,
                messages_payload,
                summary_item=item,
            )
            result = evaluate_conversation(conversation, ruleset).to_dict()
            enriched_item["qa_summary"] = {
                "total_score": result["total_score"],
                "max_score": result["max_score"],
                "grade": result["grade"],
                "failed_rule_count": len([finding for finding in result["findings"] if not finding["passed"]]),
                "blacklist_triggered": result["blacklist_triggered"],
                "blacklist_count": len(result["blacklist_findings"]),
            }
        except NhanhClientError:
            enriched_item["qa_summary"] = None
        enriched_recent_items.append(enriched_item)

    live_case = None
    live_error = None
    if live_conversation_id:
        try:
            summary_item = next((item for item in enriched_recent_items if str(item.get("id")) == live_conversation_id), None)
            messages_payload = nhanh_list_messages(config, conversation_id=live_conversation_id, size=50)
            conversation = build_conversation(
                live_conversation_id,
                messages_payload,
                summary_item=summary_item,
            )
            result = evaluate_conversation(conversation, ruleset).to_dict()
            live_case = {"conversation": conversation, "result": result}
        except NhanhClientError as exc:
            live_error = str(exc)

    return {
        "enabled": True,
        "error": error_message,
        "recent": enriched_recent_items,
        "live_case": live_case,
        "live_error": live_error,
    }


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


def render_findings(result: dict) -> str:
    failed = [item for item in result["findings"] if not item["passed"]]
    blacklist = result["blacklist_findings"]
    lines = []
    for item in failed[:8]:
        lines.append(
            f'<div class="warning-line"><span class="warning-dot amber"></span><strong>{escape(item["rule_name"])}</strong>: {escape(item["explanation"])}</div>'
        )
    for item in blacklist:
        lines.append(
            f'<div class="warning-line"><span class="warning-dot red"></span><strong>{escape(item["rule_name"])}</strong>: {escape(item["explanation"])}</div>'
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
    for item in items:
        conversation_id = str(item.get("id") or "-")
        customer_name = str(item.get("pageUserName") or "-")
        updated_at = parse_display_datetime(item.get("updatedAt"))
        has_reply = bool(item.get("hasReply"))
        has_phone = bool(item.get("hasPhone"))
        last_message = str(item.get("lastMessage") or "-").strip()
        status_text = "Da phan hoi" if has_reply else "Chua phan hoi"
        phone_text = "Co SDT" if has_phone else "Chua co SDT"
        selected_class = " selected" if selected_conversation_id == conversation_id else ""
        qa_summary = item.get("qa_summary") or {}
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
        rows.append(
            f"""
            <a class="nhanh-link{selected_class}" href="/dashboard?live_conversation_id={escape(conversation_id)}#live-review">
                <div class="nhanh-link-head">
                    <strong>{escape(customer_name)}</strong>
                    <span class="nhanh-link-cta">View tin nhan</span>
                </div>
                <div class="nhanh-link-id">{escape(conversation_id)}</div>
                <div class="nhanh-link-meta">
                    <span>{escape(updated_at)}</span>
                    <span>{escape(status_text)}</span>
                    <span>{escape(phone_text)}</span>
                </div>
                {qa_html}
                <div class="nhanh-link-preview">{escape(last_message[:140] + ('...' if len(last_message) > 140 else ''))}</div>
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


def render_nhanh_panel(nhanh_data: dict, live_conversation_id: str | None) -> str:
    if not nhanh_data["enabled"]:
        return ""
    recent_items = []
    for item in nhanh_data["recent"]:
        recent_items.append(dict(item))
    error_html = ""
    if nhanh_data["error"]:
        error_html += f'<div class="nhanh-error">{escape(nhanh_data["error"])}</div>'
    if nhanh_data["live_error"]:
        error_html += f'<div class="nhanh-error">{escape(nhanh_data["live_error"])}</div>'
    return f"""
    <section class="panel nhanh-panel" style="margin-bottom: 22px;">
        <div class="panel-head"><h2>Nhanh Live Review</h2></div>
        <div class="panel-body">
            <form class="nhanh-form" method="get" action="/dashboard">
                <input
                    type="text"
                    name="live_conversation_id"
                    value="{escape(live_conversation_id or '')}"
                    placeholder="Nhap conversationId tu Nhanh"
                >
                <button type="submit">Load Live Conversation</button>
            </form>
            <div class="nhanh-note">Viewer nay dang dung config local tai <code>data/nhanh_config.local.json</code> va goi truc tiep Vpage API.</div>
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
    return f"""
    <nav class="top-nav">
        <a class="top-nav-link {live_class}" href="/dashboard">Conversation Review</a>
        <a class="top-nav-link {team_class}" href="/dashboard/team">Team Coaching</a>
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


def render_dashboard(page: str = "live", live_conversation_id: str | None = None) -> str:
    ruleset = load_ruleset(RULESET_PATH)
    dashboard = load_dashboard_data(ruleset)
    nhanh_data = load_nhanh_data(ruleset, live_conversation_id=live_conversation_id)
    nhanh_html = render_nhanh_panel(nhanh_data, live_conversation_id)
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
                <div class="hero-pill">Nguồn dữ liệu: local file + Nhanh Vpage API</div>
        """
        body_html = f"""
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


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "MessageQALocalViewer/0.2"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in {"/", "/dashboard"}:
            live_conversation_id = query.get("live_conversation_id", [None])[0]
            self.send_html(render_dashboard(page="live", live_conversation_id=live_conversation_id))
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
        if parsed.path == "/integrations/nhanh/oauth/callback":
            self.send_html(render_nhanh_callback(parse_qs(parsed.query)))
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
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


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), ViewerHandler)
    print(f"Viewer running at http://{host}:{port}/dashboard")
    server.serve_forever()


if __name__ == "__main__":
    run()
