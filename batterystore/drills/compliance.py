"""合规缺口判定。

判定只走自然周期口径：枚举每条要求从起始周期到基准日的全部自然窗口，逐窗核对
有没有"现场动作全部成功 + 必需证据齐全"的合格尝试；原始计划与补救计划下的
尝试同等参与覆盖，但窗口关闭后才做的补救不回溯，原缺口保留、另记整改完成。

滚动间隔不算合规结论，只在两条合格演练之间超过档级月数时给出加严预警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .periods import (
    NaturalWindow,
    add_months,
    iter_natural_windows,
    months_between,
    rolling_due_dates,
)
from .register import PLAN_REMEDY, DrillRegister
from .regulations import ACTION_LABELS, EVIDENCE_LABELS

# 窗口状态
COVERED = "covered"                  # 窗口内有合格演练
PENDING = "pending"                  # 窗口进行中，尚未合格，尚可补
GAP_FAILED = "gap_failed"            # 有演练但动作失败
GAP_EVIDENCE = "gap_evidence"        # 动作成功但证据不齐
GAP_MISSING = "gap_missing"          # 窗口内没有任何演练
GAP_LATE_REMEDY = "gap_late_remedy"  # 窗口关闭后才补做合格，缺口保留
REMEDY_OPEN = "remedy_open"          # 已追加同窗口补救计划，待执行

OPEN_STATES = (PENDING, REMEDY_OPEN)
GAP_STATES = (GAP_FAILED, GAP_EVIDENCE, GAP_MISSING, GAP_LATE_REMEDY)

STATE_LABELS = {
    COVERED: "已覆盖",
    PENDING: "进行中待执行",
    GAP_FAILED: "缺口-演练失败",
    GAP_EVIDENCE: "缺口-证据不全",
    GAP_MISSING: "缺口-未演练",
    GAP_LATE_REMEDY: "缺口-跨窗口补救不回溯",
    REMEDY_OPEN: "已开补救待执行",
}


@dataclass
class WindowRow:
    """一个要求在一个自然窗口上的判定行。"""

    requirement: Any
    window: NaturalWindow
    state: str
    days_left: int
    plans: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    late_qualified: list[str] = field(default_factory=list)
    within_remedy: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "req_id": self.requirement.req_id,
            "unit": self.requirement.unit,
            "scope": self.requirement.scope,
            "cadence_months": self.requirement.cadence_months,
            "window": self.window.as_dict(),
            "window_key": self.window.key,
            "window_label": self.window.label,
            "window_start": self.window.start.isoformat(),
            "window_end": self.window.end.isoformat(),
            "state": self.state,
            "state_label": STATE_LABELS[self.state],
            "days_left": self.days_left,
            "plans": self.plans,
            "attempt_ids": [item["attempt_id"] for item in self.attempts],
            "missing_evidence": self.missing_evidence,
            "missing_evidence_labels": [EVIDENCE_LABELS.get(code, code) for code in self.missing_evidence],
            "late_qualified_attempts": self.late_qualified,
            "remedy_plans": self.within_remedy,
        }


def _window_attempts(register: DrillRegister, plans: list[dict]) -> list[dict]:
    plan_ids = {plan["plan_id"] for plan in plans}
    return [item for item in register.attempts() if item["plan_id"] in plan_ids]


def _classify_window(register: DrillRegister, plans: list[dict], window: NaturalWindow, as_of: date) -> tuple[str, list[str], list[str]]:
    attempts = _window_attempts(register, plans)
    inside = [item for item in attempts if window.contains(date.fromisoformat(item["executed_on"]))]
    inside_qualified = [item for item in inside if register.attempt_qualified(item)]
    if inside_qualified:
        return COVERED, [], []

    missing_evidence: list[str] = []
    inside_success = [item for item in inside if item["status"] == "success"]
    if inside_success:
        required = set(register.requirement(inside_success[0]["req_id"]).required_evidence)
        for item in inside_success:
            present = {ev["kind"] for ev in register.evidence_for(item["attempt_id"])}
            missing_evidence.extend(sorted(required - present))
        missing_evidence = sorted(set(missing_evidence))

    # 窗口关闭后的合格补做：不回溯，缺口保留。
    late = [
        item["attempt_id"]
        for item in attempts
        if not window.contains(date.fromisoformat(item["executed_on"]))
        and date.fromisoformat(item["executed_on"]) > window.end
        and register.attempt_qualified(item)
    ]
    # 有动作成功的尝试就先按证据缺口提示；只有全部失败才算演练失败。
    if late and as_of > window.end:
        state = GAP_LATE_REMEDY
    elif as_of <= window.end:
        state = PENDING if not inside else (GAP_EVIDENCE if inside_success else GAP_FAILED)
    elif inside:
        state = GAP_EVIDENCE if inside_success else GAP_FAILED
    else:
        state = GAP_MISSING
    return state, missing_evidence, late


def evaluate(register: DrillRegister, as_of: date) -> list[WindowRow]:
    """逐要求逐自然窗口判定，返回全部判定行（时间升序）。"""

    rows: list[WindowRow] = []
    for requirement in register.requirements():
        windows = iter_natural_windows(requirement.cadence_months, requirement.start_period, as_of)
        for window in windows:
            register.ensure_plan(requirement, window)
        plans = register.plans(requirement.req_id)
        for window in windows:
            window_plans = [plan for plan in plans if plan["window_key"] == window.key]
            state, missing_evidence, late = _classify_window(register, window_plans, window, as_of)
            remedy_plans = [
                plan
                for plan in window_plans
                if plan["kind"] == PLAN_REMEDY and window.contains(date.fromisoformat(plan["planned_on"]))
            ]
            if state in (GAP_FAILED, GAP_EVIDENCE, GAP_MISSING) and as_of <= window.end and remedy_plans:
                state = REMEDY_OPEN
            rows.append(
                WindowRow(
                    requirement=requirement,
                    window=window,
                    state=state,
                    days_left=(window.end - as_of).days,
                    plans=window_plans,
                    attempts=_window_attempts(register, window_plans),
                    missing_evidence=missing_evidence,
                    late_qualified=late,
                    within_remedy=remedy_plans,
                )
            )
    rows.sort(key=lambda row: (row.requirement.unit, row.window.start))
    return rows


def gap_report(register: DrillRegister, as_of: date, due_soon_days: int = 14) -> dict:
    """检查前缺口清单：缺口、进行中风险、已覆盖统计与滚动预警。"""

    rows = evaluate(register, as_of)
    gaps = [row.as_dict() for row in rows if row.state in GAP_STATES]
    open_rows = [row for row in rows if row.state in OPEN_STATES]
    risks = [
        {
            **row.as_dict(),
            "due_soon": row.days_left <= due_soon_days,
            "failed_actions": sorted(
                {
                    action
                    for item in row.attempts
                    if item["status"] == "failed"
                    for action in item["failed_actions"]
                }
            ),
            "failed_action_labels": [
                ACTION_LABELS.get(code, code)
                for code in sorted(
                    {
                        action
                        for item in row.attempts
                        if item["status"] == "failed"
                        for action in item["failed_actions"]
                    }
                )
            ],
        }
        for row in open_rows
    ]
    return {
        "as_of": as_of.isoformat(),
        "policy": "natural_window",
        "counts": {
            "covered": sum(1 for row in rows if row.state == COVERED),
            "gaps": len(gaps),
            "open": len(open_rows),
            "windows": len(rows),
        },
        "gaps": gaps,
        "open_risks": risks,
        "remediations": register.remediations(),
        "rolling_warnings": rolling_warnings(register, as_of),
    }


def rolling_warnings(register: DrillRegister, as_of: date) -> list[dict]:
    """滚动间隔预警：合格演练间隔超过档级月数即提示。"""

    warnings: list[dict] = []
    for requirement in register.requirements():
        qualified_days = sorted(
            date.fromisoformat(item["executed_on"])
            for item in register.attempts()
            if item["req_id"] == requirement.req_id
            and register.attempt_qualified(item)
            and date.fromisoformat(item["executed_on"]) <= as_of
        )
        if not qualified_days:
            continue
        for previous, current in zip(qualified_days, qualified_days[1:]):
            if months_between(previous, current) > requirement.cadence_months:
                warnings.append(
                    {
                        "req_id": requirement.req_id,
                        "unit": requirement.unit,
                        "from": previous.isoformat(),
                        "to": current.isoformat(),
                        "gap_months": months_between(previous, current),
                        "limit_months": requirement.cadence_months,
                    }
                )
        last_day = qualified_days[-1]
        if months_between(last_day, as_of) > requirement.cadence_months:
            next_due = add_months(last_day, requirement.cadence_months)
            warnings.append(
                {
                    "req_id": requirement.req_id,
                    "unit": requirement.unit,
                    "from": last_day.isoformat(),
                    "to": as_of.isoformat(),
                    "next_due": next_due.isoformat(),
                    "gap_months": months_between(last_day, as_of),
                    "limit_months": requirement.cadence_months,
                    "open": True,
                }
            )
    return warnings


def compare_schedules(register: DrillRegister, anchor: date, count: int = 4) -> dict:
    """并排比较自然周期与滚动间隔两种排法，直观看跨年覆盖差异。"""

    comparison = []
    for requirement in register.requirements():
        anchor_window = None
        for row_window in iter_natural_windows(requirement.cadence_months, requirement.start_period, anchor):
            anchor_window = row_window
        cursor = anchor_window.end if anchor_window else anchor
        natural: list[str] = []
        for step in range(1, count + 1):
            natural.append(add_months(cursor, requirement.cadence_months * step).isoformat())
        comparison.append(
            {
                "req_id": requirement.req_id,
                "unit": requirement.unit,
                "cadence_months": requirement.cadence_months,
                "anchor": anchor.isoformat(),
                "calendar_due": natural,
                "rolling_due": [day.isoformat() for day in rolling_due_dates(anchor, requirement.cadence_months, count)],
            }
        )
    return {"anchor": anchor.isoformat(), "count": count, "rows": comparison}
