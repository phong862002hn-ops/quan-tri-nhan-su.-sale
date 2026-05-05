from __future__ import annotations

from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json

from app.employee_scorecard import build_employee_scorecards, build_employee_scorecards_from_history
from app.evaluator import evaluate_conversation
from app.exporter import export_results_csv, export_scorecard_csv
from app.history import build_employee_trend, load_all_history, save_evaluation_results, snapshot_ruleset_if_new
from app.response_time import format_seconds
from app.alert_engine import run_alerts
from app.department_overview import build_department_overview
from app.review_store import load_all_reviews, save_review
from app.weekly_analysis import (
    analyze_period_comparison,
    analyze_weekly_comparison,
    resolve_preset_ranges,
    PRESETS,
    PRESET_PERIOD_LABELS,
)
from app.schemas import Conversation, load_conversations, load_ruleset
from app.training import load_training_modules


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"
TRAINING_PATH = BASE_DIR / "data" / "training_modules.json"


def parse_display_datetime(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def format_score(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _render_response_time(result: dict) -> str:
    rt = result.get("response_time") or {}
    avg = rt.get("avg_response_seconds")
    max_rt = rt.get("max_response_seconds")
    unanswered = rt.get("unanswered_customer_messages", 0)
    if avg is None:
        return ""
    color = "#0b7a4b" if avg <= 300 else ("#b97800" if avg <= 900 else "#c73641")
    unanswered_html = (
        f' <span style="color:#c73641;font-weight:700">· {unanswered} tin chưa trả lời</span>'
        if unanswered else ""
    )
    return (
        f'<div class="rt-row">'
        f'<span class="rt-label">Phản hồi TB:</span> '
        f'<span style="font-weight:700;color:{color}">{escape(format_seconds(avg))}</span>'
        f' <span class="rt-sep">·</span> Max: {escape(format_seconds(max_rt))}'
        f'{unanswered_html}'
        f'</div>'
    )


def render_trend_sparkline(trend: list[dict]) -> str:
    if len(trend) < 2:
        if not trend:
            return '<span class="trend-na">Chưa có lịch sử</span>'
        return f'<span class="trend-na">{trend[0]["date"]}: {trend[0]["avg_score"]}</span>'

    scores = [t["avg_score"] for t in trend]
    min_s, max_s = min(scores), max(scores)
    rng = max_s - min_s or 1
    width, height = 120, 36
    pts = []
    for i, s in enumerate(scores):
        x = round(i / (len(scores) - 1) * width, 1)
        y = round(height - (s - min_s) / rng * (height - 4) - 2, 1)
        pts.append(f"{x},{y}")
    polyline = " ".join(pts)
    last = scores[-1]
    prev = scores[-2]
    arrow = "▲" if last >= prev else "▼"
    color = "#0b7a4b" if last >= prev else "#c73641"
    labels = "".join(
        f'<title>{t["date"]}: {t["avg_score"]} ({t["count"]} ca)</title>'
        for t in trend
    )
    return (
        f'<div class="sparkline-wrap">'
        f'<svg width="{width}" height="{height}" class="sparkline">'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
        f'{labels}</svg>'
        f'<span class="trend-arrow" style="color:{color}">{arrow} {last}</span>'
        f'</div>'
    )


def _render_review_panel(conversation_id: str, reviews: dict) -> str:
    review = reviews.get(conversation_id)
    if review:
        override_html = (
            f' · Override điểm: <strong>{review.score_override}</strong>'
            if review.score_override is not None else ""
        )
        return (
            f'<div class="review-panel reviewed" id="rp-{escape(conversation_id)}">'
            f'<span class="reviewed-badge">✓ Đã review</span> '
            f'<span class="review-meta">bởi {escape(review.reviewer)} · {escape(review.reviewed_at[:10])}</span>'
            f'{override_html}'
            f'<div class="review-note-text">{escape(review.note)}</div>'
            f'<button class="review-edit-btn" onclick="openReviewForm(\'{escape(conversation_id)}\')">Sửa</button>'
            f'</div>'
        )
    return (
        f'<div class="review-panel" id="rp-{escape(conversation_id)}">'
        f'<button class="review-open-btn" onclick="openReviewForm(\'{escape(conversation_id)}\')">+ Ghi chú review</button>'
        f'</div>'
    )


def load_dashboard_data() -> dict:
    ruleset = load_ruleset(RULESET_PATH)
    conversations = load_conversations(CONVERSATIONS_PATH)
    training_modules = load_training_modules(TRAINING_PATH)
    cases = []
    evaluation_results = []
    for conversation in conversations:
        result = evaluate_conversation(conversation, ruleset).to_dict()
        evaluation_results.append(result)
        cases.append({"conversation": conversation, "result": result})

    ruleset_version = snapshot_ruleset_if_new(RULESET_PATH)
    reviews = load_all_reviews()

    # Build scorecards: ưu tiên từ history nếu có, fallback về session hiện tại
    history_scorecards = build_employee_scorecards_from_history()
    scorecards = history_scorecards if history_scorecards else build_employee_scorecards(
        conversations, evaluation_results, training_modules
    )

    # Gắn trend vào scorecards nếu dùng session fallback
    if not history_scorecards:
        for sc in scorecards:
            sc.setdefault("trend", [])
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

    alerts = run_alerts()

    return {
        "cases": cases,
        "evaluation_results": evaluation_results,
        "scorecards": scorecards,
        "top_training_employees": top_training_employees,
        "top_weak_skills": top_weak_skills,
        "training_recommendations": all_recommendations,
        "reviews": reviews,
        "alerts": alerts,
        "ruleset_version": ruleset_version,
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
            attachments = '<div class="attachment-note">' + ", ".join(
                f"[{escape(item.type.upper())}] {escape(item.name or 'attachment')}"
                for item in message.attachments
            ) + "</div>"
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


def render_case(case: dict, reviews: dict | None = None) -> str:
    conversation: Conversation = case["conversation"]
    result: dict = case["result"]
    tone_class, tone_label = get_case_tone(result)
    employee_name = conversation.employee.name if conversation.employee else "Unknown Employee"
    employee_id = conversation.employee.id if conversation.employee else ""
    grade_key = result["grade"]
    blacklist_key = "yes" if result["blacklist_triggered"] else "no"
    channel_key = conversation.channel.lower()
    started_at_full = conversation.messages[0].sent_at if conversation.messages else ""
    date_key = started_at_full[:10] if started_at_full else ""
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
    return f"""
    <article class="case-thread"
        data-employee-id="{escape(employee_id)}"
        data-employee="{escape(employee_name.lower())}"
        data-grade="{escape(grade_key)}"
        data-blacklist="{escape(blacklist_key)}"
        data-channel="{escape(channel_key)}"
        data-date="{escape(date_key)}"
        data-conv-id="{escape(conversation.external_id.lower())}"
    >
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
                    {_render_response_time(result)}
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
                        <div class="score-sub">Blacklist: {'Có' if result['blacklist_triggered'] else 'Không'} · Rules v{escape(result.get('ruleset_version','?'))}</div>
                    </div>
                    {"".join(category_cards)}
                </div>

                <div class="summary-box">
                    <strong>Tổng quan:</strong> {escape(f"{len([item for item in result['findings'] if not item['passed']])} rule fail, {len(result['blacklist_findings'])} blacklist trigger.")}
                </div>

                <div class="warning-box">
                    {render_findings(result)}
                </div>
                {_render_review_panel(conversation.external_id, reviews or {{}})}
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
        trend_html = render_trend_sparkline(item.get("trend", []))
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
                    <div>Ca đã chấm: <strong>{escape(str(item["conversation_count"]))}</strong></div>
                </div>
                <div class="trend-row">
                    <span class="trend-label">Trend điểm:</span>
                    {trend_html}
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


_VERDICT_LABEL = {
    "improved": ("📈", "Cải thiện", "verdict-up"),
    "declined": ("📉", "Tụt dốc", "verdict-down"),
    "stable": ("➡", "Ổn định", "verdict-flat"),
    "new": ("🆕", "Mới", "verdict-new"),
    "left": ("⏸", "Vắng", "verdict-flat"),
}


def _delta_pill(delta: dict, suffix: str = "", invert: bool = False) -> str:
    """Render một pill hiển thị delta với mũi tên màu."""
    val = delta.get("value")
    direction = delta.get("direction", "flat")
    if val is None or direction == "flat":
        return '<span class="delta-pill flat">—</span>'
    if direction == "new":
        return f'<span class="delta-pill new">+{val}{suffix} (mới)</span>'
    arrow = "▲" if direction == "up" else "▼"
    is_good = (direction == "up") if not invert else (direction == "down")
    cls = "up" if is_good else "down"
    sign = "+" if val > 0 else ""
    pct_str = ""
    if delta.get("pct") is not None:
        pct_str = f' ({"+" if delta["pct"] > 0 else ""}{delta["pct"]}%)'
    return f'<span class="delta-pill {cls}">{arrow} {sign}{val}{suffix}{pct_str}</span>'


def _fmt_metric(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value}{suffix}"


def _render_weekly_panel(weekly: dict) -> str:
    a_label = weekly.get("period_a_label", "Kỳ này")
    b_label = weekly.get("period_b_label", "Kỳ trước")

    if not weekly["employees"]:
        return (
            '<section class="panel" style="margin-bottom: 22px;">'
            f'<div class="panel-head"><h2>Phân tích nhân sự ({escape(a_label)} vs {escape(b_label)})</h2></div>'
            '<div class="panel-body">'
            f'<p style="margin:0;color:var(--muted)">Chưa đủ dữ liệu để so sánh {escape(a_label)} ({escape(weekly["this_week"]["label"])}) với {escape(b_label)} ({escape(weekly["last_week"]["label"])}).</p>'
            '</div>'
            '</section>'
        )

    summary = weekly["summary"]
    team_this = summary["team_this"]
    team_last = summary["team_last"]
    team_delta = summary["team_delta_avg_score"]

    rows = []
    for e in weekly["employees"]:
        icon, label, vcls = _VERDICT_LABEL[e["verdict"]]
        tw = e["this_week"]
        lw = e["last_week"]
        d = e["delta"]
        rows.append(
            f'<tr class="{vcls}">'
            f'<td><strong>{escape(e["employee_name"])}</strong></td>'
            f'<td><span class="verdict-tag {vcls}">{icon} {label}</span></td>'
            f'<td>{_fmt_metric(tw["avg_score"])}</td>'
            f'<td>{_fmt_metric(lw["avg_score"])}</td>'
            f'<td>{_delta_pill(d["avg_score"])}</td>'
            f'<td>{_fmt_metric(tw["passed_rate"], "%")}</td>'
            f'<td>{_delta_pill(d["passed_rate"], suffix="%")}</td>'
            f'<td>{tw["blacklist_count"]}</td>'
            f'<td>{_delta_pill(d["blacklist_count"], invert=True)}</td>'
            f'<td>{tw["conversation_count"]}</td>'
            f'</tr>'
        )

    return f"""
        <section class="panel weekly-panel" style="margin-bottom: 22px;">
            <div class="panel-head">
                <h2>Phân tích nhân sự ({escape(a_label)} vs {escape(b_label)})</h2>
                <div class="weekly-meta">
                    {escape(a_label)}: <strong>{escape(weekly["this_week"]["label"])}</strong>
                    · {escape(b_label)}: <strong>{escape(weekly["last_week"]["label"])}</strong>
                </div>
            </div>
            <div class="panel-body">
                <div class="team-summary">
                    <div class="team-card">
                        <div class="team-card-label">Avg toàn team — {escape(a_label.lower())}</div>
                        <div class="team-card-value">{_fmt_metric(team_this["avg_score"])}</div>
                        <div class="team-card-sub">{team_this["conversation_count"]} ca · pass rate {_fmt_metric(team_this["passed_rate"], "%")}</div>
                    </div>
                    <div class="team-card">
                        <div class="team-card-label">Avg toàn team — {escape(b_label.lower())}</div>
                        <div class="team-card-value">{_fmt_metric(team_last["avg_score"])}</div>
                        <div class="team-card-sub">{team_last["conversation_count"]} ca · pass rate {_fmt_metric(team_last["passed_rate"], "%")}</div>
                    </div>
                    <div class="team-card">
                        <div class="team-card-label">Thay đổi avg điểm</div>
                        <div class="team-card-value">{_delta_pill(team_delta)}</div>
                        <div class="team-card-sub">Blacklist: {team_this["blacklist_count"]} ({escape(a_label.lower())}) vs {team_last["blacklist_count"]} ({escape(b_label.lower())})</div>
                    </div>
                </div>
                <table class="weekly-table">
                    <thead>
                        <tr>
                            <th>Nhân viên</th>
                            <th>Xu hướng</th>
                            <th>Avg {escape(a_label.lower())}</th>
                            <th>Avg {escape(b_label.lower())}</th>
                            <th>Δ điểm</th>
                            <th>Pass rate</th>
                            <th>Δ pass</th>
                            <th>Blacklist</th>
                            <th>Δ BL</th>
                            <th>Số ca</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(rows)}</tbody>
                </table>
            </div>
        </section>
    """


def _overview_from_query(qs: dict) -> dict:
    """Build department overview từ query string. Dùng cùng range với weekly analysis (period A)."""
    from datetime import date as _date

    preset = (qs.get("preset", ["this_vs_last_week"]) or ["this_vs_last_week"])[0]

    if preset == "custom":
        try:
            a_start = _date.fromisoformat(qs["from_a"][0])
            a_end = _date.fromisoformat(qs["to_a"][0])
            return build_department_overview(a_start, a_end)
        except (KeyError, ValueError, IndexError):
            return build_department_overview()

    if preset in PRESETS:
        a, _ = resolve_preset_ranges(preset)
        return build_department_overview(a[0], a[1])

    return build_department_overview()


def _render_dept_trend_sparkline(daily_trend: list[dict]) -> str:
    if len(daily_trend) < 2:
        return ""
    scores = [t["avg_score"] for t in daily_trend]
    min_s, max_s = min(scores), max(scores)
    rng = max_s - min_s or 1
    width, height = 260, 50
    pts = []
    for i, s in enumerate(scores):
        x = round(i / (len(scores) - 1) * width, 1)
        y = round(height - (s - min_s) / rng * (height - 6) - 3, 1)
        pts.append(f"{x},{y}")
    polyline = " ".join(pts)
    last = scores[-1]
    first = scores[0]
    color = "#0b7a4b" if last >= first else "#c73641"
    return (
        f'<svg width="{width}" height="{height}" class="dept-spark">'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>'
        f'</svg>'
    )


def _render_dept_overview(overview: dict) -> str:
    kpi = overview["kpi"]
    rt_label = format_seconds(kpi["avg_response_seconds"]) if kpi["avg_response_seconds"] is not None else "—"
    avg_score_color = "#0b7a4b" if kpi["team_avg_score"] >= 75 else ("#b97800" if kpi["team_avg_score"] >= 50 else "#c73641")
    pass_color = "#0b7a4b" if kpi["pass_rate"] >= 75 else ("#b97800" if kpi["pass_rate"] >= 50 else "#c73641")
    bl_color = "#c73641" if kpi["blacklist_count"] > 0 else "#0b7a4b"

    distribution_bars = []
    grade_color_map = {
        "Xuất sắc": "#0b7a4b",
        "Tốt": "#3eaa6f",
        "Trung bình": "#b97800",
        "Không đạt": "#c73641",
    }
    for d in overview["grade_distribution"]:
        color = grade_color_map.get(d["grade"], "#888")
        distribution_bars.append(
            f'<div class="dist-row">'
            f'<span class="dist-label">{escape(d["grade"])}</span>'
            f'<div class="dist-bar-track">'
            f'<div class="dist-bar-fill" style="width:{d["pct"]}%;background:{color}"></div>'
            f'</div>'
            f'<span class="dist-count">{d["count"]} ({d["pct"]}%)</span>'
            f'</div>'
        )

    top_html = "".join(
        f'<li class="perf-item top"><span class="perf-name">{escape(p["employee_name"])}</span>'
        f'<span class="perf-score">{p["avg_score"]}đ · {p["conversation_count"]} ca</span></li>'
        for p in overview["top_performers"]
    ) or '<li class="perf-empty">—</li>'

    bottom_html = "".join(
        f'<li class="perf-item bottom"><span class="perf-name">{escape(p["employee_name"])}</span>'
        f'<span class="perf-score">{p["avg_score"]}đ · BL:{p["blacklist_count"]}</span></li>'
        for p in overview["bottom_performers"]
    ) or '<li class="perf-empty">—</li>'

    sparkline_html = _render_dept_trend_sparkline(overview["daily_trend"])
    sparkline_section = (
        f'<div class="dept-trend-section">'
        f'<div class="dept-trend-label">Trend điểm theo ngày ({len(overview["daily_trend"])} ngày)</div>'
        f'{sparkline_html}'
        f'</div>'
    ) if sparkline_html else ""

    critical_count = len(overview["critical_employees"])
    critical_badge = (
        f'<span class="critical-badge">{critical_count} nhân viên cần can thiệp</span>'
        if critical_count else
        '<span class="critical-badge ok">✓ Không có ca nguy hiểm</span>'
    )

    return f"""
        <section class="dept-panel">
            <div class="dept-head">
                <div>
                    <div class="dept-title">Tổng quan phòng ban</div>
                    <div class="dept-period">{escape(overview["period"]["label"])}</div>
                </div>
                {critical_badge}
            </div>
            <div class="dept-kpi-grid">
                <div class="dept-kpi">
                    <div class="dept-kpi-label">Tổng hội thoại</div>
                    <div class="dept-kpi-value">{kpi["total_conversations"]}</div>
                </div>
                <div class="dept-kpi">
                    <div class="dept-kpi-label">Số nhân viên</div>
                    <div class="dept-kpi-value">{kpi["total_employees"]}</div>
                </div>
                <div class="dept-kpi">
                    <div class="dept-kpi-label">Điểm TB toàn team</div>
                    <div class="dept-kpi-value" style="color:{avg_score_color}">{kpi["team_avg_score"]}</div>
                </div>
                <div class="dept-kpi">
                    <div class="dept-kpi-label">Tỉ lệ đạt</div>
                    <div class="dept-kpi-value" style="color:{pass_color}">{kpi["pass_rate"]}%</div>
                </div>
                <div class="dept-kpi">
                    <div class="dept-kpi-label">Vi phạm Blacklist</div>
                    <div class="dept-kpi-value" style="color:{bl_color}">{kpi["blacklist_count"]}</div>
                </div>
                <div class="dept-kpi">
                    <div class="dept-kpi-label">Phản hồi TB</div>
                    <div class="dept-kpi-value" style="font-size:22px">{escape(rt_label)}</div>
                </div>
            </div>

            <div class="dept-detail-grid">
                <div class="dept-detail-card">
                    <div class="dept-detail-title">Phân phối điểm</div>
                    <div class="dist-list">{"".join(distribution_bars)}</div>
                </div>
                <div class="dept-detail-card">
                    <div class="dept-detail-title">🏆 Top 3 nhân viên</div>
                    <ul class="perf-list">{top_html}</ul>
                </div>
                <div class="dept-detail-card">
                    <div class="dept-detail-title">⚠ Cần hỗ trợ</div>
                    <ul class="perf-list">{bottom_html}</ul>
                </div>
            </div>

            {sparkline_section}
        </section>
    """


def _analysis_from_query(qs: dict) -> dict:
    """Build weekly analysis từ query string. Hỗ trợ preset hoặc custom dates."""
    from datetime import date as _date

    preset = (qs.get("preset", ["this_vs_last_week"]) or ["this_vs_last_week"])[0]
    a_label, b_label = PRESET_PERIOD_LABELS.get(preset, ("Kỳ A", "Kỳ B"))

    if preset == "custom":
        try:
            a_start = _date.fromisoformat(qs["from_a"][0])
            a_end = _date.fromisoformat(qs["to_a"][0])
        except (KeyError, ValueError, IndexError):
            return analyze_weekly_comparison()
        b_period = None
        if "from_b" in qs and "to_b" in qs:
            try:
                b_start = _date.fromisoformat(qs["from_b"][0])
                b_end = _date.fromisoformat(qs["to_b"][0])
                b_period = (b_start, b_end)
            except (ValueError, IndexError):
                b_period = None
        return analyze_period_comparison((a_start, a_end), b_period, a_label, b_label)

    if preset in PRESETS:
        a, b = resolve_preset_ranges(preset)
        return analyze_period_comparison(a, b, a_label, b_label)

    return analyze_weekly_comparison()


def _render_filter_pills(weekly: dict, current_preset: str, qs: dict) -> str:
    """Filter row kiểu pill như admin platform: hiển thị label + giá trị + dropdown."""
    a_label = weekly.get("period_a_label", "Kỳ này")
    a_range = weekly["this_week"]["label"]
    preset_label = PRESETS.get(current_preset, "Tùy chọn")

    a_start = weekly["this_week"]["start"] or ""
    a_end = weekly["this_week"]["end"] or ""
    b_start = weekly["last_week"]["start"] or ""
    b_end = weekly["last_week"]["end"] or ""

    preset_options = "".join(
        f'<option value="{escape(k)}"{" selected" if k == current_preset else ""}>{escape(v)}</option>'
        for k, v in PRESETS.items()
    )
    custom_show = "flex" if current_preset == "custom" else "none"

    return f"""
        <div class="filter-pills" id="filterPills">
            <form method="get" action="/dashboard" id="pillForm" class="pill-form">
                <button type="button" class="filter-pill" id="pillTrigger" onclick="togglePillPanel()">
                    <span class="pill-label">Khung Thời Gian</span>
                    <span class="pill-value">{escape(preset_label)} · {escape(a_range)}</span>
                    <span class="pill-caret">▾</span>
                </button>

                <div class="pill-panel" id="pillPanel">
                    <div class="pill-section">
                        <label class="pill-section-label">Chọn khoảng thời gian phân tích</label>
                        <select name="preset" id="tlPreset" onchange="onPresetChange()">
                            {preset_options}
                        </select>
                    </div>
                    <div class="pill-section pill-custom" id="tlCustom" style="display:{custom_show}">
                        <div class="tl-field">
                            <label>{escape(a_label)} — Từ</label>
                            <input type="date" name="from_a" value="{escape(a_start)}">
                        </div>
                        <div class="tl-field">
                            <label>{escape(a_label)} — Đến</label>
                            <input type="date" name="to_a" value="{escape(a_end)}">
                        </div>
                        <div class="tl-field">
                            <label>Kỳ B — Từ</label>
                            <input type="date" name="from_b" value="{escape(b_start)}">
                        </div>
                        <div class="tl-field">
                            <label>Kỳ B — Đến</label>
                            <input type="date" name="to_b" value="{escape(b_end)}">
                        </div>
                    </div>
                    <div class="pill-actions">
                        <button type="button" class="pill-cancel" onclick="closePillPanel()">Hủy</button>
                        <button type="submit" class="pill-apply">Áp dụng</button>
                    </div>
                </div>
            </form>
        </div>
    """


def _render_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return (
            '<section class="alert-panel alert-empty">'
            '<div class="alert-empty-icon">✓</div>'
            '<div class="alert-empty-title">Toàn team đang ổn định</div>'
            '<div class="alert-empty-sub">Hiện không có nhân viên nào vượt ngưỡng cảnh báo. '
            'Hệ thống sẽ tự flag khi có nhân viên vi phạm Blacklist nhiều lần hoặc điểm trung bình tụt dốc.</div>'
            '</section>'
        )
    items = []
    for a in alerts:
        level_class = "alert-critical" if a["level"] == "critical" else "alert-warning"
        icon = "🔴" if a["level"] == "critical" else "🟡"
        items.append(
            f'<div class="alert-item {level_class}">'
            f'<span class="alert-icon">{icon}</span>'
            f'<div class="alert-content">'
            f'<strong>{escape(a["message"])}</strong>'
            f'<div class="alert-detail">{escape(a["detail"])}</div>'
            f'</div>'
            f'</div>'
        )
    return (
        '<section class="alert-panel">'
        '<div class="alert-panel-head">⚠ Cảnh báo cần xử lý</div>'
        + "".join(items)
        + "</section>"
    )


def _build_filter_options(cases: list[dict]) -> tuple[str, str]:
    employees = sorted({
        (c["conversation"].employee.id, c["conversation"].employee.name)
        for c in cases
        if c["conversation"].employee
    }, key=lambda x: x[1])
    channels = sorted({c["conversation"].channel for c in cases})

    emp_opts = "".join(f'<option value="{escape(eid)}">{escape(name)}</option>' for eid, name in employees)
    ch_opts = "".join(f'<option value="{escape(ch)}">{escape(ch.title())}</option>' for ch in channels)
    return emp_opts, ch_opts


def render_dashboard(qs: dict | None = None) -> str:
    qs = qs or {}
    current_preset = (qs.get("preset", ["this_vs_last_week"]) or ["this_vs_last_week"])[0]
    weekly = _analysis_from_query(qs)
    dashboard = load_dashboard_data()
    case_html = "".join(render_case(item, dashboard["reviews"]) for item in dashboard["cases"])
    scorecards_html = render_scorecards(dashboard["scorecards"])
    top_training_html = render_top_training_employees(dashboard["top_training_employees"])
    weak_skills_html = render_top_weak_skills(dashboard["top_weak_skills"])
    training_html = render_training_recommendations(dashboard["training_recommendations"])
    emp_opts, ch_opts = _build_filter_options(dashboard["cases"])
    total_cases = len(dashboard["cases"])
    alerts_html = _render_alerts(dashboard["alerts"])
    filter_bar_pill_html = _render_filter_pills(weekly, current_preset, qs)
    weekly_html = _render_weekly_panel(weekly)
    overview = _overview_from_query(qs)
    overview_html = _render_dept_overview(overview)
    ruleset_version = escape(dashboard["ruleset_version"] or "unknown")
    alert_count = len(dashboard["alerts"])
    alert_count_badge = (
        f'<span class="tab-badge">{alert_count}</span>' if alert_count else ""
    )
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
            --accent: #ee4d2d;
            --accent-hover: #d04420;
            --accent-soft: rgba(238, 77, 45, 0.08);
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
                radial-gradient(circle at top left, rgba(238, 77, 45, 0.10), transparent 20%),
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
            text-decoration: none;
        }}
        .export-btn {{
            background: rgba(238, 77, 45, 0.08);
            border-color: var(--accent);
            color: var(--accent);
            font-weight: 700;
        }}
        .export-btn:hover {{ background: rgba(238, 77, 45, 0.10); }}
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
            background: linear-gradient(135deg, #fff5f1, #fffaf8);
        }}
        .panel-body {{
            padding: 18px 20px;
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
            background: linear-gradient(135deg, #ffe7df, #fff5f1);
            border-top: 1px solid rgba(238, 77, 45, 0.08);
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
            color: var(--accent);
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
            color: var(--accent);
            font-size: 13px;
            font-weight: 700;
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
        /* Topbar */
        .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 0;
            margin-bottom: 8px;
            border-bottom: 1px solid var(--line);
        }}
        .topbar-brand strong {{ font-size: 20px; color: var(--ink); }}
        .topbar-sub {{ font-size: 12px; color: var(--muted); margin-left: 10px; }}
        .topbar-actions {{ display: flex; gap: 8px; }}
        .topbar-btn {{
            padding: 8px 14px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #fff;
            color: var(--ink);
            text-decoration: none;
            font-size: 13px;
            font-weight: 600;
            transition: all 0.15s;
        }}
        .topbar-btn:hover {{ background: #f0f3f7; border-color: var(--accent); color: var(--accent); }}

        /* Tab navigation */
        .tab-nav {{
            display: flex;
            gap: 0;
            border-bottom: 1px solid var(--line);
            margin-bottom: 18px;
            overflow-x: auto;
        }}
        .tab-link {{
            position: relative;
            padding: 14px 22px;
            background: none;
            border: none;
            font-size: 15px;
            font-weight: 700;
            color: #4f5968;
            cursor: pointer;
            white-space: nowrap;
            transition: color 0.15s;
        }}
        .tab-link:hover {{ color: var(--ink); }}
        .tab-link.active {{ color: #ee4d2d; }}
        .tab-link.active::after {{
            content: '';
            position: absolute;
            left: 18px;
            right: 18px;
            bottom: -1px;
            height: 3px;
            background: #ee4d2d;
            border-radius: 3px 3px 0 0;
        }}
        .tab-badge {{
            display: inline-block;
            background: #c73641;
            color: #fff;
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 11px;
            font-weight: 700;
            margin-left: 4px;
        }}
        .tab-pane {{ display: none; }}
        .tab-pane.active {{ display: block; }}

        /* Filter pills */
        .filter-pills {{
            display: flex;
            gap: 12px;
            margin-bottom: 22px;
            position: relative;
            flex-wrap: wrap;
        }}
        .pill-form {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 0; position: relative; width: 100%; }}
        .filter-pill {{
            display: flex;
            align-items: center;
            gap: 12px;
            background: #fff;
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 10px 16px;
            font-size: 14px;
            cursor: pointer;
            transition: border-color 0.15s, box-shadow 0.15s;
            font-family: inherit;
            text-align: left;
        }}
        .filter-pill:hover {{ border-color: #ee4d2d; box-shadow: 0 2px 8px rgba(238, 77, 45, 0.08); }}
        .filter-pill.static {{ cursor: default; }}
        .filter-pill.static:hover {{ border-color: var(--line); box-shadow: none; }}
        .pill-label {{
            color: var(--muted);
            font-size: 13px;
            font-weight: 600;
            white-space: nowrap;
        }}
        .pill-value {{
            color: var(--ink);
            font-weight: 600;
            font-size: 14px;
        }}
        .pill-caret {{ color: var(--muted); font-size: 11px; }}
        .pill-panel {{
            position: absolute;
            top: 100%;
            left: 0;
            margin-top: 8px;
            width: 640px;
            max-width: 95vw;
            background: #fff;
            border: 1px solid var(--line);
            border-radius: 12px;
            box-shadow: 0 12px 32px rgba(34, 41, 52, 0.12);
            padding: 18px;
            z-index: 100;
            display: none;
        }}
        .pill-panel.open {{ display: block; }}
        .pill-section {{ margin-bottom: 14px; }}
        .pill-section-label {{
            display: block;
            font-size: 12px;
            font-weight: 700;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 8px;
        }}
        .pill-section select {{
            width: 100%;
            height: 38px;
            padding: 0 12px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #f8fafc;
            font-size: 14px;
            cursor: pointer;
        }}
        .pill-custom {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .pill-actions {{
            display: flex;
            gap: 10px;
            justify-content: flex-end;
            border-top: 1px solid #edf1f6;
            padding-top: 14px;
            margin-top: 14px;
        }}
        .pill-cancel, .pill-apply {{
            padding: 8px 18px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            border: none;
            font-family: inherit;
        }}
        .pill-cancel {{ background: #edf1f6; color: var(--ink); }}
        .pill-apply {{ background: #ee4d2d; color: #fff; }}
        .pill-apply:hover {{ background: #d04420; }}

        /* Department overview */
        .dept-panel {{
            background: linear-gradient(135deg, #fff8f0 0%, #fff 60%, #fff5f1 100%);
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 22px 26px;
            margin-bottom: 22px;
            box-shadow: var(--shadow);
        }}
        .dept-head {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 18px;
        }}
        .dept-title {{ font-size: 22px; font-weight: 800; color: var(--ink); }}
        .dept-period {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}
        .critical-badge {{
            background: #fde4e6;
            color: #c73641;
            border-radius: 999px;
            padding: 8px 14px;
            font-size: 13px;
            font-weight: 700;
        }}
        .critical-badge.ok {{ background: #dff5e7; color: #0b7a4b; }}
        .dept-kpi-grid {{
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }}
        .dept-kpi {{
            background: var(--panel);
            border: 1px solid #e6ebf2;
            border-radius: 14px;
            padding: 14px 16px;
            text-align: center;
        }}
        .dept-kpi-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; font-weight: 700; }}
        .dept-kpi-value {{ font-size: 30px; font-weight: 800; color: var(--ink); line-height: 1; }}
        .dept-detail-grid {{
            display: grid;
            grid-template-columns: 1.4fr 1fr 1fr;
            gap: 14px;
            margin-bottom: 14px;
        }}
        .dept-detail-card {{
            background: var(--panel);
            border: 1px solid #e6ebf2;
            border-radius: 14px;
            padding: 14px 16px;
        }}
        .dept-detail-title {{ font-size: 13px; font-weight: 800; color: var(--ink); margin-bottom: 12px; }}
        .dist-list {{ display: grid; gap: 8px; }}
        .dist-row {{ display: grid; grid-template-columns: 100px 1fr 90px; align-items: center; gap: 10px; font-size: 13px; }}
        .dist-label {{ color: #354050; font-weight: 600; }}
        .dist-bar-track {{ background: #eef2f7; border-radius: 999px; height: 12px; overflow: hidden; }}
        .dist-bar-fill {{ height: 100%; border-radius: 999px; transition: width 0.3s; }}
        .dist-count {{ color: var(--muted); font-size: 12px; text-align: right; }}
        .perf-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }}
        .perf-item {{
            display: flex; justify-content: space-between; gap: 8px;
            padding: 7px 10px; border-radius: 8px;
            font-size: 13px;
        }}
        .perf-item.top {{ background: #e8f7ee; }}
        .perf-item.bottom {{ background: #fde9eb; }}
        .perf-name {{ font-weight: 700; color: var(--ink); }}
        .perf-score {{ color: var(--muted); font-size: 12px; }}
        .perf-empty {{ color: var(--muted); font-style: italic; padding: 6px; list-style: none; }}
        .dept-trend-section {{
            background: var(--panel);
            border: 1px solid #e6ebf2;
            border-radius: 14px;
            padding: 14px 18px;
            display: flex;
            align-items: center;
            gap: 18px;
        }}
        .dept-trend-label {{ font-size: 13px; font-weight: 700; color: var(--muted); white-space: nowrap; }}
        .dept-spark {{ display: block; }}
        @media (max-width: 1080px) {{
            .dept-kpi-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
            .dept-detail-grid {{ grid-template-columns: 1fr; }}
        }}
        @media (max-width: 640px) {{
            .dept-kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            .dept-kpi-value {{ font-size: 24px; }}
        }}
        /* Date field (used inside filter-pill custom panel) */
        .tl-field {{ display: flex; flex-direction: column; gap: 4px; }}
        .tl-field label {{ font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; }}
        .tl-field input {{
            height: 34px;
            padding: 0 10px;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #f8fafc;
            font-size: 13px;
        }}
        /* Weekly comparison panel */
        .weekly-panel .panel-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            background: linear-gradient(135deg, #e8f0fb, #f5f9ff);
        }}
        .weekly-meta {{ font-size: 13px; color: var(--muted); }}
        .team-summary {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }}
        .team-card {{
            background: #f8fafc;
            border: 1px solid #e6ebf2;
            border-radius: 14px;
            padding: 14px 16px;
        }}
        .team-card-label {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
        .team-card-value {{ font-size: 26px; font-weight: 800; color: var(--ink); }}
        .team-card-sub {{ font-size: 12px; color: var(--muted); margin-top: 6px; }}
        .weekly-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        .weekly-table th {{
            text-align: left;
            padding: 10px 8px;
            border-bottom: 1px solid var(--line);
            color: var(--muted);
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.06em;
        }}
        .weekly-table td {{
            padding: 12px 8px;
            border-bottom: 1px solid #edf1f6;
            vertical-align: middle;
        }}
        .weekly-table tr:last-child td {{ border-bottom: none; }}
        .verdict-tag {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
            white-space: nowrap;
        }}
        .verdict-tag.verdict-up {{ background: #dff5e7; color: #0b7a4b; }}
        .verdict-tag.verdict-down {{ background: #fde4e6; color: #c73641; }}
        .verdict-tag.verdict-flat {{ background: #eef2f7; color: #4f5968; }}
        .verdict-tag.verdict-new {{ background: #e8efff; color: #1d4ed8; }}
        .delta-pill {{
            display: inline-block;
            padding: 3px 9px;
            border-radius: 8px;
            font-size: 12px;
            font-weight: 700;
            white-space: nowrap;
        }}
        .delta-pill.up {{ background: #e8f7ee; color: #0b7a4b; }}
        .delta-pill.down {{ background: #fde9eb; color: #c73641; }}
        .delta-pill.flat {{ background: #eef2f7; color: #6b7280; }}
        .delta-pill.new {{ background: #e8efff; color: #1d4ed8; }}
        @media (max-width: 920px) {{
            .team-summary {{ grid-template-columns: 1fr; }}
            .weekly-table {{ font-size: 12px; }}
            .weekly-table th, .weekly-table td {{ padding: 8px 4px; }}
        }}
        /* Alert panel */
        .alert-panel {{
            background: #fff8f0;
            border: 1px solid #f5c07a;
            border-radius: 16px;
            padding: 16px 20px;
            margin-bottom: 18px;
        }}
        .alert-panel-head {{
            font-size: 15px;
            font-weight: 800;
            color: #8a4700;
            margin-bottom: 12px;
        }}
        .alert-item {{
            display: flex;
            gap: 12px;
            padding: 10px 0;
            border-bottom: 1px solid #f0dfc0;
        }}
        .alert-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .alert-icon {{ font-size: 18px; flex: 0 0 22px; margin-top: 1px; }}
        .alert-content {{ display: grid; gap: 3px; }}
        .alert-detail {{ font-size: 12px; color: #7a5200; }}
        .alert-critical {{ --alert-accent: #c73641; }}
        .alert-critical .alert-content strong {{ color: #c73641; }}
        .alert-panel.alert-empty {{
            background: #eef9f2;
            border-color: #b3dec5;
            text-align: center;
            padding: 40px 20px;
        }}
        .alert-empty-icon {{
            width: 56px; height: 56px; margin: 0 auto 14px;
            border-radius: 50%; background: #0b7a4b; color: #fff;
            display: flex; align-items: center; justify-content: center;
            font-size: 28px; font-weight: 800;
        }}
        .alert-empty-title {{ font-size: 18px; font-weight: 800; color: #0b7a4b; margin-bottom: 6px; }}
        .alert-empty-sub {{ font-size: 13px; color: #4f7a64; max-width: 520px; margin: 0 auto; line-height: 1.5; }}
        /* Filter bar */
        .filter-bar {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 14px 18px;
            margin-bottom: 18px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: flex-end;
        }}
        .filter-group {{ display: flex; flex-direction: column; gap: 4px; }}
        .filter-label {{ font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }}
        .filter-bar select, .filter-bar input[type=text], .filter-bar input[type=date] {{
            height: 36px;
            padding: 0 10px;
            border: 1px solid var(--line);
            border-radius: 8px;
            font-size: 14px;
            background: #f8fafc;
            color: var(--ink);
            outline: none;
            cursor: pointer;
        }}
        .filter-bar select:focus, .filter-bar input:focus {{ border-color: var(--accent); }}
        .filter-bar input[type=text] {{ min-width: 200px; }}
        .filter-btn {{
            height: 36px;
            padding: 0 16px;
            border: none;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            background: var(--accent);
            color: #fff;
        }}
        .filter-btn.reset {{ background: #edf1f6; color: var(--ink); }}
        .filter-count {{ font-size: 13px; color: var(--muted); align-self: center; margin-left: auto; }}
        .hidden {{ display: none !important; }}
        .trend-row {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid #edf1f6;
        }}
        .trend-label {{ font-size: 13px; color: var(--muted); white-space: nowrap; }}
        .sparkline-wrap {{ display: flex; align-items: center; gap: 8px; }}
        .sparkline {{ display: block; }}
        .trend-arrow {{ font-size: 13px; font-weight: 700; }}
        .trend-na {{ font-size: 12px; color: var(--muted); font-style: italic; }}
        .rt-row {{ margin-top: 8px; font-size: 14px; color: #354050; }}
        .rt-label {{ color: var(--muted); }}
        .rt-sep {{ color: #c0c6ce; }}
        /* Review panel */
        .review-panel {{
            margin-top: 14px;
            padding: 12px 14px;
            border-radius: 10px;
            background: #f0f3f7;
            border: 1px dashed #c8cfd8;
        }}
        .review-panel.reviewed {{ background: #eef8f0; border-color: #a8ddb7; border-style: solid; }}
        .reviewed-badge {{ background: #0b7a4b; color: #fff; border-radius: 999px; padding: 3px 10px; font-size: 12px; font-weight: 700; }}
        .review-meta {{ font-size: 12px; color: var(--muted); }}
        .review-note-text {{ margin-top: 6px; font-size: 14px; color: #293241; font-style: italic; }}
        .review-open-btn, .review-edit-btn {{
            border: 1px solid var(--accent);
            background: transparent;
            color: var(--accent);
            border-radius: 8px;
            padding: 5px 12px;
            font-size: 13px;
            cursor: pointer;
            font-weight: 600;
        }}
        .review-edit-btn {{ margin-top: 8px; }}
        /* Modal */
        .modal-overlay {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(34,41,52,0.5);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }}
        .modal-overlay.open {{ display: flex; }}
        .modal-box {{
            background: var(--panel);
            border-radius: 18px;
            padding: 28px;
            width: 480px;
            max-width: 95vw;
            box-shadow: 0 24px 64px rgba(34,41,52,0.2);
        }}
        .modal-title {{ font-size: 18px; font-weight: 800; margin: 0 0 18px; }}
        .modal-field {{ margin-bottom: 14px; }}
        .modal-field label {{ display: block; font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase; margin-bottom: 5px; }}
        .modal-field input, .modal-field textarea {{
            width: 100%; padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px;
            font-size: 14px; color: var(--ink); background: #f8fafc; box-sizing: border-box;
        }}
        .modal-field textarea {{ min-height: 90px; resize: vertical; }}
        .modal-actions {{ display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; }}
        .modal-save {{ background: var(--accent); color: #fff; border: none; border-radius: 8px; padding: 9px 20px; font-size: 14px; font-weight: 700; cursor: pointer; }}
        .modal-cancel {{ background: #edf1f6; color: var(--ink); border: none; border-radius: 8px; padding: 9px 16px; font-size: 14px; cursor: pointer; }}
    </style>
</head>
<body>
    <div class="wrap">
        <header class="topbar">
            <div class="topbar-brand">
                <strong>Message QA</strong>
                <span class="topbar-sub">Local Quality Dashboard · Rules v{ruleset_version}</span>
            </div>
            <div class="topbar-actions">
                <button class="topbar-btn" onclick="snapshotNow()">📸 Chấm & Lưu</button>
                <a href="/api/export/csv" class="topbar-btn" download>⬇ Export kết quả</a>
                <a href="/api/export/scorecard-csv" class="topbar-btn" download>⬇ Export Scorecard</a>
            </div>
        </header>

        <nav class="tab-nav" id="tabNav">
            <button class="tab-link active" data-tab="overview">Tổng quan</button>
            <button class="tab-link" data-tab="weekly">Phân tích nhân sự</button>
            <button class="tab-link" data-tab="alerts">Cảnh báo {alert_count_badge}</button>
            <button class="tab-link" data-tab="training">Training & Coaching</button>
            <button class="tab-link" data-tab="cases">Hội thoại chi tiết</button>
        </nav>

        {filter_bar_pill_html}

        <div class="tab-pane active" data-pane="overview">
            {overview_html}
        </div>

        <div class="tab-pane" data-pane="weekly">
            {weekly_html}
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
        </div>

        <div class="tab-pane" data-pane="alerts">
            {alerts_html}
        </div>

        <div class="tab-pane" data-pane="training">
            <section class="panel" style="margin-bottom: 22px;">
                <div class="panel-head"><h2>Training Recommendations</h2></div>
                <div class="panel-body training-list">{training_html}</div>
            </section>
        </div>

        <div class="tab-pane" data-pane="cases">
        <div class="filter-bar" id="filterBar">
            <div class="filter-group">
                <span class="filter-label">Nhân viên</span>
                <select id="fEmployee">
                    <option value="">Tất cả</option>
                    {emp_opts}
                </select>
            </div>
            <div class="filter-group">
                <span class="filter-label">Xếp loại</span>
                <select id="fGrade">
                    <option value="">Tất cả</option>
                    <option value="Xuất sắc">Xuất sắc</option>
                    <option value="Tốt">Tốt</option>
                    <option value="Trung bình">Trung bình</option>
                    <option value="Không đạt">Không đạt</option>
                </select>
            </div>
            <div class="filter-group">
                <span class="filter-label">Blacklist</span>
                <select id="fBlacklist">
                    <option value="">Tất cả</option>
                    <option value="yes">Có vi phạm</option>
                    <option value="no">Không vi phạm</option>
                </select>
            </div>
            <div class="filter-group">
                <span class="filter-label">Kênh</span>
                <select id="fChannel">
                    <option value="">Tất cả</option>
                    {ch_opts}
                </select>
            </div>
            <div class="filter-group">
                <span class="filter-label">Từ ngày</span>
                <input type="date" id="fDateFrom">
            </div>
            <div class="filter-group">
                <span class="filter-label">Đến ngày</span>
                <input type="date" id="fDateTo">
            </div>
            <div class="filter-group">
                <span class="filter-label">Tìm kiếm</span>
                <input type="text" id="fSearch" placeholder="ID hội thoại, tên nhân viên...">
            </div>
            <button class="filter-btn reset" onclick="resetFilters()">Xóa bộ lọc</button>
            <span class="filter-count" id="filterCount">Hiển thị {total_cases}/{total_cases} hội thoại</span>
        </div>

        <section class="case-list" id="caseList">
            {case_html}
        </section>
        </div>
    </div>
<!-- Review Modal -->
<div class="modal-overlay" id="reviewModal">
  <div class="modal-box">
    <div class="modal-title">Ghi chú Review</div>
    <input type="hidden" id="modalConvId">
    <div class="modal-field">
      <label>Conversation ID</label>
      <input type="text" id="modalConvDisplay" readonly>
    </div>
    <div class="modal-field">
      <label>Người review</label>
      <input type="text" id="modalReviewer" placeholder="Tên QA leader" value="QA Leader">
    </div>
    <div class="modal-field">
      <label>Ghi chú</label>
      <textarea id="modalNote" placeholder="Nhận xét, ghi chú về hội thoại..."></textarea>
    </div>
    <div class="modal-field">
      <label>Override điểm (để trống = giữ nguyên)</label>
      <input type="number" id="modalOverride" min="0" max="100" step="0.5" placeholder="0 - 100">
    </div>
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeReviewModal()">Hủy</button>
      <button class="modal-save" onclick="submitReview()">Lưu review</button>
    </div>
  </div>
</div>
<script>
(function() {{
    var totalCases = {total_cases};
    var inputs = ['fEmployee','fGrade','fBlacklist','fChannel','fDateFrom','fDateTo','fSearch'];
    inputs.forEach(function(id) {{
        var el = document.getElementById(id);
        if (el) el.addEventListener('input', applyFilters);
    }});

    function applyFilters() {{
        var emp = document.getElementById('fEmployee').value;
        var grade = document.getElementById('fGrade').value;
        var bl = document.getElementById('fBlacklist').value;
        var ch = document.getElementById('fChannel').value;
        var dateFrom = document.getElementById('fDateFrom').value;
        var dateTo = document.getElementById('fDateTo').value;
        var search = document.getElementById('fSearch').value.toLowerCase().trim();

        var cases = document.querySelectorAll('#caseList .case-thread');
        var shown = 0;
        cases.forEach(function(el) {{
            var d = el.dataset;
            var ok = true;
            if (emp && d.employeeId !== emp) ok = false;
            if (grade && d.grade !== grade) ok = false;
            if (bl && d.blacklist !== bl) ok = false;
            if (ch && d.channel !== ch) ok = false;
            if (dateFrom && d.date && d.date < dateFrom) ok = false;
            if (dateTo && d.date && d.date > dateTo) ok = false;
            if (search) {{
                var haystack = (d.convId || '') + ' ' + (d.employee || '');
                if (haystack.indexOf(search) === -1) ok = false;
            }}
            el.classList.toggle('hidden', !ok);
            if (ok) shown++;
        }});
        document.getElementById('filterCount').textContent =
            'Hiển thị ' + shown + '/' + totalCases + ' hội thoại';
    }}

    window.resetFilters = function() {{
        inputs.forEach(function(id) {{
            var el = document.getElementById(id);
            if (el) el.value = '';
        }});
        applyFilters();
    }};
}})();

window.openReviewForm = function(convId) {{
    document.getElementById('modalConvId').value = convId;
    document.getElementById('modalConvDisplay').value = convId;
    document.getElementById('modalNote').value = '';
    document.getElementById('modalOverride').value = '';
    document.getElementById('reviewModal').classList.add('open');
}};

window.closeReviewModal = function() {{
    document.getElementById('reviewModal').classList.remove('open');
}};

window.submitReview = function() {{
    var convId = document.getElementById('modalConvId').value;
    var reviewer = document.getElementById('modalReviewer').value.trim() || 'QA Leader';
    var note = document.getElementById('modalNote').value.trim();
    var override = document.getElementById('modalOverride').value;
    var payload = {{
        conversation_id: convId,
        reviewer: reviewer,
        note: note,
        score_override: override !== '' ? parseFloat(override) : null
    }};
    fetch('/api/review', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
    }}).then(function(r) {{
        if (r.ok) {{
            closeReviewModal();
            location.reload();
        }} else {{
            alert('Lỗi khi lưu review.');
        }}
    }});
}};

document.getElementById('reviewModal').addEventListener('click', function(e) {{
    if (e.target === this) closeReviewModal();
}});

window.onPresetChange = function() {{
    var preset = document.getElementById('tlPreset').value;
    var custom = document.getElementById('tlCustom');
    if (custom) custom.style.display = (preset === 'custom') ? 'flex' : 'none';
}};

window.snapshotNow = function() {{
    if (!confirm('Chấm điểm tất cả hội thoại hiện tại và lưu vào lịch sử hôm nay?')) return;
    fetch('/api/snapshot', {{method: 'POST'}}).then(function(r) {{
        return r.json();
    }}).then(function(d) {{
        alert('Đã chấm ' + d.evaluated + ' hội thoại. Lưu tại: ' + d.saved_path);
        location.reload();
    }}).catch(function(e) {{
        alert('Lỗi snapshot: ' + e);
    }});
}};

window.togglePillPanel = function() {{
    document.getElementById('pillPanel').classList.toggle('open');
}};

window.closePillPanel = function() {{
    document.getElementById('pillPanel').classList.remove('open');
}};

document.addEventListener('click', function(e) {{
    var panel = document.getElementById('pillPanel');
    var trigger = document.getElementById('pillTrigger');
    if (panel && trigger && !panel.contains(e.target) && !trigger.contains(e.target)) {{
        panel.classList.remove('open');
    }}
}});

(function() {{
    var TABS_USING_TIMELINE = ['overview', 'weekly'];
    var tabs = document.querySelectorAll('.tab-link');
    var panes = document.querySelectorAll('.tab-pane');
    var filterPills = document.getElementById('filterPills');

    function setActiveTab(target) {{
        tabs.forEach(function(t) {{ t.classList.remove('active'); }});
        panes.forEach(function(p) {{ p.classList.remove('active'); }});
        var tab = document.querySelector('.tab-link[data-tab="' + target + '"]');
        var pane = document.querySelector('[data-pane="' + target + '"]');
        if (tab) tab.classList.add('active');
        if (pane) pane.classList.add('active');
        if (filterPills) {{
            var show = TABS_USING_TIMELINE.indexOf(target) !== -1;
            filterPills.style.display = show ? '' : 'none';
        }}
    }}

    tabs.forEach(function(tab) {{
        tab.addEventListener('click', function() {{
            var target = tab.getAttribute('data-tab');
            setActiveTab(target);
            try {{
                var url = new URL(window.location);
                url.searchParams.set('tab', target);
                window.history.replaceState({{}}, '', url);
            }} catch(e) {{}}
        }});
    }});

    try {{
        var params = new URLSearchParams(window.location.search);
        var initialTab = params.get('tab') || 'overview';
        setActiveTab(initialTab);
    }} catch(e) {{}}
}})();
</script>
</body>
</html>"""


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "MessageQALocalViewer/0.2"

    def do_GET(self) -> None:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in {"/", "/dashboard"}:
            self.send_html(render_dashboard(qs))
            return
        if path == "/api/results":
            dashboard = load_dashboard_data()
            payload = {
                "evaluation_results": dashboard["evaluation_results"],
                "scorecards": dashboard["scorecards"],
                "top_weak_skills": dashboard["top_weak_skills"],
                "training_recommendations": dashboard["training_recommendations"],
            }
            self.send_json(payload)
            return
        if path == "/api/export/csv":
            dashboard = load_dashboard_data()
            conversations = load_conversations(CONVERSATIONS_PATH)
            csv_content = export_results_csv(dashboard["evaluation_results"], conversations)
            self.send_csv(csv_content, "qa_results.csv")
            return
        if path == "/api/export/scorecard-csv":
            dashboard = load_dashboard_data()
            csv_content = export_scorecard_csv(dashboard["scorecards"])
            self.send_csv(csv_content, "qa_scorecard.csv")
            return
        if path == "/api/weekly-analysis":
            self.send_json(_analysis_from_query(qs))
            return
        if path == "/api/department-overview":
            self.send_json(_overview_from_query(qs))
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/snapshot":
            ruleset = load_ruleset(RULESET_PATH)
            conversations = load_conversations(CONVERSATIONS_PATH)
            evaluation_results = [
                evaluate_conversation(c, ruleset).to_dict() for c in conversations
            ]
            saved_path = save_evaluation_results(evaluation_results, conversations)
            self.send_json({
                "ok": True,
                "saved_path": str(saved_path.relative_to(BASE_DIR)),
                "evaluated": len(evaluation_results),
            })
            return
        if self.path == "/api/review":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
                conv_id = str(data.get("conversation_id", "")).strip()
                if not conv_id:
                    raise ValueError("missing conversation_id")
                save_review(
                    conversation_id=conv_id,
                    reviewer=str(data.get("reviewer", "QA")).strip(),
                    note=str(data.get("note", "")).strip(),
                    score_override=float(data["score_override"]) if data.get("score_override") is not None else None,
                )
                self.send_json({"ok": True})
            except (ValueError, KeyError) as exc:
                body_err = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.BAD_REQUEST)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_err)))
                self.end_headers()
                self.wfile.write(body_err)
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

    def send_csv(self, content: str, filename: str) -> None:
        body = content.encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), ViewerHandler)
    print(f"Viewer running at http://{host}:{port}/dashboard")
    server.serve_forever()


if __name__ == "__main__":
    run()
