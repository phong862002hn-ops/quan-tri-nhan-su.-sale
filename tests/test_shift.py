import unittest
from datetime import datetime

from app.shift import (
    VN_TZ,
    SHIFT_DEFINITIONS,
    get_conversation_last_message_unix,
    get_conversation_shift,
    get_shift_by_id,
    get_shift_date_range,
    get_shift_for_timestamp,
)


def _unix(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=VN_TZ).timestamp())


class ShiftTimestampTests(unittest.TestCase):
    def test_morning(self) -> None:
        shift = get_shift_for_timestamp(_unix(2026, 5, 22, 9, 30))
        self.assertEqual(shift["id"], "morning")

    def test_afternoon_boundary(self) -> None:
        # 13:00 is the start of afternoon (>=, exclusive end of morning).
        shift = get_shift_for_timestamp(_unix(2026, 5, 22, 13, 0))
        self.assertEqual(shift["id"], "afternoon")

    def test_evening(self) -> None:
        shift = get_shift_for_timestamp(_unix(2026, 5, 22, 20, 0))
        self.assertEqual(shift["id"], "evening")

    def test_after_close(self) -> None:
        # 23:00 sharp is outside the evening window (exclusive end).
        self.assertIsNone(get_shift_for_timestamp(_unix(2026, 5, 22, 23, 0)))
        self.assertIsNone(get_shift_for_timestamp(_unix(2026, 5, 22, 23, 59)))

    def test_overnight(self) -> None:
        self.assertIsNone(get_shift_for_timestamp(_unix(2026, 5, 22, 2, 0)))

    def test_just_before_open(self) -> None:
        self.assertIsNone(get_shift_for_timestamp(_unix(2026, 5, 22, 7, 59)))

    def test_none_input(self) -> None:
        self.assertIsNone(get_shift_for_timestamp(None))
        self.assertIsNone(get_shift_for_timestamp(0))


class ConversationShiftTests(unittest.TestCase):
    def _conv(self, sent_at_iso_list):
        msgs = [{"id": f"m{i}", "sender_type": "customer", "text": "",
                 "attachments": [], "sent_at": ts} for i, ts in enumerate(sent_at_iso_list)]
        return {"messages": msgs}

    def test_picks_latest_message(self) -> None:
        conv = self._conv([
            "2026-05-22T09:00:00+07:00",
            "2026-05-22T19:30:00+07:00",  # latest → evening
            "2026-05-22T10:00:00+07:00",
        ])
        self.assertEqual(get_conversation_shift(conv)["id"], "evening")

    def test_empty_conversation_returns_none(self) -> None:
        self.assertIsNone(get_conversation_shift({"messages": []}))
        self.assertIsNone(get_conversation_shift(None))

    def test_last_message_overnight_returns_none(self) -> None:
        conv = self._conv(["2026-05-22T02:00:00+07:00"])
        self.assertIsNone(get_conversation_shift(conv))

    def test_last_message_unix_extraction(self) -> None:
        conv = self._conv(["2026-05-22T10:00:00+07:00", "2026-05-22T12:30:00+07:00"])
        self.assertEqual(
            get_conversation_last_message_unix(conv),
            _unix(2026, 5, 22, 12, 30),
        )


class ShiftDateRangeTests(unittest.TestCase):
    def test_morning_range(self) -> None:
        start, end = get_shift_date_range("2026-05-22", "morning")
        self.assertEqual(start, _unix(2026, 5, 22, 8, 0))
        self.assertEqual(end, _unix(2026, 5, 22, 13, 0))

    def test_evening_range(self) -> None:
        start, end = get_shift_date_range("2026-05-22", "evening")
        self.assertEqual(start, _unix(2026, 5, 22, 18, 0))
        self.assertEqual(end, _unix(2026, 5, 22, 23, 0))

    def test_unknown_shift_raises(self) -> None:
        with self.assertRaises(ValueError):
            get_shift_date_range("2026-05-22", "midnight")


class ShiftRegistryTests(unittest.TestCase):
    def test_get_by_id(self) -> None:
        self.assertEqual(get_shift_by_id("morning")["start_hour"], 8)
        self.assertIsNone(get_shift_by_id("nope"))

    def test_three_shifts_defined(self) -> None:
        self.assertEqual(len(SHIFT_DEFINITIONS), 3)


if __name__ == "__main__":
    unittest.main()
