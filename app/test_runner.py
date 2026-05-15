from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluator import evaluate_conversation
from app.schemas import load_conversations, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"


def format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def render_text_result(result: dict) -> str:
    lines = [
        f"Conversation: {result['conversation_id']}",
        f"Score: {format_number(result['total_score'])}/{format_number(result['max_score'])}",
        f"Grade: {result['grade']}",
        f"Passed: {'true' if result['passed'] else 'false'}",
        f"Blacklist triggered: {'true' if result['blacklist_triggered'] else 'false'}",
        "",
        "Findings failed:",
    ]
    failed_findings = [item for item in result["findings"] if not item["passed"]]
    if not failed_findings:
        lines.append("- None")
    else:
        for item in failed_findings:
            lines.append(f"- {item['rule_name']}: {item['explanation']}")

    if result["blacklist_findings"]:
        lines.extend(["", "Blacklist findings:"])
        for item in result["blacklist_findings"]:
            lines.append(f"- {item['rule_name']}: {item['explanation']}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run local scoring tests for message QA.")
    parser.add_argument("--conversation-id", help="Only evaluate one conversation by external_id.")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text.")
    parser.add_argument("--rules", default=str(RULESET_PATH), help="Path to rules.json.")
    parser.add_argument("--input", default=str(CONVERSATIONS_PATH), help="Path to sample conversations JSON.")
    args = parser.parse_args()

    ruleset = load_ruleset(args.rules)
    conversations = load_conversations(args.input)

    if args.conversation_id:
        conversations = [item for item in conversations if item.external_id == args.conversation_id]

    if not conversations:
        raise SystemExit("Khong tim thay hoi thoai nao de cham.")

    outputs = [evaluate_conversation(conversation, ruleset).to_dict() for conversation in conversations]
    if args.json:
        print(json.dumps(outputs[0] if len(outputs) == 1 else outputs, ensure_ascii=False, indent=2))
        return

    for index, result in enumerate(outputs):
        if index:
            print("-" * 72)
        print(render_text_result(result))


if __name__ == "__main__":
    main()
