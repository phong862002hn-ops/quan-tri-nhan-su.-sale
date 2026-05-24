from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app import cache_db, lead_review


class LeadReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "cache.db"
        cache_db.init_db(self.db)

    def tearDown(self) -> None:
        for key, conn in list(cache_db._connections.items()):
            conn.close()
            cache_db._connections.pop(key, None)
        self.tmp.cleanup()

    def _make_pending(self, conv_id: str = "c1", shift_id: str = "morning") -> None:
        lead_review.upsert_pending_review(
            self.db, conv_id, shift_id, "2026-05-22",
            app_score=80, app_max_score=97, app_blacklist=False,
            snapshot={"total_score": 80, "max_score": 97, "findings": []},
            last_message_at=1700000000,
        )

    def test_pending_created(self) -> None:
        self._make_pending()
        row = lead_review.get_review(self.db, "c1")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["app_score"], 80.0)
        self.assertIsNone(row["lead_score"])

    def test_upsert_is_idempotent_does_not_overwrite_review(self) -> None:
        self._make_pending()
        lead_review.submit_lead_review(
            self.db, "c1", lead_score=85, lead_blacklist=False,
            lead_comment="Đã review", status="edited",
        )
        # Re-upserting should NOT roll back to 'pending'.
        self._make_pending()
        row = lead_review.get_review(self.db, "c1")
        self.assertEqual(row["status"], "edited")
        self.assertEqual(row["lead_score"], 85.0)

    def test_submit_confirm(self) -> None:
        self._make_pending()
        lead_review.submit_lead_review(
            self.db, "c1", lead_score=80, lead_blacklist=False,
            lead_comment="OK", status="confirmed",
        )
        row = lead_review.get_review(self.db, "c1")
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["lead_score"], 80.0)
        self.assertIsNotNone(row["reviewed_at"])

    def test_submit_edit_changes_score(self) -> None:
        self._make_pending()
        lead_review.submit_lead_review(
            self.db, "c1", lead_score=65, lead_blacklist=True,
            lead_comment="App chấm cao", status="edited",
        )
        row = lead_review.get_review(self.db, "c1")
        self.assertEqual(row["status"], "edited")
        self.assertEqual(row["lead_score"], 65.0)
        self.assertEqual(row["lead_blacklist"], 1)

    def test_submit_skipped(self) -> None:
        self._make_pending()
        lead_review.submit_lead_review(
            self.db, "c1", lead_score=None, lead_blacklist=False,
            lead_comment="", status="skipped",
        )
        row = lead_review.get_review(self.db, "c1")
        self.assertEqual(row["status"], "skipped")
        self.assertIsNone(row["lead_score"])

    def test_invalid_status_raises(self) -> None:
        self._make_pending()
        with self.assertRaises(ValueError):
            lead_review.submit_lead_review(
                self.db, "c1", lead_score=80, lead_blacklist=False,
                lead_comment="", status="random",
            )

    def test_shift_progress(self) -> None:
        for i in range(5):
            self._make_pending(conv_id=f"c{i}")
        lead_review.submit_lead_review(
            self.db, "c0", lead_score=80, lead_blacklist=False, lead_comment="", status="confirmed",
        )
        lead_review.submit_lead_review(
            self.db, "c1", lead_score=70, lead_blacklist=False, lead_comment="", status="edited",
        )
        progress = lead_review.get_shift_progress(self.db, "2026-05-22", "morning")
        self.assertEqual(progress["total"], 5)
        self.assertEqual(progress["pending"], 3)
        self.assertEqual(progress["confirmed"], 1)
        self.assertEqual(progress["edited"], 1)
        self.assertEqual(progress["done"], 2)
        self.assertEqual(progress["progress_percent"], 40)

    def test_recent_shifts_summary_groups_by_date_and_shift(self) -> None:
        lead_review.upsert_pending_review(
            self.db, "c-a", "morning", "2026-05-22",
            app_score=80, app_max_score=97, app_blacklist=False,
            snapshot=None, last_message_at=None,
        )
        lead_review.upsert_pending_review(
            self.db, "c-b", "afternoon", "2026-05-22",
            app_score=70, app_max_score=97, app_blacklist=False,
            snapshot=None, last_message_at=None,
        )
        summary = lead_review.get_recent_shifts_summary(self.db)
        keys = {(row["shift_date"], row["shift_id"]) for row in summary}
        self.assertEqual(keys, {("2026-05-22", "morning"), ("2026-05-22", "afternoon")})

    def test_list_reviews_for_shift(self) -> None:
        for i in range(3):
            self._make_pending(conv_id=f"x{i}", shift_id="evening")
        rows = lead_review.list_reviews_for_shift(self.db, "2026-05-22", "evening")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "pending" for r in rows))


if __name__ == "__main__":
    unittest.main()
