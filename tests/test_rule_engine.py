import unittest
from pathlib import Path

from app.rule_engine import evaluate_rule
from app.schemas import Conversation, Rule, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"


def build_conversation(messages, metadata=None) -> Conversation:
    return Conversation.from_dict(
        {
            "external_id": "test-conv",
            "channel": "pancake",
            "metadata": metadata or {},
            "messages": messages,
        }
    )


class RuleEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset = load_ruleset(RULESET_PATH)

    def test_keyword_any_pass(self) -> None:
        conversation = build_conversation(
            [{"id": "m1", "sender_type": "employee", "text": "Dạ chào bạn, Buddy có thể hỗ trợ gì ạ?", "attachments": []}]
        )
        rule = Rule.from_dict(
            {
                "id": "r1",
                "name": "Greeting",
                "max_score": 5,
                "type": "keyword_any",
                "applies_to": "employee",
                "config": {"keywords": ["dạ chào", "hỗ trợ gì"]},
            }
        )
        self.assertTrue(evaluate_rule(conversation, rule).passed)

    def test_keyword_any_fail(self) -> None:
        conversation = build_conversation(
            [{"id": "m1", "sender_type": "employee", "text": "Chào bạn.", "attachments": []}]
        )
        rule = Rule.from_dict(
            {
                "id": "r1",
                "name": "Greeting",
                "max_score": 5,
                "type": "keyword_any",
                "applies_to": "employee",
                "config": {"keywords": ["buddy có thể hỗ trợ", "dạ chào"]},
            }
        )
        self.assertFalse(evaluate_rule(conversation, rule).passed)

    def test_employee_last_message_pass(self) -> None:
        conversation = build_conversation(
            [
                {"id": "m1", "sender_type": "customer", "text": "Cho mình hỏi giá.", "attachments": []},
                {"id": "m2", "sender_type": "employee", "text": "Dạ cảm ơn bạn ạ.", "attachments": []},
            ]
        )
        rule = Rule.from_dict(
            {
                "id": "r2",
                "name": "Closing",
                "max_score": 5,
                "type": "employee_last_message",
                "config": {"closing_keywords": ["cảm ơn"]},
            }
        )
        self.assertTrue(evaluate_rule(conversation, rule).passed)

    def test_employee_last_message_fail_when_customer_speaks_last(self) -> None:
        conversation = build_conversation(
            [
                {"id": "m1", "sender_type": "employee", "text": "Dạ em báo giá ạ.", "attachments": []},
                {"id": "m2", "sender_type": "customer", "text": "Ok bạn.", "attachments": []},
            ]
        )
        rule = Rule.from_dict(
            {
                "id": "r2",
                "name": "Closing",
                "max_score": 5,
                "type": "employee_last_message",
                "config": {"closing_keywords": ["cảm ơn"]},
            }
        )
        self.assertFalse(evaluate_rule(conversation, rule).passed)

    def test_max_emoji_per_message_fail(self) -> None:
        conversation = build_conversation(
            [{"id": "m1", "sender_type": "employee", "text": "Dạ ok 😊😊😊😊", "attachments": []}]
        )
        rule = Rule.from_dict(
            {
                "id": "r3",
                "name": "Emoji limit",
                "max_score": 2,
                "type": "max_emoji_per_message",
                "config": {"max_emoji": 3},
            }
        )
        self.assertFalse(evaluate_rule(conversation, rule).passed)

    def test_missing_required_before_advice_fail(self) -> None:
        conversation = build_conversation(
            [
                {"id": "m1", "sender_type": "employee", "text": "Mã màu 6.1 sẽ hợp với tóc bạn.", "attachments": []},
                {"id": "m2", "sender_type": "employee", "text": "Bạn gửi ảnh tóc hiện tại giúp mình nhé.", "attachments": []},
            ]
        )
        rule = Rule.from_dict(
            {
                "id": "r4",
                "name": "No photo before advice",
                "max_score": 0,
                "type": "missing_required_before_advice",
                "severity": "critical",
                "config": {
                    "required_keywords": ["ảnh tóc", "gửi ảnh"],
                    "advice_keywords": ["mã màu", "tỉ lệ pha", "lên màu"],
                },
            }
        )
        self.assertFalse(evaluate_rule(conversation, rule).passed)

    def test_photo_request_via_cam_phrase_should_not_trigger_false_blacklist(self) -> None:
        conversation = build_conversation(
            [
                {
                    "id": "m1",
                    "sender_type": "employee",
                    "text": "Dạ bạn chụp tóc qua cam thường rõ độ dài và nền tóc hiện tại để Buddy tư vấn kĩ hơn và xác định lượng thuốc bạn cần nha",
                    "attachments": [],
                },
                {
                    "id": "m2",
                    "sender_type": "customer",
                    "text": "nền cũng hơi sáng xíu ạ",
                    "attachments": [{"type": "image", "url": "https://example.com/hair.jpg"}],
                },
                {
                    "id": "m3",
                    "sender_type": "employee",
                    "text": "Dạ nền cậu không quá sáng thì lên màu không sáng đâu nha, nếu lên nâu lạnh thì cần oxy 9 ạ.",
                    "attachments": [],
                },
            ]
        )
        request_rule = next(
            rule
            for category in self.ruleset.categories
            for rule in category.rules
            if rule.id == "request_current_hair_photo"
        )
        blacklist_rule = next(rule for rule in self.ruleset.blacklist if rule.id == "no_current_hair_photo")
        self.assertTrue(evaluate_rule(conversation, request_rule).passed)
        self.assertTrue(evaluate_rule(conversation, blacklist_rule).passed)

    def test_customer_sent_image_before_advice_should_count_as_photo_available(self) -> None:
        conversation = build_conversation(
            [
                {
                    "id": "m1",
                    "sender_type": "customer",
                    "text": "",
                    "attachments": [{"type": "image", "url": "https://example.com/hair-2.jpg"}],
                },
                {
                    "id": "m2",
                    "sender_type": "employee",
                    "text": "Dạ nền mình nhuộm nâu khói sáng sẽ không lên màu chuẩn ạ, nếu lên thì cần oxy 9 ạ.",
                    "attachments": [],
                },
            ]
        )
        request_rule = next(
            rule
            for category in self.ruleset.categories
            for rule in category.rules
            if rule.id == "request_current_hair_photo"
        )
        blacklist_rule = next(rule for rule in self.ruleset.blacklist if rule.id == "no_current_hair_photo")
        self.assertTrue(evaluate_rule(conversation, request_rule).passed)
        self.assertTrue(evaluate_rule(conversation, blacklist_rule).passed)

    def test_complaint_flow_pass(self) -> None:
        conversation = build_conversation(
            [
                {"id": "m1", "sender_type": "customer", "text": "Mình không hài lòng màu lên bị lệch.", "attachments": []},
                {"id": "m2", "sender_type": "employee", "text": "Dạ em xin lỗi mình về trải nghiệm này ạ.", "attachments": []},
                {"id": "m3", "sender_type": "employee", "text": "Mình gửi ảnh thực tế giúp em nhé.", "attachments": []},
                {"id": "m4", "sender_type": "employee", "text": "Em kiểm tra nguyên nhân do nền tóc cũ còn tối ạ.", "attachments": []},
                {"id": "m5", "sender_type": "employee", "text": "Giải pháp là em hỗ trợ công thức chỉnh lại màu ạ.", "attachments": []},
            ],
            metadata={"has_complaint": True},
        )
        rule = Rule.from_dict(
            {
                "id": "r5",
                "name": "Complaint flow",
                "max_score": 12,
                "type": "complaint_flow",
                "config": {
                    "complaint_keywords": ["không hài lòng"],
                    "steps": [
                        {"name": "Xin lỗi", "keywords": ["xin lỗi"]},
                        {"name": "Ảnh thực tế", "keywords": ["ảnh thực tế"]},
                        {"name": "Nguyên nhân", "keywords": ["nguyên nhân"]},
                        {"name": "Giải pháp", "keywords": ["giải pháp"]},
                    ],
                },
            }
        )
        self.assertTrue(evaluate_rule(conversation, rule).passed)

    def test_metadata_flag_fail(self) -> None:
        conversation = build_conversation(
            [{"id": "m1", "sender_type": "employee", "text": "Giá 300k.", "attachments": []}],
            metadata={"flags": {"incorrect_product_info": True}},
        )
        rule = Rule.from_dict(
            {
                "id": "r6",
                "name": "Wrong product info",
                "max_score": 0,
                "type": "metadata_flag",
                "severity": "critical",
                "config": {"flag_name": "incorrect_product_info"},
            }
        )
        self.assertFalse(evaluate_rule(conversation, rule).passed)


if __name__ == "__main__":
    unittest.main()
