import unittest
from pathlib import Path

from app.evaluator import evaluate_conversation
from app.schemas import load_conversations, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"


class EvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset = load_ruleset(RULESET_PATH)
        conversations = load_conversations(CONVERSATIONS_PATH)
        cls.by_id = {item.external_id: item for item in conversations}

    def test_hair_pass_is_excellent(self) -> None:
        result = evaluate_conversation(self.by_id["hair-pass-001"], self.ruleset)
        self.assertGreaterEqual(result.total_score, 90)
        self.assertEqual(result.grade, "Xuất sắc")
        self.assertTrue(result.passed)
        self.assertFalse(result.blacklist_triggered)

    def test_hair_good_is_good(self) -> None:
        result = evaluate_conversation(self.by_id["hair-good-001"], self.ruleset)
        self.assertGreaterEqual(result.total_score, 75)
        self.assertLess(result.total_score, 90)
        self.assertEqual(result.grade, "Tốt")

    def test_hair_average_is_average(self) -> None:
        result = evaluate_conversation(self.by_id["hair-average-001"], self.ruleset)
        self.assertGreaterEqual(result.total_score, 50)
        self.assertLess(result.total_score, 75)
        self.assertEqual(result.grade, "Trung bình")
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


if __name__ == "__main__":
    unittest.main()
