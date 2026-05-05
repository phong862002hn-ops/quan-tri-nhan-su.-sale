"""
Weekly analysis — so sánh hiệu suất nhân sự giữa các tuần.

Tuần được tính theo ISO week (thứ 2 → chủ nhật).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from app.history import load_all_history


def _iso_week_range(reference: date) -> tuple[date, date]:
    """Trả về (monday, sunday) của tuần chứa reference."""
    monday = reference - timedelta(days=reference.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _week_label(monday: date, sunday: date) -> str:
    return f"{monday.isoformat()} → {sunday.isoformat()}"


def _aggregate_week(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Tính các metric cho một list entries của 1 nhân viên trong 1 tuần."""
    if not entries:
        return {
            "conversation_count": 0,
            "avg_score": None,
            "passed_rate": None,
            "blacklist_count": 0,
            "avg_response_seconds": None,
        }
    scores = [float(e["evaluation"]["total_score"]) for e in entries]
    passed = sum(1 for e in entries if e["evaluation"].get("passed"))
    blacklist = sum(1 for e in entries if e["evaluation"].get("blacklist_triggered"))
    rt_values = [
        e["evaluation"].get("response_time", {}).get("avg_response_seconds")
        for e in entries
    ]
    rt_clean = [v for v in rt_values if v is not None]
    return {
        "conversation_count": len(entries),
        "avg_score": round(sum(scores) / len(scores), 2),
        "passed_rate": round(passed / len(entries) * 100, 1),
        "blacklist_count": blacklist,
        "avg_response_seconds": round(sum(rt_clean) / len(rt_clean), 1) if rt_clean else None,
    }


def _delta(current: float | None, previous: float | None) -> dict[str, Any]:
    """So sánh hai số, trả về delta + trend direction."""
    if current is None and previous is None:
        return {"value": None, "direction": "flat", "pct": None}
    if previous is None:
        return {"value": current, "direction": "new", "pct": None}
    if current is None:
        return {"value": -previous, "direction": "down", "pct": -100.0}
    if previous == 0:
        if current == 0:
            return {"value": 0, "direction": "flat", "pct": None}
        return {"value": current, "direction": "up" if current > 0 else "down", "pct": None}
    diff = round(current - previous, 2)
    pct = round((diff / previous) * 100, 1) if previous else None
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    return {"value": diff, "direction": direction, "pct": pct}


PRESETS = {
    "this_vs_last_week": "Tuần này vs Tuần trước",
    "last_7_days": "7 ngày qua vs 7 ngày trước đó",
    "last_30_days": "30 ngày qua vs 30 ngày trước đó",
    "this_vs_last_month": "Tháng này vs Tháng trước",
    "custom": "Tùy chọn",
}

PRESET_PERIOD_LABELS = {
    "this_vs_last_week": ("Tuần này", "Tuần trước"),
    "last_7_days": ("7 ngày qua", "7 ngày trước đó"),
    "last_30_days": ("30 ngày qua", "30 ngày trước đó"),
    "this_vs_last_month": ("Tháng này", "Tháng trước"),
    "custom": ("Kỳ A", "Kỳ B"),
}


