"""
Import hội thoại từ CSV hoặc JSON batch vào sample_conversations.json (hoặc file tùy chọn).

CSV format tối thiểu (mỗi dòng = 1 tin nhắn):
    conversation_id, channel, employee_id, employee_name, sender_type, text, sent_at[, attachment_type, attachment_name]

JSON batch format: list của conversation objects theo schema chuẩn của repo.

CLI:
    python -m app.importer --file path/to/data.csv [--output data/sample_conversations.json] [--append]
    python -m app.importer --file path/to/data.json [--output data/sample_conversations.json] [--append]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = BASE_DIR / "data" / "sample_conversations.json"


# ── CSV → internal dict ─────────────────────────────────────────────────────

_REQUIRED_CSV_COLUMNS = {"conversation_id", "sender_type", "text"}

def _parse_csv(path: Path) -> list[dict[str, Any]]:
    """Đọc CSV và group theo conversation_id, trả về list conversation dicts."""
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        raise ValueError("CSV rỗng, không có dữ liệu.")

    cols = set(rows[0].keys())
    missing = _REQUIRED_CSV_COLUMNS - cols
    if missing:
        raise ValueError(f"CSV thiếu cột bắt buộc: {', '.join(sorted(missing))}")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, dict[str, Any]] = {}

    for i, row in enumerate(rows):
        conv_id = row.get("conversation_id", "").strip()
        if not conv_id:
            print(f"  [warn] Dòng {i + 2}: bỏ qua vì thiếu conversation_id", file=sys.stderr)
            continue

        if conv_id not in meta:
            meta[conv_id] = {
                "channel": row.get("channel", "unknown").strip() or "unknown",
                "employee_id": row.get("employee_id", "").strip(),
                "employee_name": row.get("employee_name", "").strip(),
            }

        attachment: list[dict[str, Any]] = []
        att_type = row.get("attachment_type", "").strip()
        if att_type:
            attachment = [{"type": att_type, "name": row.get("attachment_name", "").strip() or None}]

        grouped[conv_id].append({
            "id": f"{conv_id}_m{i + 1}",
            "sender_type": row.get("sender_type", "").strip(),
            "text": row.get("text", "").strip(),
            "attachments": attachment,
            "sent_at": row.get("sent_at", "").strip() or None,
        })

    conversations = []
    for conv_id, messages in grouped.items():
        m = meta[conv_id]
        employee = None
        if m["employee_id"] or m["employee_name"]:
            employee = {"id": m["employee_id"] or "unknown", "name": m["employee_name"] or m["employee_id"]}
        conversations.append({
            "external_id": conv_id,
            "channel": m["channel"],
            "employee": employee,
            "metadata": {"has_complaint": False, "flags": {}},
            "messages": messages,
        })
    return conversations


# ── JSON batch ───────────────────────────────────────────────────────────────

def _parse_json(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON phải là array hoặc object hội thoại.")
    for item in data:
        if "external_id" not in item:
            raise ValueError(f"Thiếu trường 'external_id' trong: {json.dumps(item)[:80]}")
    return data


# ── Validation ───────────────────────────────────────────────────────────────

def _validate_conversations(conversations: list[dict[str, Any]]) -> list[str]:
    errors = []
    for conv in conversations:
        cid = conv.get("external_id", "?")
        if not conv.get("messages"):
            errors.append(f"{cid}: không có messages")
        for msg in conv.get("messages", []):
            if msg.get("sender_type") not in ("employee", "customer"):
                errors.append(f"{cid}: sender_type không hợp lệ — '{msg.get('sender_type')}'")
    return errors


# ── Write to output ──────────────────────────────────────────────────────────

def _write_output(
    new_conversations: list[dict[str, Any]],
    output_path: Path,
    append: bool,
) -> tuple[int, int]:
    """Ghi vào file output. Trả về (added, skipped)."""
    existing: list[dict[str, Any]] = []
    if append and output_path.exists():
        with output_path.open("r", encoding="utf-8") as f:
            existing = json.load(f)

    existing_ids = {c["external_id"] for c in existing}
    added, skipped = 0, 0
    for conv in new_conversations:
        if conv["external_id"] in existing_ids:
            skipped += 1
        else:
            existing.append(conv)
            existing_ids.add(conv["external_id"])
            added += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    return added, skipped


# ── Public API ───────────────────────────────────────────────────────────────

def import_file(
    file_path: str | Path,
    output_path: str | Path = DEFAULT_OUTPUT,
    append: bool = True,
) -> dict[str, Any]:
    """
    Import conversations từ file CSV hoặc JSON vào output_path.
    Trả về dict: {"added": int, "skipped": int, "errors": list[str]}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy file: {path}")

    ext = path.suffix.lower()
    if ext == ".csv":
        conversations = _parse_csv(path)
    elif ext in (".json", ".jsonl"):
        conversations = _parse_json(path)
    else:
        raise ValueError(f"Định dạng không hỗ trợ: {ext}. Dùng .csv hoặc .json")

    errors = _validate_conversations(conversations)
    if errors:
        return {"added": 0, "skipped": 0, "errors": errors}

    added, skipped = _write_output(conversations, Path(output_path), append)
    return {"added": added, "skipped": skipped, "errors": []}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Import hội thoại từ CSV/JSON vào conversations file."
    )
    parser.add_argument("--file", required=True, help="Đường dẫn file CSV hoặc JSON cần import.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"File output (mặc định: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        default=True,
        help="Merge vào file output hiện có (mặc định: true).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Ghi đè toàn bộ file output thay vì merge.",
    )
    args = parser.parse_args()

    append = not args.overwrite
    try:
        result = import_file(args.file, args.output, append=append)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)

    if result["errors"]:
        print("[error] Dữ liệu không hợp lệ:", file=sys.stderr)
        for err in result["errors"]:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)

    print(f"[import] Thêm mới: {result['added']} hội thoại | Bỏ qua (trùng ID): {result['skipped']}")
    print(f"[import] Output: {args.output}")


if __name__ == "__main__":
    main()
