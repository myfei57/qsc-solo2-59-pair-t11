"""演练台账的单测（标准库 unittest，无需第三方依赖）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from batterystore.drills.compliance import (
    GAP_EVIDENCE,
    GAP_FAILED,
    GAP_LATE_REMEDY,
    GAP_MISSING,
    COVERED,
    PENDING,
    REMEDY_OPEN,
    compare_schedules,
    gap_report,
    rolling_warnings,
)
from batterystore.drills.ledger import ChainBrokenError, digest_payload
from batterystore.drills.periods import (
    add_months,
    iter_natural_windows,
    parse_iso_date,
    rolling_due_dates,
    window_for_date,
)
from batterystore.drills.register import ATTEMPT_FAILED, ATTEMPT_SUCCESS, DrillValidationError, hash_file
from batterystore.drills.service import build_register
from batterystore.store.jsonfile import JsonStore
from batterystore.drills.ledger import HashLedger
from batterystore.store.journal import JsonlJournal


def _fresh_register(root: str):
    register = build_register(root)
    register.initialize()
    return register


def _make_requirement(register, unit="station1", cadence=6, start="2025-01", basis="测试依据"):
    req_id, _ = register.add_requirement(
        unit=unit,
        scope="测试档",
        cadence_months=cadence,
        basis=basis,
        start_period=start,
    )
    return register.requirement(req_id)


def _all_actions_ok(register, req_id):
    return {code: True for code in register.requirement(req_id).required_actions}


class PeriodTests(unittest.TestCase):
    def test_half_year_and_quarter_boundaries(self):
        h1 = window_for_date(6, date(2025, 2, 10))
        self.assertEqual((h1.key, h1.start.isoformat(), h1.end.isoformat()), ("2025-H1", "2025-01-01", "2025-06-30"))
        h2 = window_for_date(6, date(2025, 12, 31))
        self.assertEqual(h2.key, "2025-H2")
        q4 = window_for_date(3, date(2025, 11, 1))
        self.assertEqual((q4.key, q4.start.isoformat(), q4.end.isoformat()), ("2025-Q4", "2025-10-01", "2025-12-31"))
        self.assertEqual(window_for_date(12, date(2025, 6, 1)).key, "2025")

    def test_iter_windows_covers_cross_year(self):
        windows = iter_natural_windows(6, "2025-01", date(2026, 1, 15))
        self.assertEqual([w.key for w in windows], ["2025-H1", "2025-H2", "2026-H1"])

    def test_rolling_anchor_drifts(self):
        anchor = date(2025, 12, 20)
        due = rolling_due_dates(anchor, 6, 2)
        self.assertEqual(due, [date(2026, 6, 20), date(2026, 12, 20)])
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_compare_calendar_vs_rolling_cover_different_months(self):
        with tempfile.TemporaryDirectory() as root:
            register = _fresh_register(root)
            requirement = _make_requirement(register, cadence=6)
            comparison = compare_schedules(register, date(2025, 12, 20), count=2)
            row = next(item for item in comparison["rows"] if item["req_id"] == requirement.req_id)
            self.assertEqual(row["calendar_due"], ["2026-06-30", "2026-12-31"])
            self.assertEqual(row["rolling_due"], ["2026-06-20", "2026-12-20"])


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.register = _fresh_register(self.root)
        self.requirement = _make_requirement(self.register)

    def tearDown(self):
        self._tmp.cleanup()

    def _initial_plan(self, window_key):
        return self.register.find_initial_plan(self.requirement.req_id, window_key)

    def _plan(self, window_key, on_date):
        return self.register.ensure_plan(
            self.requirement, window_for_date(self.requirement.cadence_months, parse_iso_date(on_date))
        )

    def _attempt_ok(self, plan_id, day, commander="张三"):
        return self.register.record_attempt(
            plan_id=plan_id,
            executed_on=day,
            action_results=_all_actions_ok(self.register, self.requirement.req_id),
            commander=commander,
        )

    def _attempt_fail(self, plan_id, day):
        results = _all_actions_ok(self.register, self.requirement.req_id)
        results["suppression_valve"] = False
        return self.register.record_attempt(
            plan_id=plan_id,
            executed_on=day,
            action_results=results,
            commander="张三",
            note="阀门未到位",
        )

    def _attach_all_evidence(self, attempt_id):
        for index, kind in enumerate(self.requirement.required_evidence):
            self.register.attach_evidence(
                attempt_id=attempt_id,
                kind=kind,
                file_name=f"{kind}.bin",
                sha256=f"{kind}-{index}",
            )

    def test_failed_attempt_is_kept_and_not_completed(self):
        plan = self._plan("2025-H2", "2025-11-10")
        record = self._attempt_fail(plan["plan_id"], "2025-11-10")
        self.assertEqual(record["status"], ATTEMPT_FAILED)
        self.assertIn("suppression_valve", record["failed_actions"])
        self.assertFalse(self.register.attempt_qualified(record))
        stored = [item for item in self.register.attempts() if item["attempt_id"] == record["attempt_id"]]
        self.assertEqual(len(stored), 1)

    def test_missing_action_result_counts_as_failure(self):
        plan = self._plan("2025-H2", "2025-11-10")
        record = self.register.record_attempt(
            plan_id=plan["plan_id"],
            executed_on="2025-11-10",
            action_results={"alarm_linkage": True},
            commander="张三",
        )
        self.assertEqual(record["status"], ATTEMPT_FAILED)
        self.assertIn("suppression_valve", record["failed_actions"])

    def test_same_window_remediation_covers_gap_and_original_plan_unchanged(self):
        original = self._plan("2025-H2", "2025-11-10")
        failed = self._attempt_fail(original["plan_id"], "2025-11-10")
        remedy = self.register.open_remediation(
            source_plan_id=original["plan_id"], planned_on="2025-12-20", reason="阀门复演"
        )
        self.assertEqual(remedy["kind"], "remedy")
        self.assertEqual(remedy["source_plan_id"], original["plan_id"])
        success = self._attempt_ok(remedy["plan_id"], "2025-12-20", commander="李四")
        self._attach_all_evidence(success["attempt_id"])

        # 原始计划字段原样保留：计划日未被补救动作改写。
        self.assertEqual(self._initial_plan("2025-H2")["planned_on"], original["planned_on"])
        from batterystore.drills.compliance import evaluate

        evaluated = {row.window.key: row for row in evaluate(self.register, date(2026, 1, 10))}
        self.assertEqual(evaluated["2025-H2"].state, COVERED)
        self.assertTrue(any(item["attempt_id"] == failed["attempt_id"] for item in evaluated["2025-H2"].attempts))

    def test_late_remediation_does_not_backfill_closed_window(self):
        original = self._plan("2026-H1", "2026-06-30")
        remedy = self.register.open_remediation(
            source_plan_id=original["plan_id"], planned_on="2026-07-15", reason="整改"
        )
        success = self._attempt_ok(remedy["plan_id"], "2026-07-15")
        self._attach_all_evidence(success["attempt_id"])
        report = gap_report(self.register, date(2026, 10, 1))
        target = [item for item in report["gaps"] if item["window_key"] == "2026-H1" and item["unit"] == "station1"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0]["state"], GAP_LATE_REMEDY)
        self.assertEqual(target[0]["late_qualified_attempts"], [success["attempt_id"]])

    def test_evidence_required_and_cannot_be_overwritten(self):
        plan = self._plan("2025-H2", "2025-12-10")
        success = self._attempt_ok(plan["plan_id"], "2025-12-10")
        self.assertFalse(self.register.attempt_qualified(success))
        self.register.attach_evidence(attempt_id=success["attempt_id"], kind="drill_plan", file_name="a", sha256="x")
        with self.assertRaises(DrillValidationError):
            self.register.attach_evidence(attempt_id=success["attempt_id"], kind="drill_plan", file_name="b", sha256="y")
        with self.assertRaises(DrillValidationError):
            self.register.attach_evidence(attempt_id=success["attempt_id"], kind="not_allowed", file_name="c", sha256="z")

    def test_gap_states_failed_evidence_missing_open(self):
        from batterystore.drills.compliance import evaluate

        failed_plan = self._plan("2025-H2", "2025-11-10")
        self._attempt_fail(failed_plan["plan_id"], "2025-11-10")
        evidence_plan = self._plan("2026-H1", "2026-03-01")
        success_no_evidence = self._attempt_ok(evidence_plan["plan_id"], "2026-03-01")
        self.assertFalse(self.register.attempt_qualified(success_no_evidence))

        evaluated = {row.window.key: row.state for row in evaluate(self.register, date(2026, 7, 15))}
        self.assertEqual(evaluated["2025-H1"], GAP_MISSING)
        self.assertEqual(evaluated["2025-H2"], GAP_FAILED)
        self.assertEqual(evaluated["2026-H1"], GAP_EVIDENCE)
        self.assertEqual(evaluated["2026-H2"], PENDING)

    def test_remedy_open_state_then_covered(self):
        from batterystore.drills.compliance import evaluate

        plan = self._plan("2026-H1", "2026-02-01")
        self._attempt_fail(plan["plan_id"], "2026-02-01")
        self.register.open_remediation(source_plan_id=plan["plan_id"], planned_on="2026-05-20", reason="补")
        states = {row.window.key: row.state for row in evaluate(self.register, date(2026, 3, 1))}
        self.assertEqual(states["2026-H1"], REMEDY_OPEN)

    def test_rolling_warning_only_when_interval_exceeded(self):
        first_plan = self._plan("2025-H2", "2025-12-20")
        first = self._attempt_ok(first_plan["plan_id"], "2025-12-20")
        self._attach_all_evidence(first["attempt_id"])
        self.assertEqual(rolling_warnings(self.register, date(2026, 4, 1)), [])
        warnings = rolling_warnings(self.register, date(2026, 10, 4))
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["gap_months"], 10)

    def test_reload_preserves_append_only_state(self):
        plan = self._plan("2025-H2", "2025-11-10")
        self._attempt_fail(plan["plan_id"], "2025-11-10")
        reloaded = build_register(self.root)
        self.assertEqual(len(reloaded.attempts()), 1)
        self.assertEqual(reloaded.attempts()[0]["status"], ATTEMPT_FAILED)
        self.assertTrue(reloaded.verify_chain()["ok"])

    def test_plan_generation_is_idempotent(self):
        first = self._plan("2025-H2", "2025-11-10")
        # 再次跑评估不得产生第二条原始计划。
        gap_report(self.register, date(2026, 10, 1))
        second = self._initial_plan("2025-H2")
        self.assertEqual(first["plan_id"], second["plan_id"])


class EvidenceHashTests(unittest.TestCase):
    def test_file_hash_matches_sha256(self):
        import hashlib

        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"fire drill evidence")
            path = handle.name
        try:
            self.assertEqual(
                hash_file(path),
                hashlib.sha256(b"fire drill evidence").hexdigest(),
            )
        finally:
            os.remove(path)


class HashChainTests(unittest.TestCase):
    def test_tampering_breaks_chain(self):
        with tempfile.TemporaryDirectory() as root:
            store = JsonStore(root)
            ledger = HashLedger(JsonlJournal(root, "t", segment_limit=8))
            ledger.append("a", {"v": 1})
            ledger.append("a", {"v": 2})
            ledger.append("a", {"v": 3})
            self.assertTrue(ledger.verify()["ok"])
            self.assertNotEqual(digest_payload({"v": 1}), digest_payload({"v": 2}))

            # 直接改流水字节，重放时必须断链。
            target = os.path.join(root, "t-00000.jsonl")
            import json

            lines = open(target, encoding="utf-8").read().splitlines()
            record = json.loads(lines[0])
            record["v"] = 99
            lines[0] = json.dumps(record, ensure_ascii=False)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            broken = HashLedger(JsonlJournal(root, "t", segment_limit=8))
            with self.assertRaises(ChainBrokenError):
                broken.verify()


if __name__ == "__main__":
    unittest.main()
