import unittest

from app.grading import grade_score


class GradingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
