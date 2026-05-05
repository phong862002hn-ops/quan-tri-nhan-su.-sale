"""
Alert engine — tự động phát hiện nhân viên vi phạm vượt ngưỡng trong khoảng thời gian.

Thresholds được cấu hình trong data/alert_config.json (tạo tự động nếu chưa có).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.history import load_history_range, load_all_history


BASE_DIR = Path(__file__).resolve().parent.parent
ALERT_CONFIG_PATH = BASE_DIR / "data" / "alert_config.json"

_DEFAULT_CONFIG = {
    "blacklist_per_week": 2,
    "blacklist_per_month": 4,
    "low_score_threshold": 50.0,
    "low_score_streak": 3,
    "avg_score_below": 60.0,
    "min_conversations": 2,
}


def load_alert_config() -> dict[str, Any]:
    if ALERT_CONFIG_PATH.exists():
        with ALERT_CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        return {**_DEFAULT_CONFIG, **cfg}
    _write_default_config()
    return dict(_DEFAULT_CONFIG)


def _write_default_config() -> None:
    ALERT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(_DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)


def _iso_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def run_alerts(history: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """
    Chạy alert engine trên toàn bộ lịch sử.
    Trả về list alert dicts với các field: level, employee_id, employee_name, type, message, detail.
    """
    cfg = load_alert_config()
    entries = history if history is not None else load_all_history()

    if not entries:
        return []

    # Group by employee
    by_emp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    emp_names: dict[str, str] = {}
    for entry in entries:
        emp = entry.get("employee")
        if not emp:
            continue
        eid = emp["id"]
        emp_names[eid] = emp.get("name", eid)
        by_emp[eid].append(entry)

    today = date.today()
    week_start = _iso_week_start(today).isoformat()
    month_start = today.replace(day=1).isoformat()

    alerts: list[dict[str, Any]] = []

    for eid, emp_entries in by_emp.items():
        name = emp_names[eid]

        if len(emp_entries) < cfg["min_conversations"]:
            continue

        # ── Blacklist per week ──────────────────────────────────────────────
        bl_week = sum(
            1 for e in emp_entries
            if e.get("evaluation", {}).get("blacklist_triggered")
            and e.get("date", "") >= week_start
        )
        if bl_week >= cfg["blacklist_per_week"]:
            alerts.append({
                "level": "critical",
                "employee_id": eid,
                "employee_name": name,
                "type": "blacklist_week",
                "message": f"{name} vi phạm blacklist {bl_week} lần trong tuần này",
                "detail": f"Ngưỡng: ≥{cfg['blacklist_per_week']} lần/tuần",
            })

        # ── Blacklist per month ─────────────────────────────────────────────
        bl_month = sum(
            1 for e in emp_entries
            if e.get("evaluation", {}).get("blacklist_triggered")
            and e.get("date", "") >= month_start
        )
        if bl_month >= cfg["blacklist_per_month"]:
            alerts.append({
                "level": "critical",
                "employee_id": eid,
                "employee_name": name,
                "type": "blacklist_month",
                "message": f"{name} vi phạm blacklist {bl_month} lần trong tháng này",
                "detail": f"Ngưỡng: ≥{cfg['blacklist_per_month']} lần/tháng",
            })

        # ── Low score streak ────────────────────────────────────────────────
        sorted_entries = sorted(emp_entries, key=lambda e: e.get("date", ""))
        recent_scores = [float(e["evaluation"]["total_score"]) for e in sorted_entries]
        streak = 0
        for s in reversed(recent_scores):
            if s < cfg["low_score_threshold"]:
                streak += 1
            else:
                break
        if streak >= cfg["low_score_streak"]:
            alerts.append({
                "level": "warning",
                "employee_id": eid,
                "employee_name": name,
                "type": "low_score_streak",
                "message": f"{name} có {streak} hội thoại liên tiếp dưới {cfg['low_score_threshold']} điểm",
                "detail": f"Ngưỡng: {cfg['low_score_streak']} ca liên tiếp dưới {cfg['low_score_threshold']}đ",
            })

        # ── Low average score ───────────────────────────────────────────────
        avg = sum(recent_scores) / len(recent_scores)
        if avg < cfg["avg_score_below"] and len(recent_scores) >= cfg["min_conversations"]:
            alerts.append({
                "level": "warning",
                "employee_id": eid,
                "employee_name": name,
                "type": "low_avg_score",
                "message": f"{name} có điểm trung bình {round(avg, 1)} — dưới ngưỡng {cfg['avg_score_below']}",
                "detail": f"Tính trên {len(recent_scores)} hội thoại gần nhất",
            })

    # Deduplicate: ưu tiên critical, 1 alert/loại/nhân viên
    seen: set[tuple[str, str]] = set()
    deduped = []
    for alert in sorted(alerts, key=lambda a: (0 if a["level"] == "critical" else 1)):
        key = (alert["employee_id"], alert["type"])
        if key not in seen:
            seen.add(key)
            deduped.append(alert)

    return deduped
