import unittest
from pathlib import Path

from app.evaluator import evaluate_conversation
from app.schemas import load_conversations, load_ruleset
from app.training import (
    load_training_modules,
    map_findings_to_skill_gaps,
    recommend_training_for_employee,
    recommend_training_for_evaluation,
)


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"
TRAINING_PATH = BASE_DIR / "data" / "training_modules.json"


class TrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset = load_ruleset(RULESET_PATH)
        cls.conversations = load_conversations(CONVERSATIONS_PATH)
        cls.by_id = {item.external_id: item for item in cls.conversations}
        cls.training_modules = load_training_modules(TRAINING_PATH)

    def test_failed_finding_maps_to_skill(self) -> None:
        result = evaluate_conversation(self.by_id["hair-average-001"], self.ruleset)
        gaps = map_findings_to_skill_gaps(result)
        self.assertTrue(any(item["skill"] == "hair_analysis" for item in gaps))

    def test_blacklist_recommendation_priority_high(self) -> None:
        result = evaluate_conversation(self.by_id["hair-blacklist-no-photo-001"], self.ruleset)
        recommendations = recommend_training_for_evaluation(result, self.training_modules)
        hair_analysis = next(item for item in recommendations if item["skill"] == "hair_analysis")
        self.assertEqual(hair_analysis["priority"], "high")

    def test_training_recommendation_returns_correct_module(self) -> None:
        result = evaluate_conversation(self.by_id["hair-average-001"], self.ruleset)
        recommendations = recommend_training_for_evaluation(result, self.training_modules)
        target = next(item for item in recommendations if item["skill"] == "product_consulting")
        self.assertEqual(target["recommended_module_id"], "training_product_consulting")

    def test_employee_without_failed_findings_has_no_training(self) -> None:
        result = evaluate_conversation(self.by_id["hair-pass-001"], self.ruleset)
        recommendations = recommend_training_for_employee([result], self.training_modules)
        self.assertEqual(recommendations, [])


if __name__ == "__main__":
    unittest.main()
