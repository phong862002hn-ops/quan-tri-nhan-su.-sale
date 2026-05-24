import unittest

from app.grading import grade_score


class GradingTests(unittest.TestCase):
    """Legacy behaviour (no max_score given): treat total_score as % directly."""

    def test_grade_excellent(self) -> None:
        self.assertEqual(grade_score(95, False), "Xuất sắc")

    def test_grade_good(self) -> None:
        self.assertEqual(grade_score(80, False), "Tốt")

    def test_grade_average(self) -> None:
        self.assertEqual(grade_score(60, False), "Trung bình")

    def test_grade_fail(self) -> None:
        self.assertEqual(grade_score(40, False), "Không đạt")

    def test_blacklist_forces_fail(self) -> None:
        self.assertEqual(grade_score(90, True), "Không đạt")


class GradingWithMaxScoreTests(unittest.TestCase):
    """When max_score is provided, grades are anchored to percentage."""

    def test_fb_excellent_full_ruleset(self) -> None:
        # 95/105 = 90.5% → Xuất sắc
        self.assertEqual(grade_score(95, False, max_score=105), "Xuất sắc")

    def test_shopee_excellent_when_close_order_skipped(self) -> None:
        # Shopee skips sop_close_order (7đ) → effective_max=98
        # 88.2/98 = 90.0% → Xuất sắc
        self.assertEqual(grade_score(88.2, False, max_score=98), "Xuất sắc")

    def test_fb_good_under_90_percent(self) -> None:
        # 85/105 = 80.95% → Tốt
        self.assertEqual(grade_score(85, False, max_score=105), "Tốt")

    def test_zero_max_score_returns_unevaluable(self) -> None:
        self.assertEqual(grade_score(0, False, max_score=0), "Không đánh giá được")

    def test_blacklist_overrides_percentage(self) -> None:
        self.assertEqual(grade_score(100, True, max_score=105), "Không đạt")


if __name__ == "__main__":
    unittest.main()
