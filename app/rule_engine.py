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
    norm_text = normalize_text(text)
    norm_keyword = normalize_text(keyword).strip()
    if not norm_keyword:
        return False
    pattern = r"(?<![\w])" + re.escape(norm_keyword) + r"(?![\w])"
    return bool(re.search(pattern, norm_text))


def get_messages_for_scope(conversation: Conversation, scope: str) -> list[Message]:
    if scope == "all":
        return conversation.messages
    return [message for message in conversation.messages if message.sender_type == scope]


def combine_text(messages: Iterable[Message]) -> str:
    return "\n".join(message.text for message in messages)


def message_has_attachment_type(message: Message, attachment_type: str) -> bool:
    expected = attachment_type.lower()
    return any((attachment.type or "").lower() == expected for attachment in message.attachments)


def find_customer_image_messages(conversation: Conversation) -> list[Message]:
    return [
        message
        for message in conversation.messages
        if message.sender_type == "customer" and message_has_attachment_type(message, "image")
    ]


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
        rationale=rule.rationale,
    )


def evaluate_keyword_any(conversation: Conversation, rule: Rule) -> Finding:
    messages = get_messages_for_scope(conversation, rule.applies_to)
    matched = [
        message
        for message in messages
        if any(keyword_in_text(message.text, keyword) for keyword in rule.config["keywords"])
    ]
    # Some rules are satisfied if the customer has already provided the required evidence,
    # even when the employee did not explicitly type the expected phrase.
    if rule.id == "request_current_hair_photo" and not matched:
        customer_images = find_customer_image_messages(conversation)
        if customer_images:
            return build_finding(
                rule,
                True,
                customer_images,
                "Khách đã gửi ảnh tóc thực tế trong hội thoại.",
                evidence_text="Customer image received before/within consultation flow.",
            )
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


CUSTOMER_CLOSING_KEYWORDS = [
    "cảm ơn", "cám ơn", "thanks", "thank you", "tks", "tk",
    "ok shop", "ok ạ", "okela", "oke ạ", "oki", "okie",
    "em đặt", "em chốt", "chốt đơn",
    "vâng ạ", "dạ vâng", "dạ được", "được rồi",
    "vậy nhé", "vậy ha",
]


def evaluate_customer_not_left_unanswered(conversation: Conversation, rule: Rule) -> Finding:
    if not conversation.messages:
        return build_finding(rule, False, [], "Hội thoại rỗng.", suggestion=rule.config.get("fail_suggestion"))
    last_message = conversation.messages[-1]
    if last_message.sender_type != "customer":
        return build_finding(rule, True, [last_message], "Khách không bị bỏ lại ở cuối hội thoại.")
    closing_keywords = rule.config.get("customer_closing_keywords", CUSTOMER_CLOSING_KEYWORDS)
    is_closing_ack = any(keyword_in_text(last_message.text, kw) for kw in closing_keywords)
    if is_closing_ack:
        return build_finding(
            rule,
            True,
            [last_message],
            "Khách nhắn cuối là lời cảm ơn / xác nhận, không phải câu hỏi cần trả lời.",
        )
    return build_finding(
        rule,
        False,
        [last_message],
        "Khách là người nhắn cuối và chưa có acknowledgment từ shop.",
        suggestion=rule.config.get("fail_suggestion"),
    )


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
    required_keywords = rule.config["required_keywords"]
    advice_keywords = rule.config["advice_keywords"]

    first_advice_index: int | None = None
    first_advice_message: Message | None = None
    first_required_index: int | None = None

    customer_image_indexes = [
        index
        for index, message in enumerate(conversation.messages)
        if message.sender_type == "customer" and message_has_attachment_type(message, "image")
    ]

    for index, message in enumerate(conversation.messages):
        if message.sender_type != rule.applies_to:
            continue
        text = message.text
        if first_required_index is None and any(keyword_in_text(text, kw) for kw in required_keywords):
            first_required_index = index
        if first_advice_message is None and any(keyword_in_text(text, kw) for kw in advice_keywords):
            first_advice_message = message
            first_advice_index = index

    if first_advice_message is None:
        return build_finding(
            rule,
            True,
            [],
            "Hội thoại không có tư vấn ràng buộc, rule không áp dụng.",
        )

    if first_required_index is not None and first_required_index <= first_advice_index:
        return build_finding(
            rule,
            True,
            [first_advice_message],
            "Nhân viên đã yêu cầu ảnh tóc trước khi tư vấn.",
        )

    if customer_image_indexes:
        first_image_index = customer_image_indexes[0]
        return build_finding(
            rule,
            True,
            [conversation.messages[first_image_index]],
            "Khách đã gửi ảnh tóc trong hội thoại, nhân viên có evidence để tư vấn.",
            evidence_text="Customer image provided in conversation.",
        )

    return build_finding(
        rule,
        False,
        [first_advice_message],
        "Nhân viên tư vấn mà không có ảnh tóc của khách trong toàn bộ hội thoại.",
        suggestion=rule.config.get("fail_suggestion"),
        evidence_text=first_advice_message.text,
    )


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


