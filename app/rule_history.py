from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import json

from app.schemas import Ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_PATH = BASE_DIR / "data" / "rules_history.json"

VN_TZ = timezone(timedelta(hours=7))


def _load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"versions": []}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if "versions" not in data or not isinstance(data["versions"], list):
        return {"versions": []}
    return data


def _save_history(history: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(history, file, ensure_ascii=False, indent=2)


def record_version(ruleset: Ruleset, history_path: Path | None = None) -> str:
    path = history_path or HISTORY_PATH
    version = ruleset.compute_version()
    history = _load_history(path)
    existing_versions = {entry["version"] for entry in history["versions"]}
    if version in existing_versions:
        return version
    history["versions"].append({
        "version": version,
        "created_at": datetime.now(VN_TZ).isoformat(timespec="seconds"),
        "ruleset_snapshot": ruleset.to_dict(),
    })
    _save_history(history, path)
    return version


def load_ruleset_version(version: str, history_path: Path | None = None) -> Ruleset:
    path = history_path or HISTORY_PATH
    history = _load_history(path)
    for entry in history["versions"]:
        if entry["version"] == version:
            return Ruleset.from_dict(entry["ruleset_snapshot"])
    raise KeyError(f"Rules version not found: {version}")


def list_versions(history_path: Path | None = None) -> list[dict[str, Any]]:
    path = history_path or HISTORY_PATH
    history = _load_history(path)
    return [
        {"version": entry["version"], "created_at": entry["created_at"]}
        for entry in history["versions"]
    ]
