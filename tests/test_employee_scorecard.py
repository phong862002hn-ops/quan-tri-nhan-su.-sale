import unittest
from pathlib import Path

from app.employee_scorecard import build_employee_scorecards
from app.evaluator import evaluate_conversation
from app.schemas import load_conversations, load_ruleset
from app.training import load_training_modules


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"
CONVERSATIONS_PATH = BASE_DIR / "data" / "sample_conversations.json"
TRAINING_PATH = BASE_DIR / "data" / "training_modules.json"


class EmployeeScorecardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset = load_ruleset(RULESET_PATH)
        cls.conversations = load_conversations(CONVERSATIONS_PATH)
        cls.training_modules = load_training_modules(TRAINING_PATH)
        cls.results = [evaluate_conversation(item, cls.ruleset) for item in cls.conversations]
        cls.scorecards = build_employee_scorecards(cls.conversations, cls.results, cls.training_modules)
        cls.by_employee = {item["employee_id"]: item for item in cls.scorecards}

    def test_employee_average_score_correct(self) -> None:
        scorecard = self.by_employee["emp_002"]
        self.assertEqual(scorecard["average_score"], 49.0)

    def test_top_failed_skill_correct(self) -> None:
        scorecard = self.by_employee["emp_002"]
        self.assertEqual(scorecard["top_failed_skills"][0]["skill"], "sop_compliance")

    def test_employee_training_recommendation_exists(self) -> None:
        scorecard = self.by_employee["emp_002"]
        self.assertTrue(any(item["skill"] == "hair_analysis" for item in scorecard["training_recommendations"]))

    def test_employee_without_failed_findings_has_no_training(self) -> None:
        scorecard = self.by_employee["emp_001"]
        self.assertEqual(scorecard["training_recommendations"], [])


if __name__ == "__main__":
    unittest.main()
