"""消防演练合规台账：周期排期、失败留痕、补救追加、缺口与跨年口径。"""

from __future__ import annotations

from datetime import date

import pytest

from batterystore.drill.period import (
    add_months,
    compare_calendars,
    iter_period_keys,
    natural_schedule,
    parse_date,
    parse_period_label,
    period_label,
    period_window,
    rolling_due_after,
)
from batterystore.drill.snapshot import (
    STATUS_COVERED,
    STATUS_COVERED_LATE,
    STATUS_EVIDENCE_GAP,
    STATUS_FAILED,
    STATUS_MISSING,
)
from batterystore.errors import InterlockError


def test_period_windows_split_natural_quarters():
    assert period_window("quarterly", 2025, 1) == (date(2025, 1, 1), date(2025, 3, 31))
    assert period_window("quarterly", 2025, 4) == (date(2025, 10, 1), date(2025, 12, 31))
    assert period_window("half_year", 2025, 2) == (date(2025, 7, 1), date(2025, 12, 31))
    assert period_window("yearly", 2025, 1) == (date(2025, 1, 1), date(2025, 12, 31))
    assert period_label("quarterly", 2025, 4) == "2025-Q4"
    assert parse_period_label("half_year", "2025-H2") == (2025, 2)


def test_iter_period_keys_handles_leap_february_and_year_boundary():
    keys = iter_period_keys("quarterly", date(2025, 12, 15), date(2026, 2, 1))
    assert keys == ["2025-Q4", "2026-Q1"]
    schedule = natural_schedule("monthly", date(2024, 1, 1), date(2024, 3, 1))
    assert [slot["due"] for slot in schedule] == ["2024-01-31", "2024-02-29", "2024-03-31"]


def test_add_months_clamps_to_month_end():
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 12, 31), 2) == date(2025, 2, 28)
    assert rolling_due_after(date(2025, 11, 20), "quarterly") == date(2026, 2, 20)


def test_compare_calendars_shows_year_end_divergence():
    comparison = compare_calendars("quarterly", date(2025, 11, 20), [2025, 2026])
    by_year = {row["year"]: row for row in comparison["years"]}
    assert by_year[2025]["natural_months"] == [f"2025-{m:02d}" for m in range(1, 13)]
    assert by_year[2026]["natural_months"] == [f"2026-{m:02d}" for m in range(1, 13)]
    # 滚动口径：每年 12 月要等次年 2 月那次演练才产生闭合证据，年末检查无法在当年闭合。
    assert by_year[2025]["rolling_unproven_at_year_end"] == ["2025-12"]
    assert by_year[2026]["rolling_unproven_at_year_end"] == ["2026-12"]
    # 次年 2 月的演练跨回去覆盖上年 12 月，跨年拼接关系单列。
    assert by_year[2026]["rolling_straddles_from_prior"] == ["2025-12"]
    assert comparison["selected_calendar"] == "natural"


def test_natural_schedule_with_day_of_period():
    schedule = natural_schedule("quarterly", date(2025, 1, 1), date(2025, 6, 30), day_of_period=15)
    assert [slot["due"] for slot in schedule] == ["2025-01-15", "2025-04-15"]
    assert schedule[0]["period"] == "2025-Q1"


def _prepare(platform, start="2025-01-01", end="2026-12-31"):
    """登记季度要求并排两个自然年的计划，返回季度计划项。"""

    platform.drill_register_defaults()
    platform.drill_plan(start, end)
    plans = platform.drill_plans()
    quarterly = [item for item in plans if item["frequency"] == "quarterly"]
    half_year = [item for item in plans if item["frequency"] == "half_year"]
    return quarterly, half_year


# ManualClock 默认基线在 2025-09；台账测试统一改到 2025-01-01，再按秒推进到目标月。
def _set_jan_first(clock):
    clock._now = 1735689600.0
    clock._mono = 0.0


MONTH_S = 2_600_000.0


def test_plan_generation_is_idempotent_and_calendar_based(platform):
    platform.drill_register_defaults()
    first = platform.drill_plan("2025-01-01", "2025-12-31")
    assert len(first["created"]) == 6  # 季度 4 次 + 半年 2 次
    second = platform.drill_plan("2025-01-01", "2025-12-31")
    assert second["created"] == []
    assert len(second["skipped"]) == 6
    assert all(item["period_end"] is not None for item in first["created"])
    policy = platform.drill_policy()
    assert policy["selected_calendar"] == "natural"
    assert policy["references"]


