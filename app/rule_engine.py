from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from app.schemas import Conversation, Finding, Message, Rule


EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]",
    flags=re.UNICODE,
)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    lowered = text.strip().lower()
    decomposed = unicodedata.normalize("NFD", lowered)
    no_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", no_accents)


def keyword_in_text(text: str, keyword: str) -> bool:
    return normalize_text(keyword) in normalize_text(text)


def get_messages_for_scope(conversation: Conversation, scope: str) -> list[Message]:
    if scope == "all":
        return conversation.messages
    return [message for message in conversation.messages if message.sender_type == scope]


def combine_text(messages: Iterable[Message]) -> str:
    return "\n".join(message.text for message in messages)


def build_finding(
    rule: Rule,
    passed: bool,
    evidence_messages: list[Message],
    explanation: str,
    suggestion: str | None = None,
    override_score: float | None = None,
    evidence_text: str | None = None,
) -> Finding:
    score = rule.max_score if passed else 0.0
    if override_score is not None:
        score = override_score
    if evidence_text is None and evidence_messages:
        evidence_text = " | ".join(message.text for message in evidence_messages[:2])
    return Finding(
        rule_id=rule.id,
        rule_name=rule.name,
        passed=passed,
        score=score,
        max_score=rule.max_score,
        severity=rule.severity,
        evidence_message_ids=[message.id for message in evidence_messages[:3]],
        evidence_text=evidence_text,
        explanation=explanation,
        suggestion=None if passed else suggestion,
    )


