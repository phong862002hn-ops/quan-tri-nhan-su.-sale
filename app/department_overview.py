"""
Department overview — KPI tổng phòng ban cho cấp quản lý.
Tính các chỉ số tổng hợp trong một khoảng thời gian bất kỳ.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from app.history import load_all_history


GRADE_TIERS = ["Xuất sắc", "Tốt", "Trung bình", "Không đạt"]


def _filter_history(
    history: list[dict[str, Any]],
    start: date | None,
    end: date | None,
) -> list[dict[str, Any]]:
    if not start and not end:
        return history
    out = []
    for entry in history:
        try:
            ed = date.fromisoformat(entry.get("date", ""))
        except ValueError:
            continue
        if start and ed < start:
            continue
        if end and ed > end:
            continue
        out.append(entry)
    return out


def build_department_overview(
    period_start: date | None = None,
    period_end: date | None = None,
) -> dict[str, Any]:
    """
    Trả về KPI tổng cho phòng ban trong khoảng [period_start, period_end].
    Nếu None thì tính trên toàn bộ lịch sử.
    """
    history = _filter_history(load_all_history(), period_start, period_end)

    if not history:
        return _empty_overview(period_start, period_end)

    # ── Tổng hợp ─────────────────────────────────────────────────────────────
    by_employee: dict[str, list[dict[str, Any]]] = defaultdict(list)
    employee_names: dict[str, str] = {}
    by_date: dict[str, list[float]] = defaultdict(list)
    grade_counts: dict[str, int] = defaultdict(int)
    all_scores: list[float] = []
    total_blacklist = 0
    total_passed = 0
    response_times: list[float] = []

    for entry in history:
        emp = entry.get("employee")
        if not emp:
            continue
        eid = emp["id"]
        employee_names[eid] = emp.get("name", eid)
        by_employee[eid].append(entry)
        ev = entry["evaluation"]
        score = float(ev["total_score"])
        all_scores.append(score)
        by_date[entry["date"]].append(score)
        grade_counts[ev["grade"]] += 1
        if ev.get("blacklist_triggered"):
            total_blacklist += 1
        if ev.get("passed"):
            total_passed += 1
        rt = ev.get("response_time", {}).get("avg_response_seconds")
        if rt is not None:
            response_times.append(float(rt))

    total_conversations = len(all_scores)
    total_employees = len(by_employee)
    team_avg = round(sum(all_scores) / total_conversations, 2) if total_conversations else 0.0
    pass_rate = round(total_passed / total_conversations * 100, 1) if total_conversations else 0.0
    avg_response = round(sum(response_times) / len(response_times), 1) if response_times else None

    # ── Phân phối điểm theo grade ────────────────────────────────────────────
    distribution = []
    for tier in GRADE_TIERS:
        count = grade_counts.get(tier, 0)
        pct = round(count / total_conversations * 100, 1) if total_conversations else 0.0
        distribution.append({"grade": tier, "count": count, "pct": pct})

    # ── Best & worst performer ───────────────────────────────────────────────
    employee_avgs = []
    for eid, entries in by_employee.items():
        scores = [float(e["evaluation"]["total_score"]) for e in entries]
        emp_avg = round(sum(scores) / len(scores), 2)
        emp_blacklist = sum(1 for e in entries if e["evaluation"].get("blacklist_triggered"))
        employee_avgs.append({
            "employee_id": eid,
            "employee_name": employee_names[eid],
            "avg_score": emp_avg,
            "conversation_count": len(entries),
            "blacklist_count": emp_blacklist,
        })
    employee_avgs.sort(key=lambda x: x["avg_score"], reverse=True)
    top_3 = employee_avgs[:3]
    bottom_3 = sorted(employee_avgs, key=lambda x: x["avg_score"])[:3]

    # ── Trend theo ngày ──────────────────────────────────────────────────────
    daily_trend = [
        {
            "date": d,
            "avg_score": round(sum(scores) / len(scores), 2),
            "count": len(scores),
        }
        for d, scores in sorted(by_date.items())
    ]

    # ── Critical employees ───────────────────────────────────────────────────
    critical = [
        emp for emp in employee_avgs
        if emp["blacklist_count"] > 0 or emp["avg_score"] < 50
    ]

    return {
        "period": {
            "start": period_start.isoformat() if period_start else None,
            "end": period_end.isoformat() if period_end else None,
            "label": _period_label(period_start, period_end, daily_trend),
        },
        "kpi": {
            "total_conversations": total_conversations,
            "total_employees": total_employees,
            "team_avg_score": team_avg,
            "pass_rate": pass_rate,
            "blacklist_count": total_blacklist,
            "avg_response_seconds": avg_response,
        },
        "grade_distribution": distribution,
        "top_performers": top_3,
        "bottom_performers": bottom_3,
        "critical_employees": critical,
        "daily_trend": daily_trend,
    }


def _empty_overview(start: date | None, end: date | None) -> dict[str, Any]:
    return {
        "period": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "label": "Chưa có dữ liệu",
        },
        "kpi": {
            "total_conversations": 0,
            "total_employees": 0,
            "team_avg_score": 0.0,
            "pass_rate": 0.0,
            "blacklist_count": 0,
            "avg_response_seconds": None,
        },
        "grade_distribution": [{"grade": t, "count": 0, "pct": 0.0} for t in GRADE_TIERS],
        "top_performers": [],
        "bottom_performers": [],
        "critical_employees": [],
        "daily_trend": [],
    }


def _period_label(
    start: date | None,
    end: date | None,
    daily_trend: list[dict[str, Any]],
) -> str:
    if start and end:
        return f"{start.isoformat()} → {end.isoformat()}"
    if daily_trend:
        return f"{daily_trend[0]['date']} → {daily_trend[-1]['date']} (toàn bộ lịch sử)"
    return "Toàn bộ lịch sử"