def test_failed_real_action_is_kept_but_not_completed(platform, clock):
    quarterly, _half = _prepare(platform)
    q1 = quarterly[0]
    # 2025-Q1 到期日 3/31；执行日推进到 3 月中旬，阀门确认窗口不足 → 真实动作失败。
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    outcome = platform.drill_run_scheduled(q1["item_id"], confirm_dt=0.5)
    assert outcome["ok"] is False
    assert "valve_not_confirmed" in outcome["report"]["violations"]
    detail = platform.drill_item(q1["item_id"])
    assert detail["evaluation"]["status"] == STATUS_FAILED
    assert detail["evaluation"]["failures"] == [outcome["run"]["run_id"]]
    # 失败批次仍有自动留痕的联动报告证据。
    runs = platform.drill_runs()
    assert runs[-1]["ok"] is False
    assert runs[-1]["evidence"]
    assert runs[-1]["evidence"][0]["kind"] == "linkage_report"


def test_same_period_remediation_closes_gap_without_touching_original_plan(platform, clock):
    quarterly, _half = _prepare(platform)
    q1 = quarterly[0]
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    failed = platform.drill_run_scheduled(q1["item_id"], confirm_dt=0.5)
    original = platform.drill_item(q1["item_id"])["item"]

    # 原计划不能重跑，只能追加补救。
    with pytest.raises(InterlockError):
        platform.drill_run_scheduled(q1["item_id"])
    platform.release_fire()
    remedied = platform.drill_run_remediation(q1["item_id"], confirm_dt=5.0, note="阀门复位后补做")
    assert remedied["ok"] is True
    assert remedied["remediated_run"] == failed["run"]["run_id"]

    detail = platform.drill_item(q1["item_id"])
    assert detail["evaluation"]["status"] == STATUS_COVERED
    kinds = [run["kind"] for run in detail["item"]["runs"]]
    assert kinds == ["scheduled", "remediation"]
    assert detail["item"]["runs"][0]["ok"] is False
    # 原始计划字段原样保留：周期、到期日、计划签发时间不变。
    assert detail["item"]["period"] == original["period"]
    assert detail["item"]["due"] == original["due"]
    assert detail["item"]["plan_ts"] == original["plan_ts"]
    assert detail["evaluation"]["remediation"][0]["run_id"] == remedied["run"]["run_id"]


def test_cross_year_remediation_is_listed_as_covered_late(platform, clock):
    # 只排 2025 Q4：在 12 月失败，拖到次年 1 月才补做成功。
    platform.drill_register("x_q", "季度联动", "quarterly")
    platform.drill_plan("2025-10-01", "2025-12-31")
    item = platform.drill_plans()[0]
    _set_jan_first(clock)
    clock.advance(11 * MONTH_S)  # 推进到 2025 年 12 月
    platform.drill_run_scheduled(item["item_id"], confirm_dt=0.5)
    clock.advance(1.5 * MONTH_S)  # 推进到 2026 年 1 月
    platform.release_fire()
    outcome = platform.drill_run_remediation(item["item_id"], confirm_dt=5.0)
    assert outcome["ok"] is True
    gaps = platform.drill_gaps(as_of_iso="2026-01-31")
    late = {row["item_id"]: row for row in gaps["late_covered"]}
    assert item["item_id"] in late
    assert late[item["item_id"]]["status"] == STATUS_COVERED_LATE
    assert gaps["open_gap_count"] == 0
    report = platform.drill_report(as_of_iso="2026-01-31")
    assert report["counts"][STATUS_COVERED_LATE] == 1


def test_missing_and_failed_gaps_listed_before_inspection(platform, clock):
    quarterly, half_year = _prepare(platform)
    # Q1 成功、Q2 失败、Q3/Q4 不做。
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)  # 3 月
    q1 = platform.drill_run_scheduled(quarterly[0]["item_id"])
    assert q1["ok"]
    clock.advance(3 * MONTH_S)  # 6 月
    platform.release_fire()
    platform.drill_run_scheduled(quarterly[1]["item_id"], confirm_dt=0.5)

    gaps = platform.drill_gaps(as_of_iso="2025-12-31")
    statuses = {row["period"]: row["status"] for row in gaps["gaps"]}
    assert statuses["2025-Q2"] == STATUS_FAILED
    assert statuses["2025-Q3"] == STATUS_MISSING
    assert statuses["2025-Q4"] == STATUS_MISSING
    # 半年档两期也缺；Q1 已闭合，不在缺口清单。
    assert "2025-Q1" not in statuses
    assert gaps["open_gap_count"] == len(gaps["gaps"])
    assert gaps["gaps"][0]["due"] <= gaps["gaps"][-1]["due"]