def evaluate_conditional_keyword(conversation: Conversation, rule: Rule) -> Finding:
    """Rule chỉ áp dụng khi khách trigger bằng `trigger_keywords`. Nếu không
    trigger → skip (`max_score=0`, không ảnh hưởng tổng điểm). Nếu trigger
    và nhân viên trả lời chứa `response_keywords` → pass. Trigger mà nhân
    viên không response đúng → fail."""
    cfg = rule.config or {}
    triggers = cfg.get("trigger_keywords", [])
    responses = cfg.get("response_keywords", [])

    triggered_at = None
    triggered_msg: Message | None = None
    for msg in conversation.messages:
        if msg.sender_type != "customer":
            continue
        if any(keyword_in_text(msg.text, kw) for kw in triggers):
            triggered_at = msg.sent_at
            triggered_msg = msg
            break

    if triggered_at is None:
        finding = build_finding(
            rule, True, [], "Khách không hỏi về kỹ thuật — rule không áp dụng.",
        )
        finding.skipped = True
        finding.score = 0.0
        finding.max_score = 0.0
        return finding

    for msg in conversation.messages:
        if msg.sender_type != "employee":
            continue
        if msg.sent_at is None or triggered_at is None or msg.sent_at <= triggered_at:
            continue
        if any(keyword_in_text(msg.text, kw) for kw in responses):
            return build_finding(
                rule, True, [triggered_msg, msg] if triggered_msg else [msg],
                "Nhân viên đã hướng dẫn kỹ thuật theo yêu cầu khách.",
            )

    return build_finding(
        rule, False,
        [triggered_msg] if triggered_msg else [],
        "Khách có hỏi kỹ thuật nhưng nhân viên chưa hướng dẫn rõ.",
        suggestion=cfg.get("fail_suggestion"),
    )


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
    "conditional_keyword": evaluate_conditional_keyword,
}


def _normalize_channel(value: str | None) -> str:
    """Strip the `nhanh_` prefix the adapter prepends and lowercase."""
    if not value:
        return ""
    raw = str(value).lower()
    return raw[len("nhanh_"):] if raw.startswith("nhanh_") else raw


def should_skip_rule_for_channel(rule: Rule, conversation: Conversation) -> tuple[bool, str]:
    """Decide whether a rule should be skipped based on the conversation's
    channel. Returns (skip, reason). Skipped rules contribute 0 to both score
    and effective max_score so they don't penalise unrelated conversations."""
    cfg = rule.config or {}
    channel = _normalize_channel(conversation.channel)

    skip_list = [str(c).lower() for c in cfg.get("skip_for_channels", [])]
    if channel and channel in skip_list:
        return True, cfg.get("skip_reason", f"Channel '{channel}' không áp dụng rule này.")

    applies_list = [str(c).lower() for c in cfg.get("applies_to_channels", [])]
    if applies_list and channel and channel not in applies_list:
        return True, cfg.get(
            "skip_reason",
            f"Rule chỉ áp dụng cho channel: {', '.join(applies_list)}.",
        )
    return False, ""


def evaluate_rule(conversation: Conversation, rule: Rule) -> Finding:
    skip, reason = should_skip_rule_for_channel(rule, conversation)
    if skip:
        finding = build_finding(rule, True, [], reason)
        finding.skipped = True
        finding.score = 0.0
        finding.max_score = 0.0
        return finding

    handler = RULE_HANDLERS.get(rule.type)
    if handler is None:
        raise ValueError(f"Unsupported rule type: {rule.type}")
    return handler(conversation, rule)
