from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rule_history import list_versions, load_ruleset_version, record_version
from app.schemas import Ruleset, load_ruleset


BASE_DIR = Path(__file__).resolve().parent.parent
RULESET_PATH = BASE_DIR / "data" / "rules.json"


def mutate_ruleset(ruleset: Ruleset) -> Ruleset:
    data = copy.deepcopy(ruleset.to_dict())
    data["categories"][0]["rules"][0]["config"]["keywords"] = ["__test_keyword_mutation__"]
    return Ruleset.from_dict(data)


class RuleVersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ruleset = load_ruleset(RULESET_PATH)
        self._tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self._tmp.name) / "rules_history.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_same_ruleset_same_version(self):
        v1 = self.ruleset.compute_version()
        v2 = load_ruleset(RULESET_PATH).compute_version()
        self.assertEqual(v1, v2)

    def test_different_ruleset_different_version(self):
        mutated = mutate_ruleset(self.ruleset)
        self.assertNotEqual(self.ruleset.compute_version(), mutated.compute_version())

    def test_history_records_new_version(self):
        v = record_version(self.ruleset, history_path=self.history_path)
        versions = list_versions(history_path=self.history_path)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], v)

    def test_history_dedupe(self):
        v1 = record_version(self.ruleset, history_path=self.history_path)
        v2 = record_version(self.ruleset, history_path=self.history_path)
        self.assertEqual(v1, v2)
        versions = list_versions(history_path=self.history_path)
        self.assertEqual(len(versions), 1)

    def test_history_records_two_distinct_versions(self):
        v1 = record_version(self.ruleset, history_path=self.history_path)
        mutated = mutate_ruleset(self.ruleset)
        v2 = record_version(mutated, history_path=self.history_path)
        self.assertNotEqual(v1, v2)
        versions = list_versions(history_path=self.history_path)
        self.assertEqual(len(versions), 2)

    def test_load_historical_version(self):
        v = record_version(self.ruleset, history_path=self.history_path)
        loaded = load_ruleset_version(v, history_path=self.history_path)
        self.assertEqual(loaded.compute_version(), v)
        self.assertEqual(loaded.to_dict(), self.ruleset.to_dict())


if __name__ == "__main__":
    unittest.main()
