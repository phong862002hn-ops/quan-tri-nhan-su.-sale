from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest import mock

from app.shift import VN_TZ
from app.shift_selector import (
    get_quota_config,
    select_conversations_for_shift,
)


def _msg(sent_at_iso: str) -> dict:
    return {"id": "m", "sender_type": "customer", "text": "", "attachments": [], "sent_at": sent_at_iso}


def _conv(external_id: str, last_at_iso: str) -> dict:
    return {"external_id": external_id, "messages": [_msg(last_at_iso)]}


def _eval(
    total: float,
    max_score: float = 100.0,
    blacklist: bool = False,
    failed_rules: list[str] | None = None,
) -> dict:
    findings = []
    for rid in failed_rules or []:
        findings.append({"rule_id": rid, "passed": False})
    return {
        "total_score": total,
        "max_score": max_score,
        "blacklist_triggered": blacklist,
        "findings": findings,
        "blacklist_findings": [],
    }


def _entry(external_id: str, hour: int, total: float, **eval_kwargs) -> dict:
    iso = datetime(2026, 5, 22, hour, 0, tzinfo=VN_TZ).isoformat()
    return {"conversation": _conv(external_id, iso), "evaluation": _eval(total, **eval_kwargs)}


class QuotaConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for var in [
                "SHIFT_QUOTA_PRIORITY_1", "SHIFT_QUOTA_PRIORITY_2",
                "SHIFT_QUOTA_PRIORITY_3", "SHIFT_QUOTA_PRIORITY_4", "SHIFT_QUOTA_TOTAL",
            ]:
                os.environ.pop(var, None)
            cfg = get_quota_config()
            self.assertEqual(cfg, {"priority_1": 10, "priority_2": 15,
                                    "priority_3": 15, "priority_4": 10, "total": 50})

    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {
            "SHIFT_QUOTA_PRIORITY_1": "5",
            "SHIFT_QUOTA_TOTAL": "30",
        }):
            cfg = get_quota_config()
        self.assertEqual(cfg["priority_1"], 5)
        self.assertEqual(cfg["total"], 30)


class SelectionTests(unittest.TestCase):
    def test_only_keeps_target_shift(self) -> None:
        items = [
            _entry("morning-1", 9, 80),
            _entry("evening-1", 20, 80),
        ]
        result = select_conversations_for_shift(items, "morning", "2026-05-22")
        self.assertEqual(result["total_in_shift"], 1)

    def test_blacklist_lands_in_priority_1(self) -> None:
        items = [
            _entry("bl-1", 9, 30, blacklist=True),
            _entry("normal-1", 9, 80),
        ]
        result = select_conversations_for_shift(items, "morning", "2026-05-22")
        ids = {x["conversation"]["external_id"] for x in result["priority_1"]}
        self.assertEqual(ids, {"bl-1"})

    def test_complaint_lands_in_priority_1(self) -> None:
        items = [
            _entry("comp-1", 9, 60, failed_rules=["complaint_flow"]),
            _entry("normal", 9, 80),
        ]
        result = select_conversations_for_shift(items, "morning", "2026-05-22")
        ids = {x["conversation"]["external_id"] for x in result["priority_1"]}
        self.assertEqual(ids, {"comp-1"})

    def test_priority_2_picks_lowest_scoring_non_p1(self) -> None:
        items = [
            _entry(f"c{i}", 9, total) for i, total in enumerate([10, 20, 30, 90, 95])
        ]
        result = select_conversations_for_shift(
            items, "morning", "2026-05-22",
            quota={"priority_1": 10, "priority_2": 2, "priority_3": 15, "priority_4": 10, "total": 50},
        )
        ids = [x["conversation"]["external_id"] for x in result["priority_2"]]
        self.assertEqual(ids, ["c0", "c1"])  # lowest 2

    def test_priority_3_pulls_only_from_medium_band(self) -> None:
        items = [
            _entry("low", 9, 10),                # below 50% — should go to P2
            _entry("med1", 9, 60),               # in 50-75 → P3
            _entry("med2", 9, 65),
            _entry("high", 9, 95),               # >= 85 → P4
        ]
        result = select_conversations_for_shift(
            items, "morning", "2026-05-22",
            quota={"priority_1": 10, "priority_2": 1, "priority_3": 15, "priority_4": 10, "total": 50},
        )
        ids = {x["conversation"]["external_id"] for x in result["priority_3"]}
        self.assertEqual(ids, {"med1", "med2"})

    def test_priority_4_pulls_from_high_band(self) -> None:
        items = [
            _entry("low", 9, 10),
            _entry("h1", 9, 90),
            _entry("h2", 9, 95),
        ]
        result = select_conversations_for_shift(
            items, "morning", "2026-05-22",
            quota={"priority_1": 10, "priority_2": 1, "priority_3": 15, "priority_4": 5, "total": 50},
        )
        ids = {x["conversation"]["external_id"] for x in result["priority_4"]}
        self.assertEqual(ids, {"h1", "h2"})

    def test_short_shift_marked_incomplete(self) -> None:
        items = [_entry(f"c{i}", 9, 50) for i in range(3)]
        result = select_conversations_for_shift(items, "morning", "2026-05-22")
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["total_selected"], 3)

    def test_empty_input(self) -> None:
        result = select_conversations_for_shift([], "morning", "2026-05-22")
        self.assertEqual(result["total_in_shift"], 0)
        for key in ("priority_1", "priority_2", "priority_3", "priority_4"):
            self.assertEqual(result[key], [])

    def test_deterministic_random_per_shift_date(self) -> None:
        # 30 medium-scoring conv, quota P3=5 — picks must be stable across calls
        items = [_entry(f"m{i}", 9, 60 + i * 0.5) for i in range(30)]
        a = select_conversations_for_shift(items, "morning", "2026-05-22")
        b = select_conversations_for_shift(items, "morning", "2026-05-22")
        self.assertEqual(
            [x["conversation"]["external_id"] for x in a["priority_3"]],
            [x["conversation"]["external_id"] for x in b["priority_3"]],
        )

    def test_no_overlap_between_priorities(self) -> None:
        items = (
            [_entry(f"bl{i}", 9, 5, blacklist=True) for i in range(2)]
            + [_entry(f"low{i}", 9, 20) for i in range(5)]
            + [_entry(f"med{i}", 9, 65) for i in range(20)]
            + [_entry(f"high{i}", 9, 95) for i in range(15)]
        )
        result = select_conversations_for_shift(items, "morning", "2026-05-22")
        seen: set[str] = set()
        for key in ("priority_1", "priority_2", "priority_3", "priority_4"):
            for x in result[key]:
                eid = x["conversation"]["external_id"]
                self.assertNotIn(eid, seen, f"duplicate {eid} across priorities")
                seen.add(eid)


if __name__ == "__main__":
    unittest.main()
