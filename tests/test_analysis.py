"""Tests cho weekly_analysis, alert_engine, department_overview."""
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from app import alert_engine, department_overview, history, weekly_analysis


def _entry(date_str, emp_id, emp_name, score, blacklist=False, passed=None):
    if passed is None:
        passed = score >= 75 and not blacklist
    return {
        "saved_at": date_str + "T00:00:00+00:00",
        "date": date_str,
        "employee": {"id": emp_id, "name": emp_name},
        "evaluation": {
            "conversation_id": f"{emp_id}-{date_str}",
            "total_score": score,
            "max_score": 100,
            "grade": "Tốt" if score >= 75 else ("Trung bình" if score >= 50 else "Không đạt"),
            "passed": passed,
            "blacklist_triggered": blacklist,
            "category_scores": [], "findings": [], "blacklist_findings": [],
            "response_time": {"avg_response_seconds": 100.0},
        },
    }


class WeeklyAnalysisTests(unittest.TestCase):
    def test_resolve_preset_ranges_this_vs_last_week(self):
        ref = date(2026, 5, 7)  # Thursday
        a, b = weekly_analysis.resolve_preset_ranges("this_vs_last_week", ref)
        self.assertEqual(a, (date(2026, 5, 4), date(2026, 5, 10)))
        self.assertEqual(b, (date(2026, 4, 27), date(2026, 5, 3)))

    def test_resolve_preset_ranges_last_7_days(self):
        ref = date(2026, 5, 10)
        a, b = weekly_analysis.resolve_preset_ranges("last_7_days", ref)
        self.assertEqual(a, (date(2026, 5, 4), date(2026, 5, 10)))
        self.assertEqual(b, (date(2026, 4, 27), date(2026, 5, 3)))

    def test_resolve_preset_invalid_raises(self):
        with self.assertRaises(ValueError):
            weekly_analysis.resolve_preset_ranges("not_a_preset")

    def test_period_labels_dict_complete(self):
        for preset in weekly_analysis.PRESETS:
            self.assertIn(preset, weekly_analysis.PRESET_PERIOD_LABELS)

    def test_analyze_period_comparison_verdicts(self):
        history_data = [
            _entry("2026-05-05", "e1", "A", 90),
            _entry("2026-04-28", "e1", "A", 70),
            _entry("2026-05-05", "e2", "B", 50, blacklist=True),
            _entry("2026-04-28", "e2", "B", 75),
        ]
        with mock.patch.object(weekly_analysis, "load_all_history", return_value=history_data):
            result = weekly_analysis.analyze_period_comparison(
                period_a=(date(2026, 5, 4), date(2026, 5, 10)),
                period_b=(date(2026, 4, 27), date(2026, 5, 3)),
                period_a_label="Tuần này",
                period_b_label="Tuần trước",
            )
        emp_by_id = {e["employee_id"]: e for e in result["employees"]}
        self.assertEqual(emp_by_id["e1"]["verdict"], "improved")
        self.assertEqual(emp_by_id["e2"]["verdict"], "declined")
        self.assertEqual(result["period_a_label"], "Tuần này")
        self.assertEqual(result["period_b_label"], "Tuần trước")

    def test_employee_appears_only_in_a_is_new(self):
        history_data = [_entry("2026-05-05", "e_new", "New", 80)]
        with mock.patch.object(weekly_analysis, "load_all_history", return_value=history_data):
            result = weekly_analysis.analyze_period_comparison(
                (date(2026, 5, 4), date(2026, 5, 10)),
                (date(2026, 4, 27), date(2026, 5, 3)),
            )
        verdicts = {e["employee_id"]: e["verdict"] for e in result["employees"]}
        self.assertEqual(verdicts["e_new"], "new")


class AlertEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_path = self.tmp / "alert_config.json"
        mock.patch.object(alert_engine, "ALERT_CONFIG_PATH", self.cfg_path).start()

    def tearDown(self):
        shutil.rmtree(self.tmp)
        mock.patch.stopall()

    def test_creates_default_config_if_missing(self):
        cfg = alert_engine.load_alert_config()
        self.assertIn("blacklist_per_week", cfg)
        self.assertTrue(self.cfg_path.exists())

    def test_no_alerts_when_history_empty(self):
        with mock.patch.object(department_overview, "load_all_history", return_value=[]):
            self.assertEqual(alert_engine.run_alerts([]), [])

    def test_low_avg_score_alert_fires(self):
        today_str = date.today().isoformat()
        history_data = [
            _entry(today_str, "e_low", "Low", 30, blacklist=False),
            _entry(today_str, "e_low", "Low", 40, blacklist=False),
        ]
        alerts = alert_engine.run_alerts(history_data)
        types = [a["type"] for a in alerts if a["employee_id"] == "e_low"]
        self.assertIn("low_avg_score", types)

    def test_blacklist_per_week_alert_fires(self):
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        history_data = [
            _entry(monday.isoformat(), "e_bl", "BL", 40, blacklist=True),
            _entry((monday + timedelta(days=1)).isoformat(), "e_bl", "BL", 40, blacklist=True),
        ]
        alerts = alert_engine.run_alerts(history_data)
        types = [a["type"] for a in alerts if a["employee_id"] == "e_bl"]
        self.assertIn("blacklist_week", types)

    def test_dedupes_same_employee_same_type(self):
        today_str = date.today().isoformat()
        history_data = [
            _entry(today_str, "e1", "A", 30),
            _entry(today_str, "e1", "A", 30),
            _entry(today_str, "e1", "A", 30),
        ]
        alerts = alert_engine.run_alerts(history_data)
        same = [a for a in alerts if a["employee_id"] == "e1" and a["type"] == "low_avg_score"]
        self.assertEqual(len(same), 1)


class DepartmentOverviewTests(unittest.TestCase):
    def test_empty_history_returns_zeros(self):
        with mock.patch.object(department_overview, "load_all_history", return_value=[]):
            o = department_overview.build_department_overview()
        self.assertEqual(o["kpi"]["total_conversations"], 0)
        self.assertEqual(o["top_performers"], [])

    def test_kpi_calculations(self):
        history_data = [
            _entry("2026-05-04", "e1", "A", 100),
            _entry("2026-05-04", "e2", "B", 50, blacklist=True),
            _entry("2026-05-04", "e3", "C", 80),
        ]
        with mock.patch.object(department_overview, "load_all_history", return_value=history_data):
            o = department_overview.build_department_overview()
        kpi = o["kpi"]
        self.assertEqual(kpi["total_conversations"], 3)
        self.assertEqual(kpi["total_employees"], 3)
        self.assertAlmostEqual(kpi["team_avg_score"], 76.67, places=1)
        self.assertEqual(kpi["blacklist_count"], 1)

    def test_critical_employees_flagged(self):
        history_data = [
            _entry("2026-05-04", "e_low", "Low", 30),
            _entry("2026-05-04", "e_bl", "BL", 80, blacklist=True),
            _entry("2026-05-04", "e_ok", "OK", 95),
        ]
        with mock.patch.object(department_overview, "load_all_history", return_value=history_data):
            o = department_overview.build_department_overview()
        critical_ids = {c["employee_id"] for c in o["critical_employees"]}
        self.assertIn("e_low", critical_ids)
        self.assertIn("e_bl", critical_ids)
        self.assertNotIn("e_ok", critical_ids)

    def test_period_filter_excludes_outside_range(self):
        history_data = [
            _entry("2026-05-01", "e1", "A", 80),
            _entry("2026-05-15", "e1", "A", 90),
        ]
        with mock.patch.object(department_overview, "load_all_history", return_value=history_data):
            o = department_overview.build_department_overview(
                period_start=date(2026, 5, 10),
                period_end=date(2026, 5, 20),
            )
        self.assertEqual(o["kpi"]["total_conversations"], 1)
        self.assertEqual(o["top_performers"][0]["avg_score"], 90.0)


if __name__ == "__main__":
    unittest.main()