def test_successful_run_without_intact_evidence_is_evidence_gap(platform, clock, tmp_path):
    platform.drill_register("x_y", "年度演练", "yearly")
    platform.drill_plan("2025-01-01", "2025-12-31")
    item = platform.drill_plans()[0]
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    platform.drill_run_scheduled(item["item_id"])
    assert platform.drill_item(item["item_id"])["evaluation"]["status"] == STATUS_COVERED
    # 模拟证据文件被篡改（等长替换，指纹变了但字节数不变）。
    descriptor = platform.drill_runs()[0]["evidence"][0]
    path = descriptor["path"]
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read().replace("valve_confirm", "valve_confXrm", 1)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    result = platform.drill_item(item["item_id"])
    assert result["evaluation"]["status"] == STATUS_EVIDENCE_GAP
    assert result["evaluation"]["evidence_bad"][0]["reason"] == "hash_mismatch"


def test_attached_field_evidence_persists_with_hash(platform, clock, tmp_path):
    platform.drill_register("x_m", "月度演练", "monthly")
    platform.drill_plan("2025-03-01", "2025-03-31")
    item = platform.drill_plans()[0]
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    outcome = platform.drill_run_scheduled(item["item_id"])
    descriptor = platform.drill_attach_evidence(
        outcome["run"]["run_id"], b"sign-sheet-bytes", filename="sign.csv", content_type="text/csv", note="签到表"
    )
    assert descriptor["sha256"]
    verdict = platform.drill.evidence.verify(descriptor)
    assert verdict["ok"]
    detail = platform.drill_item(item["item_id"])
    assert len(detail["evaluation"]["evidence_ok"]) == 2


def test_remediation_rejected_when_period_already_closed(platform, clock):
    platform.drill_register("x_h", "半年演练", "half_year")
    platform.drill_plan("2025-01-01", "2025-06-30")
    item = platform.drill_plans()[0]
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    platform.drill_run_scheduled(item["item_id"])
    with pytest.raises(InterlockError):
        platform.drill_run_remediation(item["item_id"])


def test_remediation_without_any_prior_run_rejected(platform):
    platform.drill_register("x_q2", "季度演练2", "quarterly")
    platform.drill_plan("2025-01-01", "2025-03-31")
    item = platform.drill_plans()[0]
    with pytest.raises(InterlockError):
        platform.drill_run_remediation(item["item_id"])


def test_register_duplicate_requirement_rejected(platform):
    platform.drill_register("dup", "重复", "yearly")
    with pytest.raises(InterlockError):
        platform.drill_register("dup", "重复再来", "yearly")


def test_ledger_is_append_only_and_survives_rebuild(platform, clock, tmp_path):
    quarterly, _half = _prepare(platform)
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    failed = platform.drill_run_scheduled(quarterly[0]["item_id"], confirm_dt=0.5)
    platform.release_fire()
    remedied = platform.drill_run_remediation(quarterly[0]["item_id"])

    from batterystore.app import build_platform
    from batterystore.config import default_config

    rebuilt = build_platform(
        default_config(), clock=clock, storage_root=platform.store.root, rack_count=2
    )
    detail = rebuilt.drill_item(quarterly[0]["item_id"])
    assert [run["run_id"] for run in detail["item"]["runs"]] == [
        failed["run"]["run_id"],
        remedied["run"]["run_id"],
    ]
    assert detail["evaluation"]["status"] == STATUS_COVERED
    # 流水只追加：条目数量 = 2 要求 + 计划项 + 2 批次 + 2 自动证据。
    assert rebuilt.drill._journal.count() == 2 + len(platform.drill_plans()) + 2 + 2


def test_platform_runner_uses_real_linkage_and_trips_pcs(platform, clock):
    platform.drill_register("real", "真实联动", "quarterly")
    platform.drill_plan("2025-01-01", "2025-03-31")
    item = platform.drill_plans()[0]
    _set_jan_first(clock)
    clock.advance(2.5 * MONTH_S)
    outcome = platform.drill_run_scheduled(item["item_id"])
    assert outcome["ok"] is True
    assert platform.pcs.cut.is_tripped() is True
    assert outcome["report"]["steps"]
