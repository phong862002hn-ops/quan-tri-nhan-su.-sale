import json
import tempfile
import time
import unittest
from pathlib import Path

from app import cache_db


class CacheDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.db"
        cache_db.init_db(self.path)

    def tearDown(self) -> None:
        # Close per-thread connections so the tmpdir can be cleaned on Windows too.
        for key, conn in list(cache_db._connections.items()):
            conn.close()
            cache_db._connections.pop(key, None)
        self.tmp.cleanup()

    def _sample_conv(self, conv_id: str, updated_at: int, page_id: str = "p1", channel: str = "1") -> dict:
        return {
            "id": conv_id,
            "pageId": page_id,
            "channel": channel,
            "pageUserName": f"Customer {conv_id}",
            "updatedAt": updated_at,
        }

    def test_schema_init_creates_all_tables(self) -> None:
        import sqlite3
        conn = sqlite3.connect(str(self.path))
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        self.assertIn("conversations", names)
        self.assertIn("messages", names)
        self.assertIn("evaluations", names)
        self.assertIn("sync_state", names)

    def test_upsert_conversation_is_idempotent(self) -> None:
        item = self._sample_conv("c1", updated_at=1700000000)
        cache_db.upsert_conversation(self.path, item)
        cache_db.upsert_conversation(self.path, item)
        self.assertEqual(cache_db.conversation_count(self.path), 1)

    def test_upsert_conversation_updates_existing(self) -> None:
        cache_db.upsert_conversation(self.path, self._sample_conv("c1", updated_at=1000))
        cache_db.upsert_conversation(self.path, self._sample_conv("c1", updated_at=2000))
        items = cache_db.list_recent_conversations(self.path, date_filter="all", limit=10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["updatedAt"], 2000)

    def test_upsert_messages_and_get_back(self) -> None:
        msgs = [
            {"id": "m1", "createdAt": 100, "message": "hi", "senderName": "A"},
            {"id": "m2", "createdAt": 200, "message": "yo", "senderName": "B"},
        ]
        cache_db.upsert_messages(self.path, "c1", msgs)
        cache_db.upsert_messages(self.path, "c1", msgs)  # idempotent
        loaded = cache_db.get_messages(self.path, "c1")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["id"], "m1")

    def test_evaluation_roundtrip(self) -> None:
        result = {
            "total_score": 85.0,
            "max_score": 100.0,
            "grade": "Tốt",
            "blacklist_triggered": False,
            "findings": [{"passed": True}, {"passed": False}],
            "blacklist_findings": [],
        }
        cache_db.upsert_evaluation(self.path, "c1", "v1", result)
        loaded = cache_db.get_evaluation(self.path, "c1", "v1")
        self.assertEqual(loaded["total_score"], 85.0)
        self.assertIsNone(cache_db.get_evaluation(self.path, "c1", "different_version"))

    def test_list_recent_with_date_filter(self) -> None:
        now = int(time.time())
        cache_db.upsert_conversation(self.path, self._sample_conv("old", updated_at=now - 86400 * 40))
        cache_db.upsert_conversation(self.path, self._sample_conv("recent", updated_at=now - 3600))
        all_items = cache_db.list_recent_conversations(self.path, date_filter="all", limit=10)
        self.assertEqual(len(all_items), 2)
        week_items = cache_db.list_recent_conversations(self.path, date_filter="7days", limit=10)
        ids = {item["id"] for item in week_items}
        self.assertIn("recent", ids)
        self.assertNotIn("old", ids)

    def test_list_recent_includes_qa_summary_when_evaluation_exists(self) -> None:
        cache_db.upsert_conversation(self.path, self._sample_conv("c1", updated_at=int(time.time())))
        cache_db.upsert_evaluation(self.path, "c1", "v1", {
            "total_score": 90.0,
            "max_score": 100.0,
            "grade": "Xuất sắc",
            "blacklist_triggered": False,
            "findings": [{"passed": False}, {"passed": True}],
            "blacklist_findings": [],
        })
        items = cache_db.list_recent_conversations(self.path, date_filter="all", limit=10)
        self.assertEqual(len(items), 1)
        qa = items[0]["qa_summary"]
        self.assertEqual(qa["grade"], "Xuất sắc")
        self.assertEqual(qa["failed_rule_count"], 1)

    def test_sync_state_set_and_get(self) -> None:
        cache_db.set_sync_state(self.path, "last_sync_ts", "1700000000")
        self.assertEqual(cache_db.get_sync_state(self.path, "last_sync_ts"), "1700000000")
        cache_db.set_sync_state(self.path, "last_sync_ts", None)
        self.assertIsNone(cache_db.get_sync_state(self.path, "last_sync_ts"))

    def test_channel_filter(self) -> None:
        now = int(time.time())
        cache_db.upsert_conversation(self.path, self._sample_conv("a", now, channel="1"))
        cache_db.upsert_conversation(self.path, self._sample_conv("b", now, channel="9"))
        only_fb = cache_db.list_recent_conversations(self.path, date_filter="all", channel_filter="1", limit=10)
        self.assertEqual([i["id"] for i in only_fb], ["a"])

    def test_state_filter_graded_vs_active(self) -> None:
        now = int(time.time())
        cache_db.upsert_conversation(self.path, self._sample_conv("graded", now))
        cache_db.upsert_conversation(self.path, self._sample_conv("active", now))
        cache_db.upsert_evaluation(self.path, "graded", "v1", {
            "total_score": 80, "max_score": 100, "grade": "Tốt",
            "blacklist_triggered": False, "findings": [], "blacklist_findings": [],
        })
        graded = cache_db.list_recent_conversations(self.path, date_filter="all", state_filter="graded", limit=10)
        self.assertEqual([i["id"] for i in graded], ["graded"])
        active = cache_db.list_recent_conversations(self.path, date_filter="all", state_filter="active", limit=10)
        self.assertEqual([i["id"] for i in active], ["active"])

    def test_count_by_state(self) -> None:
        now = int(time.time())
        cache_db.upsert_conversation(self.path, self._sample_conv("a", now))
        cache_db.upsert_conversation(self.path, self._sample_conv("b", now))
        cache_db.upsert_evaluation(self.path, "a", "v1", {
            "total_score": 80, "max_score": 100, "grade": "Tốt",
            "blacklist_triggered": False, "findings": [], "blacklist_findings": [],
        })
        counts = cache_db.count_by_state(self.path)
        self.assertEqual(counts["all"], 2)
        self.assertEqual(counts["graded"], 1)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(counts["alert"], 0)

    def test_last_message_fields_persist(self) -> None:
        cache_db.upsert_conversation(
            self.path,
            self._sample_conv("c1", updated_at=int(time.time())),
            sync_source="light",
            last_message_at=1700000000,
            last_message_sender="customer",
        )
        row = cache_db.get_conversation_row(self.path, "c1")
        self.assertEqual(row["last_message_sender"], "customer")
        self.assertEqual(row["last_message_at"], 1700000000)
        self.assertEqual(row["sync_source"], "light")

    def test_sla_violation_lifecycle(self) -> None:
        cache_db.upsert_conversation(self.path, self._sample_conv("c1", int(time.time())))
        cache_db.record_sla_violation(self.path, "c1", customer_message_at=1700, detected_at=1750,
                                       business_minutes_at_detection=75)
        # Idempotent: re-record updates minutes, doesn't duplicate.
        cache_db.record_sla_violation(self.path, "c1", customer_message_at=1700, detected_at=1800,
                                       business_minutes_at_detection=120)
        active = cache_db.list_active_sla_violations(self.path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["conversation_id"], "c1")
        self.assertEqual(active[0]["business_minutes_at_detection"], 120)

        cache_db.resolve_sla_violation(self.path, "c1", customer_message_at=1700, resolved_at=2000)
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 0)


if __name__ == "__main__":
    unittest.main()