def evaluate_keyword_any(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    matched = [
        message
        for message in messages
        if any(keyword_in_text(message.text, keyword) for keyword in rule.config["keywords"])
    ]
    return build_finding(
        rule,
        bool(matched),
        matched,
        rule.config.get("pass_explanation", f"Đã tìm thấy nội dung phù hợp cho rule {rule.name}.")
        if matched
        else rule.config.get("fail_explanation", f"Chưa tìm thấy nội dung bắt buộc cho rule {rule.name}."),
        suggestion=rule.config.get("fail_suggestion"),
    )


def evaluate_keyword_all(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    combined = combine_text(messages)
    missing = [keyword for keyword in rule.config["keywords"] if not keyword_in_text(combined, keyword)]
    matched = [message for message in messages if any(keyword_in_text(message.text, keyword) for keyword in rule.config["keywords"])]
    passed = not missing
    explanation = (
        rule.config.get("pass_explanation", f"Đã có đầy đủ các ý bắt buộc cho rule {rule.name}.")
        if passed
        else rule.config.get("fail_explanation", f"Thiếu các ý bắt buộc: {', '.join(missing)}.")
    )
    return build_finding(rule, passed, matched, explanation, suggestion=rule.config.get("fail_suggestion"))


def evaluate_keyword_count_min(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    min_count = int(rule.config["min_count"])

    if rule.config.get("mode") == "question_count":
        total_questions = sum(message.text.count("?") for message in messages)
        passed = total_questions >= min_count
        evidence = [message for message in messages if "?" in message.text]
        explanation = (
            f"Đã có {total_questions} câu hỏi khai thác."
            if passed
            else f"Mới có {total_questions}/{min_count} câu hỏi khai thác."
        )
        return build_finding(
            rule,
            passed,
            evidence,
            explanation,
            suggestion=rule.config.get("fail_suggestion"),
            evidence_text=f"Question marks counted: {total_questions}",
        )

    if "keyword_groups" in rule.config:
        matched_groups = 0
        evidence_messages: list[Message] = []
        for group in rule.config["keyword_groups"]:
            group_hit = [
                message
                for message in messages
                if any(keyword_in_text(message.text, keyword) for keyword in group)
            ]
            if group_hit:
                matched_groups += 1
                evidence_messages.extend(group_hit[:1])
        passed = matched_groups >= min_count
        explanation = (
            f"Đã phủ {matched_groups} nhóm thông tin bắt buộc."
            if passed
            else f"Mới phủ {matched_groups}/{min_count} nhóm thông tin bắt buộc."
        )
        return build_finding(
            rule,
            passed,
            evidence_messages,
            explanation,
            suggestion=rule.config.get("fail_suggestion"),
            evidence_text=f"Matched groups: {matched_groups}",
        )

    match_count = sum(
        1
        for message in messages
        if any(keyword_in_text(message.text, keyword) for keyword in rule.config["keywords"])
    )
    matched_messages = [
        message for message in messages if any(keyword_in_text(message.text, keyword) for keyword in rule.config["keywords"])
    ]
    passed = match_count >= min_count
    explanation = (
        f"Đã đạt {match_count} lượt khớp keyword."
        if passed
        else f"Mới đạt {match_count}/{min_count} lượt khớp keyword."
    )
    return build_finding(rule, passed, matched_messages, explanation, suggestion=rule.config.get("fail_suggestion"))


def evaluate_forbidden_keyword(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    hits = [
        message
        for message in messages
        if any(keyword_in_text(message.text, keyword) for keyword in rule.config["keywords"])
    ]
    return build_finding(
        rule,
        not hits,
        hits,
        rule.config.get("pass_explanation", f"Không phát hiện nội dung cấm trong rule {rule.name}.")
        if not hits
        else rule.config.get("fail_explanation", f"Phát hiện nội dung không phù hợp trong rule {rule.name}."),
        suggestion=rule.config.get("fail_suggestion"),
    )


def evaluate_max_emoji_per_message(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    max_emoji = int(rule.config["max_emoji"])
    violations = [message for message in messages if len(EMOJI_RE.findall(message.text)) > max_emoji]
    explanation = (
        f"Mỗi tin nhắn đều không vượt quá {max_emoji} icon."
        if not violations
        else f"Có tin nhắn vượt quá {max_emoji} icon."
    )
    return build_finding(rule, not violations, violations, explanation, suggestion=rule.config.get("fail_suggestion"))


def evaluate_employee_last_message(conversation: Conversation, rule: Rule) -> Finding:
    if not conversation.messages:
        return build_finding(rule, False, [], "Hội thoại rỗng.", suggestion=rule.config.get("fail_suggestion"))

    last_message = conversation.messages[-1]
    closing_keywords = rule.config.get("closing_keywords", [])
    has_closing_keyword = True if not closing_keywords else any(
        keyword_in_text(last_message.text, keyword) for keyword in closing_keywords
    )
    passed = last_message.sender_type == "employee" and has_closing_keyword
    explanation = (
        "Nhân viên là người kết thúc cuộc trò chuyện đúng chuẩn."
        if passed
        else "Khách đang là người nhắn cuối hoặc nhân viên chưa kết thúc đúng chuẩn."
    )
    return build_finding(rule, passed, [last_message], explanation, suggestion=rule.config.get("fail_suggestion"))


def evaluate_customer_not_left_unanswered(conversation: Conversation, rule: Rule) -> Finding:
    if not conversation.messages:
        return build_finding(rule, False, [], "Hội thoại rỗng.", suggestion=rule.config.get("fail_suggestion"))
    last_message = conversation.messages[-1]
    passed = last_message.sender_type != "customer"
    explanation = (
        "Khách không bị bỏ lại ở cuối hội thoại."
        if passed
        else "Khách là người nhắn cuối nhưng nhân viên chưa phản hồi."
    )
    return build_finding(rule, passed, [last_message], explanation, suggestion=rule.config.get("fail_suggestion"))


def evaluate_attachment_required(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    required_types = {item.lower() for item in rule.config.get("attachment_types", [])}
    matched = [
        message
        for message in messages
        if any(attachment.type.lower() in required_types for attachment in message.attachments)
    ]
    return build_finding(
        rule,
        bool(matched),
        matched,
        "Đã có attachment bắt buộc."
        if matched
        else "Chưa có attachment bắt buộc trong hội thoại.",
        suggestion=rule.config.get("fail_suggestion"),
    )


def evaluate_complaint_flow(conversation: Conversation, rule: Rule) -> Finding:
    metadata_complaint = bool(conversation.metadata.get("has_complaint"))
    complaint_keywords = rule.config.get("complaint_keywords", [])
    customer_text = combine_text(get_messages_for_scope(conversation, "customer"))
    has_complaint = metadata_complaint or any(
        keyword_in_text(customer_text, keyword) for keyword in complaint_keywords
    )
    if not has_complaint:
        return build_finding(
            rule,
            True,
            [],
            "Hội thoại không có khiếu nại, rule xử lý khiếu nại được coi là đạt.",
        )

    employee_text = combine_text(get_messages_for_scope(conversation, "employee"))
    missing_steps = []
    for step in rule.config["steps"]:
        step_name = step["name"]
        if not any(keyword_in_text(employee_text, keyword) for keyword in step["keywords"]):
            missing_steps.append(step_name)

    passed = not missing_steps
    explanation = (
        "Đã xử lý khiếu nại đủ 4 bước."
        if passed
        else f"Thiếu bước xử lý khiếu nại: {', '.join(missing_steps)}."
    )
    evidence_messages = [message for message in get_messages_for_scope(conversation, "employee") if message.text]
    return build_finding(rule, passed, evidence_messages, explanation, suggestion=rule.config.get("fail_suggestion"))


def evaluate_missing_required_before_advice(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    required_keywords = rule.config["required_keywords"]
    advice_keywords = rule.config["advice_keywords"]
    first_required_index: int | None = None
    first_advice_message: Message | None = None

    for index, message in enumerate(messages):
        if first_required_index is None and any(keyword_in_text(message.text, keyword) for keyword in required_keywords):
            first_required_index = index
        if first_advice_message is None and any(keyword_in_text(message.text, keyword) for keyword in advice_keywords):
            first_advice_message = message
            first_advice_index = index
            if first_required_index is None or first_required_index > first_advice_index:
                return build_finding(
                    rule,
                    False,
                    [message],
                    "Nhân viên tư vấn trước khi yêu cầu ảnh tóc hiện tại.",
                    suggestion=rule.config.get("fail_suggestion"),
                    evidence_text=message.text,
                )

    return build_finding(rule, True, [first_advice_message] if first_advice_message else [], "Không phát hiện tư vấn hời hợt.")


def evaluate_metadata_flag(conversation: Conversation, rule: Rule) -> Finding:
    flag_name = rule.config["flag_name"]
    flags = conversation.metadata.get("flags", {})
    flagged = bool(flags.get(flag_name))
    explanation = (
        f"Metadata không bật cờ {flag_name}."
        if not flagged
        else f"Metadata bật cờ {flag_name}, hội thoại vi phạm rule."
    )
    return build_finding(rule, not flagged, [], explanation, suggestion=rule.config.get("fail_suggestion"))


RULE_HANDLERS = {
    "keyword_any": evaluate_keyword_any,
    "keyword_all": evaluate_keyword_all,
    "keyword_count_min": evaluate_keyword_count_min,
    "forbidden_keyword": evaluate_forbidden_keyword,
    "max_emoji_per_message": evaluate_max_emoji_per_message,
    "employee_last_message": evaluate_employee_last_message,
    "customer_not_left_unanswered": evaluate_customer_not_left_unanswered,
    "attachment_required": evaluate_attachment_required,
    "complaint_flow": evaluate_complaint_flow,
    "missing_required_before_advice": evaluate_missing_required_before_advice,
    "metadata_flag": evaluate_metadata_flag,
}


def evaluate_rule(conversation: Conversation, rule: Rule) -> Finding:
    handler = RULE_HANDLERS.get(rule.type)
    if handler is None:
        raise ValueError(f"Unsupported rule type: {rule.type}")
    return handler(conversation, rule)
