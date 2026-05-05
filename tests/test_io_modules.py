"""Tests cho importer, exporter, response_time, review_store."""
import csv
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import exporter, importer, response_time, review_store


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write_csv(self, rows, name="in.csv"):
        path = self.tmp / name
        with path.open("w", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["conversation_id", "channel", "employee_id", "employee_name", "sender_type", "text", "sent_at"])
            for r in rows:
                w.writerow(r)
        return path

    def test_csv_groups_messages_by_conversation(self):
        path = self._write_csv([
            ["c1", "pancake", "e1", "A", "customer", "hi", "2026-05-04T10:00:00+07:00"],
            ["c1", "pancake", "e1", "A", "employee", "hello", "2026-05-04T10:01:00+07:00"],
            ["c2", "zalo", "e2", "B", "customer", "ahoy", "2026-05-04T11:00:00+07:00"],
        ])
        out = self.tmp / "out.json"
        result = importer.import_file(path, out, append=False)
        self.assertEqual(result["added"], 2)
        with out.open() as f:
            data = json.load(f)
        ids = {c["external_id"] for c in data}
        self.assertEqual(ids, {"c1", "c2"})
        c1 = [c for c in data if c["external_id"] == "c1"][0]
        self.assertEqual(len(c1["messages"]), 2)

    def test_invalid_sender_type_returns_error(self):
        path = self._write_csv([
            ["c1", "pancake", "e1", "A", "robot", "weird", ""],
        ])
        out = self.tmp / "out.json"
        result = importer.import_file(path, out, append=False)
        self.assertTrue(result["errors"])
        self.assertIn("sender_type", result["errors"][0])

    def test_missing_required_column_raises(self):
        path = self.tmp / "bad.csv"
        with path.open("w", encoding="utf-8") as f:
            f.write("only_one_column\nfoo\n")
        with self.assertRaises(ValueError):
            importer.import_file(path, self.tmp / "out.json", append=False)

    def test_unsupported_extension_raises(self):
        path = self.tmp / "data.txt"
        path.write_text("hello")
        with self.assertRaises(ValueError):
            importer.import_file(path, self.tmp / "out.json", append=False)

    def test_json_import_roundtrip(self):
        in_path = self.tmp / "in.json"
        in_path.write_text(json.dumps([{
            "external_id": "c1",
            "channel": "pancake",
            "employee": {"id": "e1", "name": "A"},
            "metadata": {},
            "messages": [{"id": "m1", "sender_type": "customer", "text": "x", "sent_at": ""}],
        }]), encoding="utf-8")
        out = self.tmp / "out.json"
        result = importer.import_file(in_path, out, append=False)
        self.assertEqual(result["added"], 1)

    def test_append_skips_duplicates(self):
        path = self._write_csv([
            ["c1", "pancake", "e1", "A", "customer", "hi", ""],
        ])
        out = self.tmp / "out.json"
        importer.import_file(path, out, append=False)
        result = importer.import_file(path, out, append=True)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 1)


class ExporterTests(unittest.TestCase):
    def _make_conv(self, conv_id, employee_name="A", channel="pancake", date_str="2026-05-04"):
        return SimpleNamespace(
            external_id=conv_id,
            channel=channel,
            employee=SimpleNamespace(id="e1", name=employee_name),
            messages=[SimpleNamespace(sent_at=f"{date_str}T10:00:00+07:00")],
        )

    def _make_result(self, conv_id, score=80.0, blacklist=False):
        return {
            "conversation_id": conv_id,
            "total_score": score,
            "max_score": 100.0,
            "grade": "Tốt",
            "passed": score >= 75 and not blacklist,
            "blacklist_triggered": blacklist,
            "findings": [
                {"rule_id": "r1", "rule_name": "Rule A", "passed": False, "score": 0,
                 "max_score": 5, "severity": "medium", "evidence_message_ids": [],
                 "evidence_text": None, "explanation": "x"},
            ],
            "blacklist_findings": [],
        }

    def test_results_csv_includes_failed_rules_column(self):
        results = [self._make_result("c1", 80)]
        convs = [self._make_conv("c1")]
        out = exporter.export_results_csv(results, convs)
        reader = csv.DictReader(io.StringIO(out))
        rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conversation_id"], "c1")
        self.assertIn("Rule A", rows[0]["failed_rules"])

    def test_scorecard_csv_columns(self):
        scorecards = [{
            "employee_id": "e1", "employee_name": "A",
            "conversation_count": 5, "average_score": 82.0,
            "grade": "Tốt", "passed_rate": 0.8, "blacklist_count": 0,
            "top_failed_skills": [{"skill": "attitude_style", "failed_count": 2}],
        }]
        out = exporter.export_scorecard_csv(scorecards)
        rows = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual(rows[0]["employee_name"], "A")
        self.assertEqual(rows[0]["passed_rate_pct"], "80.0")
        self.assertEqual(rows[0]["top_weak_skill"], "attitude_style")


