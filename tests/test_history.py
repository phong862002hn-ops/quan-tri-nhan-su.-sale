import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from app import history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.history_dir = self.tmp / "history"
        self.demo_dir = self.tmp / "history_demo"
        self.history_dir.mkdir()
        self.demo_dir.mkdir()
        self._patch_dirs()

    def tearDown(self):
        shutil.rmtree(self.tmp)
        mock.patch.stopall()

    def _patch_dirs(self):
        mock.patch.object(history, "HISTORY_DIR", self.history_dir).start()
        mock.patch.object(history, "HISTORY_DEMO_DIR", self.demo_dir).start()

    def _make_conversation(self, conv_id, employee_id="emp_x", name="Nhân viên X"):
        from types import SimpleNamespace
        return SimpleNamespace(
            external_id=conv_id,
            employee=SimpleNamespace(id=employee_id, name=name),
        )

    def _make_result(self, conv_id, score=80.0):
        return {
            "conversation_id": conv_id,
            "total_score": score,
            "max_score": 100,
            "grade": "Tốt",
            "passed": score >= 75,
            "blacklist_triggered": False,
            "category_scores": [],
            "findings": [],
            "blacklist_findings": [],
        }

    def test_save_creates_file_with_metadata(self):
        conv = self._make_conversation("c1")
        result = self._make_result("c1", 95.0)
        path = history.save_evaluation_results([result], [conv], date_str="2026-05-04")
        self.assertTrue(path.exists())
        with path.open() as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["evaluation"]["conversation_id"], "c1")
        self.assertEqual(data[0]["employee"]["id"], "emp_x")
        self.assertEqual(data[0]["date"], "2026-05-04")

    def test_save_dedupes_by_conversation_id(self):
        conv = self._make_conversation("c1")
        history.save_evaluation_results([self._make_result("c1", 80)], [conv], date_str="2026-05-04")
        history.save_evaluation_results([self._make_result("c1", 90)], [conv], date_str="2026-05-04")
        with (self.history_dir / "2026-05-04.json").open() as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["evaluation"]["total_score"], 80)

    def test_load_history_range(self):
        conv = self._make_conversation("c1")
        history.save_evaluation_results([self._make_result("c1")], [conv], date_str="2026-05-01")
        history.save_evaluation_results([self._make_result("c2")], [conv], date_str="2026-05-05")
        history.save_evaluation_results([self._make_result("c3")], [conv], date_str="2026-05-10")
        entries = history.load_history_range("2026-05-04", "2026-05-08")
        ids = [e["evaluation"]["conversation_id"] for e in entries]
        self.assertEqual(ids, ["c2"])

    def test_demo_dir_loaded_when_env_on(self):
        conv = self._make_conversation("c-demo")
        # Manually write demo file
        demo_entry = [{
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "date": "2026-04-28",
            "employee": {"id": "emp_demo", "name": "Demo"},
            "evaluation": self._make_result("c-demo"),
        }]
        with (self.demo_dir / "2026-04-28.json").open("w", encoding="utf-8") as f:
            json.dump(demo_entry, f)

        with mock.patch.dict(os.environ, {"QA_INCLUDE_DEMO": "1"}):
            entries = history.load_all_history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["evaluation"]["conversation_id"], "c-demo")

    def test_demo_dir_skipped_when_env_off(self):
        demo_entry = [{
            "saved_at": "2026-04-28",
            "date": "2026-04-28",
            "employee": {"id": "emp_demo", "name": "Demo"},
            "evaluation": self._make_result("c-demo"),
        }]
        with (self.demo_dir / "2026-04-28.json").open("w", encoding="utf-8") as f:
            json.dump(demo_entry, f)

        with mock.patch.dict(os.environ, {"QA_INCLUDE_DEMO": "0"}):
            entries = history.load_all_history()
        self.assertEqual(entries, [])

    def test_build_employee_trend_aggregates_by_date(self):
        history_data = [
            {"date": "2026-05-01", "employee": {"id": "e1", "name": "A"},
             "evaluation": {"total_score": 80}},
            {"date": "2026-05-01", "employee": {"id": "e1", "name": "A"},
             "evaluation": {"total_score": 90}},
            {"date": "2026-05-02", "employee": {"id": "e1", "name": "A"},
             "evaluation": {"total_score": 70}},
        ]
        trend = history.build_employee_trend(history_data)
        self.assertIn("e1", trend)
        self.assertEqual(len(trend["e1"]), 2)
        self.assertEqual(trend["e1"][0]["avg_score"], 85.0)
        self.assertEqual(trend["e1"][0]["count"], 2)
        self.assertEqual(trend["e1"][1]["avg_score"], 70.0)


if __name__ == "__main__":
    unittest.main()
