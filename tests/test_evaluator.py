import unittest
from pathlib import Path

from app.evaluator import evaluate_conversation
from app.schemas import load_conversations, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"


def _percent(result) -> float:
    if result.max_score <= 0:
        return 0.0
    return (result.total_score / result.max_score) * 100


class EvaluatorTests(unittest.TestCase):
    """v1.3.0: rule weights were rebalanced and channel-skip/conditional_keyword
    rules can change a conversation's effective_max_score, so absolute thresholds
    are no longer meaningful. Tests anchor on the resulting grade label and
    on `total_score / effective_max_score` percentage."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset = load_ruleset(RULESET_PATH)
        conversations = load_conversations(CONVERSATIONS_PATH)
        cls.by_id = {item.external_id: item for item in conversations}

    def test_hair_pass_grades_at_least_tot(self) -> None:
        result = evaluate_conversation(self.by_id["hair-pass-001"], self.ruleset)
        self.assertIn(result.grade, {"Xuất sắc", "Tốt"})
        self.assertGreaterEqual(_percent(result), 75)
        self.assertTrue(result.passed)
        self.assertFalse(result.blacklist_triggered)

    def test_hair_good_grades_good(self) -> None:
        result = evaluate_conversation(self.by_id["hair-good-001"], self.ruleset)
        self.assertEqual(result.grade, "Tốt")
        pct = _percent(result)
        self.assertGreaterEqual(pct, 75)
        self.assertLess(pct, 90)

    def test_hair_average_grades_trung_binh(self) -> None:
        result = evaluate_conversation(self.by_id["hair-average-001"], self.ruleset)
        self.assertEqual(result.grade, "Trung bình")
        pct = _percent(result)
        self.assertGreaterEqual(pct, 50)
        self.assertLess(pct, 75)
        self.assertFalse(result.passed)

    def test_blacklist_case_caps_score(self) -> None:
        result = evaluate_conversation(self.by_id["hair-blacklist-no-photo-001"], self.ruleset)
        self.assertTrue(result.blacklist_triggered)
        self.assertLessEqual(result.total_score, 49)
        self.assertEqual(result.grade, "Không đạt")

    def test_complaint_case_passes_complaint_flow(self) -> None:
        result = evaluate_conversation(self.by_id["hair-complaint-pass-001"], self.ruleset)
        complaint_finding = next(item for item in result.findings if item.rule_id == "complaint_flow")
        self.assertTrue(complaint_finding.passed)

    def test_effective_max_score_reflects_skipped_rules(self) -> None:
        """Sample convs use channel='pancake' which is NOT in
        sop_close_order.applies_to_channels (facebook, instagram). The rule
        should be skipped, dropping effective max by 7."""
        result = evaluate_conversation(self.by_id["hair-pass-001"], self.ruleset)
        # 105 (full) - 7 (sop_close_order skipped) - 1 (technical_instruction
        # skipped when no trigger) = 97
        self.assertEqual(result.max_score, 97)


if __name__ == "__main__":
    unittest.main()
