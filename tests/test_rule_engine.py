import unittest
from pathlib import Path

from app.rule_engine import evaluate_rule, keyword_in_text
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
        self.assertTrue(evaluate_rule(conversation, request_rule).passed)

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

    # Note: tests for `no_current_hair_photo` blacklist rule were removed when
    # that rule was retired in rules v1.3.0 (the equivalent behavior is now
    # graded by `request_current_hair_photo` in the regular ruleset, not as a
    # blacklist auto-fail).

    def test_keyword_word_boundary_single_word(self) -> None:
        self.assertTrue(keyword_in_text("dạ chào mình oke nhé", "oke"))
        self.assertFalse(keyword_in_text("tôi chơi poker game", "oke"))
        self.assertFalse(keyword_in_text("hôm nay đẹp trời", "om"))

    def test_keyword_word_boundary_multi_word(self) -> None:
        self.assertTrue(keyword_in_text("tại chị nói vậy đúng", "tại chị"))
        self.assertFalse(keyword_in_text("thợ tại chỗ chị làm sai", "tại chị"))

    def test_keyword_accent_insensitive(self) -> None:
        self.assertTrue(keyword_in_text("Dạ chào", "da chao"))
        self.assertTrue(keyword_in_text("nền tóc", "nen toc"))

    def test_customer_thanks_at_end_passes(self) -> None:
        conversation = build_conversation([
            {"id": "m1", "sender_type": "customer", "text": "shop có mã 6.34 không", "attachments": []},
            {"id": "m2", "sender_type": "employee", "text": "dạ có nha, 300k/bộ", "attachments": []},
            {"id": "m3", "sender_type": "customer", "text": "ok cảm ơn shop", "attachments": []},
        ])
        rule = next(r for r in self.ruleset.blacklist if r.id == "customer_unanswered")
        self.assertTrue(evaluate_rule(conversation, rule).passed)

    def test_customer_question_at_end_fails(self) -> None:
        conversation = build_conversation([
            {"id": "m1", "sender_type": "employee", "text": "dạ chào mình", "attachments": []},
            {"id": "m2", "sender_type": "customer", "text": "shop ơi có mã đỏ không", "attachments": []},
        ])
        rule = next(r for r in self.ruleset.blacklist if r.id == "customer_unanswered")
        self.assertFalse(evaluate_rule(conversation, rule).passed)

    def test_price_disclosure_pass_with_amount(self) -> None:
        conversation = build_conversation([
            {"id": "m1", "sender_type": "customer", "text": "mã 6.34 bao nhiêu", "attachments": []},
            {"id": "m2", "sender_type": "employee", "text": "dạ giá 300k/bộ nha mình", "attachments": []},
        ])
        rule = next(
            rule for cat in self.ruleset.categories for rule in cat.rules
            if rule.id == "price_disclosure"
        )
        self.assertTrue(evaluate_rule(conversation, rule).passed)

    def test_price_disclosure_fail_no_price_info(self) -> None:
        conversation = build_conversation([
            {"id": "m1", "sender_type": "customer", "text": "mã 6.34 có không", "attachments": []},
            {"id": "m2", "sender_type": "employee", "text": "dạ có mã đó nha", "attachments": []},
        ])
        rule = next(
            rule for cat in self.ruleset.categories for rule in cat.rules
            if rule.id == "price_disclosure"
        )
        self.assertFalse(evaluate_rule(conversation, rule).passed)

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


