from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


import os

BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_DIR = BASE_DIR / "data" / "history"
HISTORY_DEMO_DIR = BASE_DIR / "data" / "history_demo"
RULES_HISTORY_DIR = BASE_DIR / "data" / "rules_history"


def _include_demo() -> bool:
    """Demo data chỉ được load khi env var QA_INCLUDE_DEMO=1."""
    return os.environ.get("QA_INCLUDE_DEMO", "1") == "1"


def snapshot_ruleset_if_new(ruleset_path: Path) -> str | None:
    """
    Nếu version trong rules.json chưa có snapshot, tạo snapshot vào data/rules_history/vX.X.X.json.
    Trả về version string.
    """
    import json as _json
    with ruleset_path.open("r", encoding="utf-8") as f:
        data = _json.load(f)
    version = data.get("version", "unknown")
    RULES_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = RULES_HISTORY_DIR / f"{version}.json"
    if not snapshot_path.exists():
        with snapshot_path.open("w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    return version


def _today_str() -> str:
    return date.today().isoformat()


def _history_path(date_str: str) -> Path:
    return HISTORY_DIR / f"{date_str}.json"


def save_evaluation_results(
    evaluation_results: list[dict[str, Any]],
    conversations: list[Any],
    date_str: str | None = None,
) -> Path:
    """Lưu kết quả evaluation vào data/history/YYYY-MM-DD.json, merge với dữ liệu cũ nếu đã có."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    target_date = date_str or _today_str()
    path = _history_path(target_date)

    conv_map = {c.external_id: c for c in conversations}

    existing: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing = json.load(f)

    existing_ids = {entry["evaluation"]["conversation_id"] for entry in existing}

    new_entries = []
    for result in evaluation_results:
        conv_id = result["conversation_id"]
        if conv_id in existing_ids:
            continue
        conv = conv_map.get(conv_id)
        employee = None
        if conv and conv.employee:
            employee = {"id": conv.employee.id, "name": conv.employee.name}
        new_entries.append({
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "date": target_date,
            "employee": employee,
            "evaluation": result,
        })

    merged = existing + new_entries
    with path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    return path


def _history_dirs() -> list[Path]:
    """Trả về list folders cần đọc — luôn có HISTORY_DIR, thêm DEMO nếu env bật."""
    dirs = [HISTORY_DIR]
    if _include_demo() and HISTORY_DEMO_DIR.exists():
        dirs.append(HISTORY_DEMO_DIR)
    return [d for d in dirs if d.exists()]


def load_history_range(start: str, end: str) -> list[dict[str, Any]]:
    """Đọc tất cả entries từ start đến end (YYYY-MM-DD)."""
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    all_entries: list[dict[str, Any]] = []
    for d in _history_dirs():
        for path in sorted(d.glob("*.json")):
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if start_date <= file_date <= end_date:
                with path.open("r", encoding="utf-8") as f:
                    all_entries.extend(json.load(f))
    return all_entries


def load_all_history() -> list[dict[str, Any]]:
    """Đọc toàn bộ lịch sử (kèm demo nếu QA_INCLUDE_DEMO=1)."""
    all_entries: list[dict[str, Any]] = []
    for d in _history_dirs():
        for path in sorted(d.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                all_entries.extend(json.load(f))
    return all_entries


def build_employee_trend(history: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Từ danh sách history entries, gom theo nhân viên và trả về
    list các điểm theo ngày để vẽ trend.
    Format: { "emp_001": [{"date": "2026-05-04", "avg_score": 85.0, "count": 2}, ...] }
    """
    from collections import defaultdict

    # group by (employee_id, date)
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    employee_names: dict[str, str] = {}

    for entry in history:
        emp = entry.get("employee")
        if not emp:
            continue
        emp_id = emp["id"]
        employee_names[emp_id] = emp.get("name", emp_id)
        entry_date = entry.get("date", "")
        score = float(entry["evaluation"].get("total_score", 0))
        grouped[(emp_id, entry_date)].append(score)

    trend: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (emp_id, entry_date), scores in sorted(grouped.items()):
        avg = round(sum(scores) / len(scores), 2)
        trend[emp_id].append({
            "date": entry_date,
            "avg_score": avg,
            "count": len(scores),
            "employee_name": employee_names[emp_id],
        })

    return dict(trend)