def resolve_preset_ranges(
    preset: str,
    reference_date: date | None = None,
) -> tuple[tuple[date, date], tuple[date, date]]:
    """Trả về ((period_a_start, period_a_end), (period_b_start, period_b_end))
    period_a = kỳ hiện tại, period_b = kỳ so sánh trước đó."""
    ref = reference_date or date.today()
    if preset == "this_vs_last_week":
        a_start, a_end = _iso_week_range(ref)
        b_start, b_end = _iso_week_range(ref - timedelta(days=7))
    elif preset == "last_7_days":
        a_end = ref
        a_start = ref - timedelta(days=6)
        b_end = a_start - timedelta(days=1)
        b_start = b_end - timedelta(days=6)
    elif preset == "last_30_days":
        a_end = ref
        a_start = ref - timedelta(days=29)
        b_end = a_start - timedelta(days=1)
        b_start = b_end - timedelta(days=29)
    elif preset == "this_vs_last_month":
        a_start = ref.replace(day=1)
        # last day of this month: first day of next month - 1
        if a_start.month == 12:
            a_end = date(a_start.year, 12, 31)
            b_start = date(a_start.year - 1, 12, 1).replace(month=11)
        next_month_first = (a_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        a_end = next_month_first - timedelta(days=1)
        b_end = a_start - timedelta(days=1)
        b_start = b_end.replace(day=1)
    else:
        raise ValueError(f"Preset không hỗ trợ: {preset}")
    return (a_start, a_end), (b_start, b_end)


def analyze_period_comparison(
    period_a: tuple[date, date],
    period_b: tuple[date, date] | None = None,
    period_a_label: str = "Kỳ A",
    period_b_label: str = "Kỳ B",
) -> dict[str, Any]:
    """
    So sánh hiệu suất giữa 2 khoảng thời gian tùy ý.

    period_a = kỳ hiện tại (this period)
    period_b = kỳ so sánh (last period). Nếu None thì chỉ trả về metric kỳ A.
    """
    a_start, a_end = period_a
    b_start, b_end = period_b if period_b else (None, None)

    history = load_all_history()

    by_emp_week: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    emp_names: dict[str, str] = {}

    for entry in history:
        emp = entry.get("employee")
        if not emp:
            continue
        eid = emp["id"]
        emp_names[eid] = emp.get("name", eid)
        entry_date_str = entry.get("date", "")
        if not entry_date_str:
            continue
        try:
            entry_date = date.fromisoformat(entry_date_str)
        except ValueError:
            continue
        if a_start <= entry_date <= a_end:
            by_emp_week[(eid, "this")].append(entry)
        elif b_start and b_end and b_start <= entry_date <= b_end:
            by_emp_week[(eid, "last")].append(entry)

    employee_ids = sorted(emp_names.keys())
    employees = []

    for eid in employee_ids:
        this_entries = by_emp_week.get((eid, "this"), [])
        last_entries = by_emp_week.get((eid, "last"), [])
        if not this_entries and not last_entries:
            continue

        this_metrics = _aggregate_week(this_entries)
        last_metrics = _aggregate_week(last_entries)

        delta = {
            "avg_score": _delta(this_metrics["avg_score"], last_metrics["avg_score"]),
            "passed_rate": _delta(this_metrics["passed_rate"], last_metrics["passed_rate"]),
            "blacklist_count": _delta(
                this_metrics["blacklist_count"], last_metrics["blacklist_count"]
            ),
            "conversation_count": _delta(
                this_metrics["conversation_count"], last_metrics["conversation_count"]
            ),
            "avg_response_seconds": _delta(
                this_metrics["avg_response_seconds"], last_metrics["avg_response_seconds"]
            ),
        }

        # Verdict
        if not last_entries and this_entries:
            verdict = "new"
        elif last_entries and not this_entries:
            verdict = "left"
        else:
            score_dir = delta["avg_score"]["direction"]
            bl_diff = (delta["blacklist_count"]["value"] or 0)
            if score_dir == "up" and bl_diff <= 0:
                verdict = "improved"
            elif score_dir == "down" or bl_diff > 0:
                verdict = "declined"
            else:
                verdict = "stable"

        employees.append({
            "employee_id": eid,
            "employee_name": emp_names[eid],
            "this_week": this_metrics,
            "last_week": last_metrics,
            "delta": delta,
            "verdict": verdict,
        })

    # Team summary
    all_this = [e for entries in by_emp_week.items() for e in entries[1] if entries[0][1] == "this"]
    all_last = [e for entries in by_emp_week.items() for e in entries[1] if entries[0][1] == "last"]
    team_this = _aggregate_week(all_this)
    team_last = _aggregate_week(all_last)

    return {
        "period_a_label": period_a_label,
        "period_b_label": period_b_label,
        "this_week": {
            "label": _week_label(a_start, a_end),
            "start": a_start.isoformat(),
            "end": a_end.isoformat(),
        },
        "last_week": {
            "label": _week_label(b_start, b_end) if b_start and b_end else "—",
            "start": b_start.isoformat() if b_start else None,
            "end": b_end.isoformat() if b_end else None,
        },
        "employees": sorted(
            employees,
            key=lambda e: (
                {"declined": 0, "new": 1, "stable": 2, "improved": 3, "left": 4}[e["verdict"]],
                -(e["this_week"]["avg_score"] or 0),
            ),
        ),
        "summary": {
            "team_this": team_this,
            "team_last": team_last,
            "team_delta_avg_score": _delta(team_this["avg_score"], team_last["avg_score"]),
        },
    }


def analyze_weekly_comparison(reference_date: date | None = None) -> dict[str, Any]:
    """Backward-compat wrapper: tuần này vs tuần trước."""
    period_a, period_b = resolve_preset_ranges("this_vs_last_week", reference_date)
    a_label, b_label = PRESET_PERIOD_LABELS["this_vs_last_week"]
    return analyze_period_comparison(period_a, period_b, a_label, b_label)
