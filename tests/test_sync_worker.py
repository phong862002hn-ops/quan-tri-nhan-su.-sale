import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import cache_db
from app.business_hours import VN_TZ
from app.nhanh_client import NhanhConfig
from app.schemas import load_ruleset
from app.sync_worker import LightSyncWorker, HeavySyncWorker


REPO_ROOT = Path(__file__).resolve().parent.parent
RULESET_PATH = REPO_ROOT / "data" / "rules.json"


def _config() -> NhanhConfig:
    return NhanhConfig(
        app_id=1, business_id=1, access_token="t", secret_key="s",
        service="vpage", verify_ssl=False,
    )


def _conv_summary(conv_id: str, page_id: str, updated_at: int, customer_id: str = "cust1") -> dict:
    return {
        "id": conv_id,
        "pageId": page_id,
        "channel": "1",
        "pageUserId": customer_id,
        "pageUserName": "Customer " + conv_id,
        "updatedAt": updated_at,
        "type": 2,
    }


def _msg(msg_id: str, sender_id: str, created_at: int, text: str = "x", page_id: str = "p1") -> dict:
    return {
        "id": msg_id, "messageId": msg_id, "senderId": sender_id,
        "senderName": "S", "message": text, "createdAt": created_at,
        "pageId": page_id, "attachments": [],
    }


class _BaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "cache.db"
        cache_db.init_db(self.db_path)
        self.ruleset = load_ruleset(RULESET_PATH)
        self.config = _config()

    def tearDown(self) -> None:
        for key, conn in list(cache_db._connections.items()):
            conn.close()
            cache_db._connections.pop(key, None)
        self.tmp.cleanup()


class HeavyWorkerTests(_BaseTest):
    def _build(self, **kwargs) -> HeavySyncWorker:
        return HeavySyncWorker(
            config=self.config,
            ruleset=self.ruleset,
            db_path=self.db_path,
            ruleset_version="v_test",
            interval_s=kwargs.pop("interval_s", 3600),
            inter_call_delay_s=kwargs.pop("inter_call_delay_s", 0),
            **kwargs,
        )

    def test_is_first_run_true_on_empty_db(self) -> None:
        self.assertTrue(self._build().is_first_run())

    def test_backfill_old_idle_conv_with_customer_last_message_is_graded(self) -> None:
        # 25h-old conversation, last message = customer → should be graded.
        now_ts = int(time.time())
        page_id, customer_id = "p1", "cust1"
        last_msg_ts = now_ts - 25 * 3600

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            f = payload.get("filters") or {}
            if "conversationId" in f:
                # Single customer message > 24h ago
                return {"code": 1, "data": [_msg("m1", customer_id, last_msg_ts, page_id=page_id)]}
            if "pageIds" in f:
                return {"data": [_conv_summary("c1", page_id, last_msg_ts, customer_id=customer_id)]}
            return {"data": [_conv_summary("c1", page_id, last_msg_ts, customer_id=customer_id)]}

        worker = self._build()
        with mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            worker._tick()

        evaluation = cache_db.get_evaluation(self.db_path, "c1", "v_test")
        self.assertIsNotNone(evaluation, "conv idle 25h + last=customer must be graded")

    def test_recent_conv_not_graded_by_guard(self) -> None:
        # Conversation with messages 1 hour ago — guard rejects.
        now_ts = int(time.time())
        recent_ts = now_ts - 3600

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            f = payload.get("filters") or {}
            if "conversationId" in f:
                return {"code": 1, "data": [_msg("m1", "cust1", recent_ts)]}
            if "pageIds" in f:
                return {"data": [_conv_summary("c2", "p1", recent_ts)]}
            return {"data": [_conv_summary("c2", "p1", recent_ts)]}

        worker = self._build()
        with mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            worker._tick()

        self.assertIsNone(cache_db.get_evaluation(self.db_path, "c2", "v_test"))
        # Conv row still exists (just not graded).
        row = cache_db.get_conversation_row(self.db_path, "c2")
        self.assertIsNotNone(row)

    def test_old_conv_last_message_from_employee_not_graded(self) -> None:
        # Customer at -30h, employee replies at -25h. Last msg = employee → guard rejects.
        now_ts = int(time.time())

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            f = payload.get("filters") or {}
            if "conversationId" in f:
                return {
                    "code": 1,
                    "data": [
                        _msg("m1", "cust1", now_ts - 30 * 3600),
                        _msg("m2", "p1", now_ts - 25 * 3600),  # sender_id == pageId → employee
                    ],
                }
            if "pageIds" in f:
                return {"data": [_conv_summary("c3", "p1", now_ts - 25 * 3600)]}
            return {"data": [_conv_summary("c3", "p1", now_ts - 25 * 3600)]}

        worker = self._build()
        with mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            worker._tick()
        self.assertIsNone(cache_db.get_evaluation(self.db_path, "c3", "v_test"))

    def test_nhanh_error_does_not_kill_worker(self) -> None:
        from app.nhanh_client import NhanhClientError

        def boom(*a, **kw):
            raise NhanhClientError("500")

        worker = self._build()
        with mock.patch("app.sync_worker.post_json", side_effect=boom), \
             mock.patch("app.nhanh_client.post_json", side_effect=boom):
            worker._tick()
        status = worker.get_status()
        self.assertIn("500", status["last_error"] or "")
        self.assertFalse(status["is_syncing"])