class ResponseTimeTests(unittest.TestCase):
    def _make_conv(self, messages):
        return SimpleNamespace(messages=[
            SimpleNamespace(sender_type=t, sent_at=ts) for t, ts in messages
        ])

    def test_no_messages_returns_empty(self):
        conv = SimpleNamespace(messages=[])
        rt = response_time.compute_response_times(conv)
        self.assertIsNone(rt["avg_response_seconds"])
        self.assertEqual(rt["response_count"], 0)

    def test_basic_response_time(self):
        conv = self._make_conv([
            ("customer", "2026-05-04T10:00:00+07:00"),
            ("employee", "2026-05-04T10:01:00+07:00"),  # 60s
            ("customer", "2026-05-04T10:05:00+07:00"),
            ("employee", "2026-05-04T10:08:00+07:00"),  # 180s
        ])
        rt = response_time.compute_response_times(conv)
        self.assertEqual(rt["response_count"], 2)
        self.assertEqual(rt["avg_response_seconds"], 120.0)
        self.assertEqual(rt["max_response_seconds"], 180.0)

    def test_unanswered_customer_message_counted(self):
        conv = self._make_conv([
            ("customer", "2026-05-04T10:00:00+07:00"),
            ("employee", "2026-05-04T10:01:00+07:00"),
            ("customer", "2026-05-04T10:05:00+07:00"),
        ])
        rt = response_time.compute_response_times(conv)
        self.assertEqual(rt["unanswered_customer_messages"], 1)

    def test_format_seconds_human_friendly(self):
        self.assertEqual(response_time.format_seconds(45), "45s")
        self.assertEqual(response_time.format_seconds(125), "2m 5s")
        self.assertEqual(response_time.format_seconds(3700), "1h 1m")
        self.assertEqual(response_time.format_seconds(None), "-")


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(review_store, "REVIEWS_DIR", self.tmp).start()

    def tearDown(self):
        shutil.rmtree(self.tmp)
        mock.patch.stopall()

    def test_save_and_load_roundtrip(self):
        review_store.save_review("c1", "QA Lead", "Cần cải thiện chốt đơn", score_override=70.0)
        loaded = review_store.load_review("c1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.note, "Cần cải thiện chốt đơn")
        self.assertEqual(loaded.score_override, 70.0)
        self.assertEqual(loaded.reviewer, "QA Lead")

    def test_load_review_missing_returns_none(self):
        self.assertIsNone(review_store.load_review("not-exist"))

    def test_load_all_reviews(self):
        review_store.save_review("c1", "QA", "n1")
        review_store.save_review("c2", "QA", "n2", score_override=50)
        reviews = review_store.load_all_reviews()
        self.assertEqual(set(reviews.keys()), {"c1", "c2"})

    def test_delete_review(self):
        review_store.save_review("c1", "QA", "n1")
        self.assertTrue(review_store.delete_review("c1"))
        self.assertFalse(review_store.delete_review("c1"))


if __name__ == "__main__":
    unittest.main()
