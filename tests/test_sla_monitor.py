import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import cache_db, sla_monitor
from app.business_hours import VN_TZ


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=VN_TZ).timestamp())


def _msg(sender: str, created_at: int) -> dict:
    return {"sender_type": sender, "created_at": created_at}


class SlaMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.db"
        cache_db.init_db(self.path)
        cache_db.upsert_conversation(self.path, {
            "id": "c1", "pageId": "p", "channel": "1",
            "pageUserName": "Khach", "updatedAt": _ts(2026, 5, 21, 10),
        })

    def tearDown(self) -> None:
        for key, conn in list(cache_db._connections.items()):
            conn.close()
            cache_db._connections.pop(key, None)
        self.tmp.cleanup()

    def test_customer_msg_then_sale_reply_no_violation(self) -> None:
        messages = [
            _msg("customer", _ts(2026, 5, 21, 9, 0)),
            _msg("employee", _ts(2026, 5, 21, 9, 30)),
        ]
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1", messages, now=_ts(2026, 5, 21, 11),
        )
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 0)

    def test_customer_msg_unanswered_over_threshold_flags_violation(self) -> None:
        # Khách nhắn 9:00, không reply, check lúc 10:30. Business minutes = 90 > 60.
        messages = [_msg("customer", _ts(2026, 5, 21, 9, 0))]
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1", messages, now=_ts(2026, 5, 21, 10, 30),
        )
        active = cache_db.list_active_sla_violations(self.path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["business_minutes_at_detection"], 90)

    def test_violation_is_resolved_when_sale_replies_later(self) -> None:
        # Step 1: detect violation at 10:30.
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1",
            [_msg("customer", _ts(2026, 5, 21, 9, 0))],
            now=_ts(2026, 5, 21, 10, 30),
        )
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 1)
        # Step 2: sale replies. Pass full message set including the reply.
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1",
            [
                _msg("customer", _ts(2026, 5, 21, 9, 0)),
                _msg("employee", _ts(2026, 5, 21, 11, 0)),
            ],
            now=_ts(2026, 5, 21, 12, 0),
        )
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 0)

    def test_overnight_business_hours_respected(self) -> None:
        # Khách nhắn 22:50, không reply qua đêm. Check 9:30 hôm sau.
        # Business minutes: 22:50-23:00 = 10 + 8:00-9:30 next day = 90 → total 100.
        # Threshold 60 → violation.
        messages = [_msg("customer", _ts(2026, 5, 21, 22, 50))]
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1", messages, now=_ts(2026, 5, 22, 9, 30),
        )
        active = cache_db.list_active_sla_violations(self.path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["business_minutes_at_detection"], 100)

    def test_overnight_below_threshold_no_violation(self) -> None:
        # Khách nhắn 22:50, check 8:30 hôm sau. Business = 10 + 30 = 40 < 60.
        messages = [_msg("customer", _ts(2026, 5, 21, 22, 50))]
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1", messages, now=_ts(2026, 5, 22, 8, 30),
        )
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 0)

    def test_multiple_customer_messages_count_from_first(self) -> None:
        # 9:00, 9:05, 9:10 — chain starts at 9:00.
        messages = [
            _msg("customer", _ts(2026, 5, 21, 9, 0)),
            _msg("customer", _ts(2026, 5, 21, 9, 5)),
            _msg("customer", _ts(2026, 5, 21, 9, 10)),
        ]
        sla_monitor.detect_violations_for_conversation(
            self.path, "c1", messages, now=_ts(2026, 5, 21, 10, 30),
        )
        active = cache_db.list_active_sla_violations(self.path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["customer_message_at"], _ts(2026, 5, 21, 9, 0))
        # 9:00 → 10:30 = 90 min.
        self.assertEqual(active[0]["business_minutes_at_detection"], 90)

    def test_threshold_override_via_env(self) -> None:
        messages = [_msg("customer", _ts(2026, 5, 21, 10, 0))]
        with mock.patch.dict(os.environ, {"QA_SLA_THRESHOLD_MINUTES": "180"}):
            sla_monitor.detect_violations_for_conversation(
                self.path, "c1", messages, now=_ts(2026, 5, 21, 12, 0),
            )
        # 120 min < 180 threshold → no violation.
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 0)

    def test_empty_messages_does_nothing(self) -> None:
        sla_monitor.detect_violations_for_conversation(self.path, "c1", [], now=_ts(2026, 5, 21, 10))
        self.assertEqual(len(cache_db.list_active_sla_violations(self.path)), 0)


if __name__ == "__main__":
    unittest.main()