class LightWorkerTests(_BaseTest):
    def _build(self, **kwargs) -> LightSyncWorker:
        return LightSyncWorker(
            config=self.config,
            ruleset=self.ruleset,
            db_path=self.db_path,
            ruleset_version="v_test",
            interval_s=kwargs.pop("interval_s", 600),
            inter_call_delay_s=kwargs.pop("inter_call_delay_s", 0),
            **kwargs,
        )

    def test_skips_conv_with_no_change(self) -> None:
        # Pre-populate DB with conv at updated_at=1000.
        cache_db.upsert_conversation(self.db_path, _conv_summary("c1", "p1", 1000))

        fetched_messages = False

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            nonlocal fetched_messages
            f = payload.get("filters") or {}
            if "conversationId" in f:
                fetched_messages = True
                return {"code": 1, "data": []}
            # list_conversations: same updatedAt as cache → should NOT fetch messages
            return {"data": [_conv_summary("c1", "p1", 1000)]}

        worker = self._build()
        with mock.patch("app.sync_worker.nhanh_list_conversations") as lc, \
             mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            lc.return_value = {"data": [_conv_summary("c1", "p1", 1000)]}
            worker._tick()
        self.assertFalse(fetched_messages, "Light should skip messages for unchanged convs")

    def test_fetches_when_updated_at_advances(self) -> None:
        cache_db.upsert_conversation(self.db_path, _conv_summary("c1", "p1", 1000))
        now_ts = int(time.time())

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            f = payload.get("filters") or {}
            if "conversationId" in f:
                return {"code": 1, "data": [_msg("m1", "cust1", now_ts - 25 * 3600)]}
            return {"data": [_conv_summary("c1", "p1", now_ts)]}

        worker = self._build()
        with mock.patch("app.sync_worker.nhanh_list_conversations") as lc, \
             mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            lc.return_value = {"data": [_conv_summary("c1", "p1", now_ts)]}
            worker._tick()
        row = cache_db.get_conversation_row(self.db_path, "c1")
        self.assertEqual(row["updated_at"], now_ts)
        self.assertEqual(row["last_message_sender"], "customer")
        # Idle 25h + last=customer → graded.
        self.assertIsNotNone(cache_db.get_evaluation(self.db_path, "c1", "v_test"))

    def test_system_notifications_are_filtered_from_last_sender(self) -> None:
        """Regression: TikTok system notifications (empty text + attachment
        type='notification') must NOT be treated as the last message when
        computing last_message_sender / last_message_at."""
        now_ts = int(time.time())
        page_id, customer_id = "p1", "cust1"
        real_reply_ts = now_ts - 25 * 3600
        notif_ts = real_reply_ts + 60

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            f = payload.get("filters") or {}
            if "conversationId" in f:
                return {"code": 1, "data": [
                    _msg("m1", customer_id, real_reply_ts - 120, text="hỏi gì đó"),
                    _msg("m2", page_id, real_reply_ts, text="Dạ vài hôm sẽ hết nha ạ"),
                    {"id": "m3", "messageId": "m3", "senderId": page_id, "senderName": "S",
                     "message": "", "createdAt": notif_ts, "pageId": page_id,
                     "attachments": [{"type": "notification", "payload": {}}]},
                ]}
            return {"data": [_conv_summary("conv-n", page_id, real_reply_ts, customer_id=customer_id)]}

        worker = HeavySyncWorker(
            config=self.config, ruleset=self.ruleset, db_path=self.db_path,
            ruleset_version="v_test", interval_s=3600, inter_call_delay_s=0,
        )
        with mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            worker._tick()

        row = cache_db.get_conversation_row(self.db_path, "conv-n")
        self.assertEqual(row["last_message_sender"], "employee")
        # The notification (later by 60s) is filtered out → last_message_at
        # must reflect the real reply, not the notification.
        self.assertEqual(row["last_message_at"], real_reply_ts)

    def test_sop_closing_passes_when_only_notification_trails(self) -> None:
        """End-to-end: a conv ending with a real NV reply that contains a
        closing keyword, followed only by a system notification, must pass
        sop_closing once notifications are stripped."""
        from app.evaluator import evaluate_conversation
        from app.nhanh_adapter import build_conversation
        from app.sync_worker import _is_system_notification

        page_id, customer_id = "p1", "cust1"
        payload = {"data": [
            _msg("m1", customer_id, 1000, text="alo"),
            _msg("m2", page_id, 1100, text="Dạ vài hôm sẽ hết nha ạ"),
            {"id": "m3", "messageId": "m3", "senderId": page_id, "senderName": "S",
             "message": "", "createdAt": 1200, "pageId": page_id,
             "attachments": [{"type": "notification", "payload": {}}]},
        ]}
        summary = _conv_summary("c-clo", page_id, 1100, customer_id=customer_id)

        # Without filter: sop_closing fails (last text is empty notification).
        conv_with_notif = build_conversation("c-clo", payload, summary_item=summary)
        result_with = evaluate_conversation(conv_with_notif, self.ruleset).to_dict()
        closing_with = next(f for f in result_with["findings"] if f["rule_id"] == "sop_closing")
        self.assertFalse(closing_with["passed"])

        # With filter: sop_closing passes.
        clean = dict(payload)
        clean["data"] = [it for it in payload["data"] if not _is_system_notification(it)]
        conv_clean = build_conversation("c-clo", clean, summary_item=summary)
        result_clean = evaluate_conversation(conv_clean, self.ruleset).to_dict()
        closing_clean = next(f for f in result_clean["findings"] if f["rule_id"] == "sop_closing")
        self.assertTrue(closing_clean["passed"],
                        f"sop_closing should pass — got: {closing_clean['explanation']}")

    def test_sla_violation_recorded_on_unanswered_customer_msg(self) -> None:
        # Customer message 2 hours ago in business hours, sale hasn't replied.
        # Force a known timestamp during business hours to dodge real-clock flakes.
        msg_ts = int(datetime(2026, 5, 21, 10, 0, tzinfo=VN_TZ).timestamp())
        now_ts = int(datetime(2026, 5, 21, 13, 0, tzinfo=VN_TZ).timestamp())

        def fake_post_json(url, payload, access_token, verify_ssl=True):
            f = payload.get("filters") or {}
            if "conversationId" in f:
                return {"code": 1, "data": [_msg("m1", "cust1", msg_ts)]}
            return {"data": [_conv_summary("c1", "p1", now_ts)]}

        worker = LightSyncWorker(
            config=self.config, ruleset=self.ruleset, db_path=self.db_path,
            ruleset_version="v", interval_s=600, inter_call_delay_s=0,
            clock=lambda: now_ts,
        )
        with mock.patch("app.sync_worker.nhanh_list_conversations") as lc, \
             mock.patch("app.sync_worker.post_json", side_effect=fake_post_json), \
             mock.patch("app.nhanh_client.post_json", side_effect=fake_post_json):
            lc.return_value = {"data": [_conv_summary("c1", "p1", now_ts)]}
            worker._tick()

        active = cache_db.list_active_sla_violations(self.db_path)
        self.assertEqual(len(active), 1)
        # 10:00 → 13:00 entirely in business hours = 180 min > 60 → flagged.
        self.assertEqual(active[0]["business_minutes_at_detection"], 180)


if __name__ == "__main__":
    unittest.main()
