from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import copy
import hashlib
import json


@dataclass
class Attachment:
    """Cross-channel attachment. `metadata` carries channel-specific data
    (Shopee product_card, TikTok video_clip, Instagram story_reply, ...).
    """

    type: str
    name: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        return cls(
            type=str(data.get("type", "")),
            name=data.get("name"),
            url=data.get("url"),
            metadata=data.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type, "name": self.name, "url": self.url}
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass
class Message:
    id: str
    sender_type: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    sent_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        attachments = [Attachment.from_dict(item) for item in data.get("attachments", [])]
        return cls(
            id=str(data.get("id") or data.get("message_id") or ""),
            sender_type=str(data.get("sender_type", "")),
            text=str(data.get("text", "")),
            attachments=attachments,
            sent_at=data.get("sent_at") or data.get("timestamp"),
        )


@dataclass
class Employee:
    id: str
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Employee | None":
        if not data:
            return None
        employee_id = str(data.get("id", "")).strip()
        employee_name = str(data.get("name", "")).strip()
        if not employee_id and not employee_name:
            return None
        return cls(
            id=employee_id or "unknown_employee",
            name=employee_name or employee_id or "Unknown Employee",
        )


@dataclass
class Conversation:
    external_id: str
    channel: str
    employee: Employee | None
    metadata: dict[str, Any]
    messages: list[Message]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conversation":
        messages = [Message.from_dict(item) for item in data.get("messages", [])]
        return cls(
            external_id=str(data.get("external_id", "")),
            channel=str(data.get("channel", "")),
            employee=Employee.from_dict(data.get("employee")),
            metadata=data.get("metadata", {}) or {},
            messages=messages,
        )


@dataclass
class Rule:
    id: str
    name: str
    max_score: float
    type: str
    applies_to: str = "employee"
    severity: str = "medium"
    config: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            max_score=float(data.get("max_score", 0)),
            type=str(data["type"]),
            applies_to=str(data.get("applies_to", "employee")),
            severity=str(data.get("severity", "medium")),
            config=data.get("config", {}) or {},
            rationale=str(data.get("rationale", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "max_score": self.max_score,
            "type": self.type,
            "applies_to": self.applies_to,
            "severity": self.severity,
            "config": copy.deepcopy(self.config),
            "rationale": self.rationale,
        }


@dataclass
class RuleCategory:
    id: str
    name: str
    max_score: float
    rules: list[Rule]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleCategory":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            max_score=float(data.get("max_score", 0)),
            rules=[Rule.from_dict(item) for item in data.get("rules", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "max_score": self.max_score,
            "rules": [rule.to_dict() for rule in self.rules],
        }


@dataclass
class Ruleset:
    version: str
    max_score: float
    categories: list[RuleCategory]
    blacklist: list[Rule]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ruleset":
        return cls(
            version=str(data.get("version", "1.0.0")),
            max_score=float(data.get("max_score", 100)),
            categories=[RuleCategory.from_dict(item) for item in data.get("categories", [])],
            blacklist=[Rule.from_dict(item) for item in data.get("blacklist", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "max_score": self.max_score,
            "categories": [cat.to_dict() for cat in self.categories],
            "blacklist": [rule.to_dict() for rule in self.blacklist],
        }

    def compute_version(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


@dataclass
class Finding:
    rule_id: str
    rule_name: str
    passed: bool
    score: float
    max_score: float
    severity: str
    evidence_message_ids: list[str]
    evidence_text: str | None
    explanation: str
    suggestion: str | None = None
    rationale: str = ""
    # When a rule does not apply to a conversation (channel filter, or
    # conditional_keyword whose trigger never fired) the finding is reported
    # with `skipped=True` and `max_score=0` so it does not consume room in
    # the conversation's effective_max_score.
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CategoryScore:
    category_id: str
    name: str
    score: float
    max_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationResult:
    conversation_id: str
    total_score: float
    max_score: float
    grade: str
    passed: bool
    blacklist_triggered: bool
    category_scores: list[CategoryScore]
    findings: list[Finding]
    blacklist_findings: list[Finding]
    rules_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "grade": self.grade,
            "passed": self.passed,
            "blacklist_triggered": self.blacklist_triggered,
            "rules_version": self.rules_version,
            "category_scores": [item.to_dict() for item in self.category_scores],
            "findings": [item.to_dict() for item in self.findings],
            "blacklist_findings": [item.to_dict() for item in self.blacklist_findings],
        }


def load_ruleset(path: str | Path) -> Ruleset:
    with Path(path).open("r", encoding="utf-8") as file:
        data = json.load(file)
    return Ruleset.from_dict(data)


def load_conversations(path: str | Path) -> list[Conversation]:
    with Path(path).open("r", encoding="utf-8") as file:
        data = json.load(file)
    return [Conversation.from_dict(item) for item in data]