class ConditionalKeywordTests(unittest.TestCase):
    """Rule type added in v1.3.0 for `technical_instruction`."""

    def _build_rule(self) -> Rule:
        return Rule.from_dict({
            "id": "technical_instruction",
            "name": "Hướng dẫn kỹ thuật (nếu khách hỏi)",
            "max_score": 1,
            "type": "conditional_keyword",
            "applies_to": "employee",
            "severity": "low",
            "config": {
                "trigger_keywords": ["tỉ lệ", "ủ bao lâu"],
                "response_keywords": ["tỉ lệ", "phút", "ủ"],
                "fail_suggestion": "Hướng dẫn rõ.",
                "skip_if_not_triggered": True,
            },
        })

    def test_not_triggered_returns_skipped(self) -> None:
        conv = build_conversation([
            {"id": "m1", "sender_type": "customer", "text": "Em mua thuốc nhuộm số 7 ạ",
             "attachments": [], "sent_at": "2026-05-22T10:00:00"},
            {"id": "m2", "sender_type": "employee", "text": "Dạ ok bạn.",
             "attachments": [], "sent_at": "2026-05-22T10:01:00"},
        ])
        finding = evaluate_rule(conv, self._build_rule())
        self.assertTrue(finding.skipped)
        self.assertEqual(finding.max_score, 0)
        self.assertEqual(finding.score, 0)
        self.assertTrue(finding.passed)  # passed=True so it doesn't show as a failure

    def test_triggered_and_responded_passes(self) -> None:
        conv = build_conversation([
            {"id": "m1", "sender_type": "customer", "text": "Pha tỉ lệ bao nhiêu ạ",
             "attachments": [], "sent_at": "2026-05-22T10:00:00"},
            {"id": "m2", "sender_type": "employee",
             "text": "Tỉ lệ 1:1.5 ạ, ủ 30 phút nhé.",
             "attachments": [], "sent_at": "2026-05-22T10:01:00"},
        ])
        finding = evaluate_rule(conv, self._build_rule())
        self.assertFalse(finding.skipped)
        self.assertTrue(finding.passed)
        self.assertEqual(finding.score, 1)
        self.assertEqual(finding.max_score, 1)

    def test_triggered_without_response_fails(self) -> None:
        conv = build_conversation([
            {"id": "m1", "sender_type": "customer", "text": "Ủ bao lâu ạ",
             "attachments": [], "sent_at": "2026-05-22T10:00:00"},
            {"id": "m2", "sender_type": "employee", "text": "Dạ shop cảm ơn ạ.",
             "attachments": [], "sent_at": "2026-05-22T10:01:00"},
        ])
        finding = evaluate_rule(conv, self._build_rule())
        self.assertFalse(finding.skipped)
        self.assertFalse(finding.passed)
        self.assertEqual(finding.max_score, 1)


class ChannelFilterTests(unittest.TestCase):
    def _build_rule(self) -> Rule:
        return Rule.from_dict({
            "id": "sop_close_order",
            "name": "Bước 4 - Chốt đơn",
            "max_score": 7,
            "type": "keyword_all",
            "applies_to": "employee",
            "severity": "medium",
            "config": {
                "keywords": ["ship", "tổng tiền", "thanh toán"],
                "applies_to_channels": ["facebook", "instagram"],
                "skip_for_channels": ["shopee", "tiktok", "lazada"],
                "skip_reason": "Channel sàn, không cần chốt đơn.",
            },
        })

    def _conv(self, channel: str) -> Conversation:
        return Conversation.from_dict({
            "external_id": "c1",
            "channel": channel,
            "metadata": {},
            "messages": [
                {"id": "m1", "sender_type": "employee", "text": "Dạ chào bạn",
                 "attachments": [], "sent_at": "2026-05-22T10:00:00"},
            ],
        })

    def test_shopee_is_skipped(self) -> None:
        finding = evaluate_rule(self._conv("nhanh_shopee"), self._build_rule())
        self.assertTrue(finding.skipped)
        self.assertEqual(finding.max_score, 0)

    def test_tiktok_is_skipped(self) -> None:
        finding = evaluate_rule(self._conv("nhanh_tiktok"), self._build_rule())
        self.assertTrue(finding.skipped)

    def test_lazada_is_skipped(self) -> None:
        finding = evaluate_rule(self._conv("lazada"), self._build_rule())
        self.assertTrue(finding.skipped)

    def test_facebook_is_applied(self) -> None:
        finding = evaluate_rule(self._conv("nhanh_facebook"), self._build_rule())
        # Rule applies, evaluation runs and fails (no keywords match) → not skipped.
        self.assertFalse(finding.skipped)
        self.assertEqual(finding.max_score, 7)
        self.assertFalse(finding.passed)

    def test_instagram_is_applied(self) -> None:
        finding = evaluate_rule(self._conv("instagram"), self._build_rule())
        self.assertFalse(finding.skipped)


if __name__ == "__main__":
    unittest.main()
