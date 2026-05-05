"""
Export kết quả evaluation và scorecard ra CSV.

CLI:
    python -m app.exporter --type results   [--output report_results.csv]
    python -m app.exporter --type scorecard [--output report_scorecard.csv]
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent


def _flatten_findings(findings: list[dict[str, Any]], passed: bool) -> str:
    failed = [f["rule_name"] for f in findings if not f.get("passed", passed)]
    return "; ".join(failed) if failed else ""


def export_results_csv(evaluation_results: list[dict[str, Any]], conversations: list[Any]) -> str:
    """Trả về nội dung CSV (string) của danh sách evaluation results."""
    conv_map = {c.external_id: c for c in conversations}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "conversation_id", "employee_id", "employee_name", "channel",
        "date", "total_score", "max_score", "grade", "passed",
        "blacklist_triggered", "failed_rules", "blacklist_rules",
    ])
    for result in evaluation_results:
        conv = conv_map.get(result["conversation_id"])
        emp_id = conv.employee.id if conv and conv.employee else ""
        emp_name = conv.employee.name if conv and conv.employee else ""
        channel = conv.channel if conv else ""
        date_str = ""
        if conv and conv.messages and conv.messages[0].sent_at:
            date_str = conv.messages[0].sent_at[:10]

        failed_rules = _flatten_findings(result.get("findings", []), passed=False)
        blacklist_rules = "; ".join(f["rule_name"] for f in result.get("blacklist_findings", []))

        writer.writerow([
            result["conversation_id"],
            emp_id,
            emp_name,
            channel,
            date_str,
            result["total_score"],
            result["max_score"],
            result["grade"],
            "Đạt" if result["passed"] else "Không đạt",
            "Có" if result["blacklist_triggered"] else "Không",
            failed_rules,
            blacklist_rules,
        ])
    return output.getvalue()


def export_scorecard_csv(scorecards: list[dict[str, Any]]) -> str:
    """Trả về nội dung CSV (string) của employee scorecards."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "employee_id", "employee_name", "conversation_count",
        "average_score", "grade", "passed_rate_pct",
        "blacklist_count", "top_weak_skill",
    ])
    for sc in scorecards:
        top_skill = sc["top_failed_skills"][0]["skill"] if sc["top_failed_skills"] else ""
        writer.writerow([
            sc["employee_id"],
            sc["employee_name"],
            sc["conversation_count"],
            sc["average_score"],
            sc["grade"],
            round(sc["passed_rate"] * 100, 1),
            sc["blacklist_count"],
            top_skill,
        ])
    return output.getvalue()


def write_csv_file(content: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    import sys
    from app.evaluator import evaluate_conversation
    from app.employee_scorecard import build_employee_scorecards
    from app.schemas import load_conversations, load_ruleset
    from app.training import load_training_modules

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Export báo cáo CSV.")
    parser.add_argument("--type", choices=["results", "scorecard"], default="results",
                        help="Loại báo cáo cần export.")
    parser.add_argument("--output", help="Đường dẫn file output CSV.")
    parser.add_argument("--input", default=str(BASE_DIR / "data" / "sample_conversations.json"),
                        help="File conversations JSON.")
    args = parser.parse_args()

    ruleset = load_ruleset(BASE_DIR / "data" / "rules.json")
    conversations = load_conversations(args.input)
    results = [evaluate_conversation(c, ruleset).to_dict() for c in conversations]

    if args.type == "results":
        content = export_results_csv(results, conversations)
        out_path = Path(args.output) if args.output else BASE_DIR / "data" / "report_results.csv"
    else:
        training = load_training_modules(BASE_DIR / "data" / "training_modules.json")
        scorecards = build_employee_scorecards(conversations, results, training)
        content = export_scorecard_csv(scorecards)
        out_path = Path(args.output) if args.output else BASE_DIR / "data" / "report_scorecard.csv"

    write_csv_file(content, out_path)
    print(f"[export] Đã lưu: {out_path}")


if __name__ == "__main__":
    main()
