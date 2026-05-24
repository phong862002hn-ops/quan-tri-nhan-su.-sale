import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import business_hours
from app.business_hours import VN_TZ, business_minutes_between, is_in_business_hours


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=VN_TZ).timestamp())


class BusinessHoursTests(unittest.TestCase):
    def test_full_hour_inside_window(self) -> None:
        self.assertEqual(
            business_minutes_between(_ts(2026, 5, 21, 10), _ts(2026, 5, 21, 11)),
            60,
        )

    def test_partial_minutes_inside_window(self) -> None:
        self.assertEqual(
            business_minutes_between(_ts(2026, 5, 21, 10, 0), _ts(2026, 5, 21, 10, 30)),
            30,
        )

    def test_late_night_to_morning_crosses_closed_window(self) -> None:
        # 22:00 → 09:00 next day. Open hours 8-23. Expect 1h (22-23) + 1h (8-9) = 120.
        result = business_minutes_between(
            _ts(2026, 5, 21, 22, 0),
            _ts(2026, 5, 22, 9, 0),
        )
        self.assertEqual(result, 120)

    def test_entirely_outside_hours_returns_zero(self) -> None:
        # 23:30 → 01:00 next day, no overlap with 8-23.
        result = business_minutes_between(
            _ts(2026, 5, 21, 23, 30),
            _ts(2026, 5, 22, 1, 0),
        )
        self.assertEqual(result, 0)

    def test_start_before_open_clips_to_open(self) -> None:
        # 06:00 → 09:00. Only 08:00-09:00 counts.
        result = business_minutes_between(
            _ts(2026, 5, 21, 6, 0),
            _ts(2026, 5, 21, 9, 0),
        )
        self.assertEqual(result, 60)

    def test_end_after_close_clips_to_close(self) -> None:
        # 22:00 → 23:30 same day. 22-23 = 60.
        result = business_minutes_between(
            _ts(2026, 5, 21, 22, 0),
            _ts(2026, 5, 21, 23, 30),
        )
        self.assertEqual(result, 60)

    def test_zero_when_start_after_end(self) -> None:
        self.assertEqual(business_minutes_between(_ts(2026, 5, 21, 12), _ts(2026, 5, 21, 10)), 0)

    def test_zero_when_equal(self) -> None:
        self.assertEqual(business_minutes_between(_ts(2026, 5, 21, 12), _ts(2026, 5, 21, 12)), 0)

    def test_multi_day_span(self) -> None:
        # Mon 10:00 → Wed 10:00. Each full business day = 15h = 900 min.
        # Day1: 10:00 → 23:00 = 13h = 780 min
        # Day2: 08:00 → 23:00 = 15h = 900 min
        # Day3: 08:00 → 10:00 = 2h = 120 min
        # Total = 1800 min
        result = business_minutes_between(
            _ts(2026, 5, 18, 10, 0),
            _ts(2026, 5, 20, 10, 0),
        )
        self.assertEqual(result, 1800)

    def test_is_in_business_hours(self) -> None:
        self.assertTrue(is_in_business_hours(_ts(2026, 5, 21, 12, 0)))
        self.assertFalse(is_in_business_hours(_ts(2026, 5, 21, 7, 59)))
        self.assertFalse(is_in_business_hours(_ts(2026, 5, 21, 23, 30)))
        self.assertTrue(is_in_business_hours(_ts(2026, 5, 21, 22, 59)))

    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"QA_BUSINESS_HOUR_START": "9", "QA_BUSINESS_HOUR_END": "18"}):
            self.assertEqual(business_hours.get_business_hours(), (9, 18))
            # 10:00 - 11:00 still inside the narrower window
            self.assertEqual(
                business_minutes_between(_ts(2026, 5, 21, 10), _ts(2026, 5, 21, 11)),
                60,
            )
            # 17:00 - 19:00: only 17-18 counts.
            self.assertEqual(
                business_minutes_between(_ts(2026, 5, 21, 17), _ts(2026, 5, 21, 19)),
                60,
            )

    def test_invalid_env_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"QA_BUSINESS_HOUR_START": "23", "QA_BUSINESS_HOUR_END": "8"}):
            self.assertEqual(business_hours.get_business_hours(), (8, 23))


if __name__ == "__main__":
    unittest.main()
