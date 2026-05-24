from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.nhanh_client import extract_items
from app.schemas import Conversation


NHANH_CHANNEL_LABELS: dict[int, str] = {
    1: "Facebook",
    2: "Instagram",
    3: "Zalo",
    4: "Shopee",
    5: "Lazada",
    6: "Tiki",
    7: "Sendo",
    8: "Website",
    9: "TikTok",
}


def channel_label(channel_code: Any, page_id: str = "") -> str:
    try:
        code = int(channel_code)
        if code in NHANH_CHANNEL_LABELS:
            return NHANH_CHANNEL_LABELS[code]
    except (TypeError, ValueError):
        pass
    # Fallback: infer from pageId prefix
    pid = (page_id or "").lower()
    if pid.startswith("sp_"):
        return "Shopee"
    if pid.startswith("tt_"):
        return "TikTok"
    if pid.startswith("ig_"):
        return "Instagram"
    if pid.startswith("zl_"):
        return "Zalo"
    if pid.isdigit():
        return "Facebook"
    return "Unknown"


def timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number = number // 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).astimezone().isoformat()


def map_attachment(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") or {}
    attachment_type = str(item.get("type") or "unknown")
    url = payload.get("url") or item.get("url")
    title = payload.get("title")
    if not title and isinstance(payload.get("elements"), list) and payload["elements"]:
        title = payload["elements"][0].get("title")
    return {
        "type": attachment_type,
        "name": title or attachment_type,
        "url": url,
        "metadata": {"raw_payload": payload} if payload else {},
    }


def map_message(item: dict[str, Any], page_id: str, customer_id: str | None = None) -> dict[str, Any]:
    sender_id = str(item.get("senderId") or "")
    # Default: anyone who is NOT the known customer is treated as employee.
    # This handles multi-admin pages where admins have senderId != pageId.
    if customer_id and sender_id == str(customer_id):
        message_type = "customer"
    else:
        message_type = "employee"
    attachments = [map_attachment(attachment) for attachment in item.get("attachments", []) if isinstance(attachment, dict)]
    return {
        "id": str(item.get("id") or item.get("messageId") or ""),
        "sender_type": message_type,
        "text": str(item.get("message") or item.get("content") or item.get("text") or ""),
        "attachments": attachments,
        "sent_at": timestamp_to_iso(item.get("createdAt")),
    }


def map_live_conversation(
    conversation_id: str,
    messages_payload: dict[str, Any],
    summary_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = summary_item or {}
    page_id = str(summary.get("pageId") or "")
    customer_id = str(summary.get("pageUserId") or "")
    items = extract_items(messages_payload, "messages", "items")
    if not page_id and items:
        page_id = str(items[0].get("pageId") or "")
    if not customer_id:
        import sys
        print(
            f"[nhanh_adapter] WARNING: pageUserId missing for conversation {conversation_id!r}; "
            "cannot distinguish customer from staff — all messages will be tagged as employee.",
            file=sys.stderr,
        )
    mapped = [map_message(item, page_id=page_id, customer_id=customer_id) for item in items]
    mapped.sort(key=lambda m: (m["sent_at"] or "9999", m["id"]))
    messages = mapped
    employee_name = summary.get("assignedUserName") or summary.get("staffName") or (f"Page {page_id}" if page_id else "Nhanh Vpage")
    employee_id = summary.get("assignedUserId") or summary.get("staffId") or (f"page_{page_id}" if page_id else "nhanh_vpage")
    channel_code = summary.get("channel")
    channel_name = channel_label(channel_code, page_id)
    metadata = {
        "source": "nhanh_vpage",
        "page_id": page_id,
        "customer_id": customer_id,
        "customer_name": summary.get("pageUserName"),
        "has_reply": summary.get("hasReply"),
        "status": summary.get("status"),
        "conversation_type": summary.get("type"),
        "channel_code": channel_code,
        "channel_label": channel_name,
        "last_message": summary.get("lastMessage"),
        "created_at": timestamp_to_iso(summary.get("createdAt")),
        "updated_at": timestamp_to_iso(summary.get("updatedAt")),
        "raw_summary": summary,
    }
    return {
        "external_id": conversation_id,
        "channel": f"nhanh_{channel_name.lower()}",
        "employee": {
            "id": str(employee_id),
            "name": str(employee_name),
        },
        "metadata": metadata,
        "messages": messages,
    }


def build_conversation(
    conversation_id: str,
    messages_payload: dict[str, Any],
    summary_item: dict[str, Any] | None = None,
) -> Conversation:
    return Conversation.from_dict(map_live_conversation(conversation_id, messages_payload, summary_item=summary_item))
