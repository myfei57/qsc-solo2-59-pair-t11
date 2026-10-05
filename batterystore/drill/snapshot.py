"""演练台账快照与缺口评估。

缺口判定以"自然周期内有没有成功、且证据完好的演练"为唯一口径：

* ``covered``       周期内有成功演练，证据指纹校验通过；
* ``covered_late``  缺口由期外补做覆盖（补做是事实，但按期仍要单列，
                    检查时说明"已整改、超期"）；
* ``evidence_gap``  演练成功了，但没有证据或证据被改动、缺失；
* ``failed``        只有失败记录（动作失败不算完成，但失败记录保留可见）；
* ``missing``       周期内没有任何执行记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .period import parse_date

STATUS_COVERED = "covered"
STATUS_COVERED_LATE = "covered_late"
STATUS_EVIDENCE_GAP = "evidence_gap"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"

# 检查前需要优先处理的状态（covered_late 单列说明，不算未闭合缺口）。
OPEN_STATUSES = (STATUS_EVIDENCE_GAP, STATUS_FAILED, STATUS_MISSING)


@dataclass
class DrillItem:
    """一条不可修改的原始计划项及其追加的执行记录。"""

    item_id: str
    requirement_id: str
    title: str
    frequency: str
    period: str
    period_start: str
    period_end: str
    due: str
    zone: str
    basis: str
    plan_ts: float
    runs: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "requirement_id": self.requirement_id,
            "title": self.title,
            "frequency": self.frequency,
            "period": self.period,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "due": self.due,
            "zone": self.zone,
            "basis": self.basis,
            "plan_ts": self.plan_ts,
            "runs": [dict(run) for run in self.runs],
        }


def evaluate_item(item: DrillItem, verify_evidence, as_of: str | None = None) -> dict:
    """评估单个周期计划项的完成状态（不改变台账，只给结论）。"""

    window_start = parse_date(item.period_start)
    window_end = parse_date(item.period_end)

    successful: list[dict] = []
    failures: list[dict] = []
    for run in item.runs:
        if run["ok"]:
            successful.append(run)
        else:
            failures.append(run)

    evidence_ok: list[dict] = []
    evidence_bad: list[dict] = []
    for run in successful:
        descriptors = list(run.get("evidence", []))
        # 联动报告是"真实动作"的自动留痕；成功批次至少要有一份完好证据。
        for descriptor in descriptors:
            verdict = verify_evidence(descriptor)
            (evidence_ok if verdict["ok"] else evidence_bad).append(
                {"evidence_id": verdict["evidence_id"], "ok": verdict["ok"], "reason": verdict.get("reason", "")}
            )

    if successful and evidence_ok:
        chosen = sorted(successful, key=lambda run: run["date"])[0]
        success_date = parse_date(chosen["date"])
        if window_start <= success_date <= window_end:
            status = STATUS_COVERED
        else:
            status = STATUS_COVERED_LATE
        return _result(item, status, chosen, failures, evidence_ok, evidence_bad, as_of)

    if successful:
        return _result(item, STATUS_EVIDENCE_GAP, successful[0], failures, evidence_ok, evidence_bad, as_of)
    if failures:
        return _result(item, STATUS_FAILED, failures[-1], failures, evidence_ok, evidence_bad, as_of)
    return _result(item, STATUS_MISSING, None, failures, evidence_ok, evidence_bad, as_of)


def _result(
    item: DrillItem,
    status: str,
    effective: dict | None,
    failures: list[dict],
    evidence_ok: list[dict],
    evidence_bad: list[dict],
    as_of: str | None,
) -> dict:
    overdue = bool(as_of and parse_date(as_of) > parse_date(item.due) and status in OPEN_STATUSES)
    return {
        "item_id": item.item_id,
        "requirement_id": item.requirement_id,
        "title": item.title,
        "frequency": item.frequency,
        "period": item.period,
        "period_start": item.period_start,
        "period_end": item.period_end,
        "due": item.due,
        "zone": item.zone,
        "basis": item.basis,
        "status": status,
        "open": status in OPEN_STATUSES,
        "late": status == STATUS_COVERED_LATE,
        "overdue": overdue,
        "runs": len(item.runs),
        "failures": [run["run_id"] for run in failures],
        "effective_run": effective["run_id"] if effective else None,
        "effective_date": effective["date"] if effective else None,
        "evidence_ok": [entry["evidence_id"] for entry in evidence_ok],
        "evidence_bad": evidence_bad,
        "remediation": [
            {
                "run_id": run["run_id"],
                "date": run["date"],
                "kind": run["kind"],
                "remediated_run": run.get("remediated_run"),
            }
            for run in item.runs
            if run["kind"] == "remediation"
        ],
    }


def build_report(
    requirements: list[dict],
    items: list[DrillItem],
    verify_evidence,
    as_of: str | None = None,
    *,
    include_closed: bool = False,
) -> dict:
    """汇总全部已到期周期的状态，缺口排在最前；未到期周期不纳入检查。"""

    cutoff = parse_date(as_of) if as_of else None
    rows: list[dict] = []
    for item in items:
        if cutoff is not None and parse_date(item.period_start) > cutoff:
            continue
        rows.append(evaluate_item(item, verify_evidence, as_of))

    counts = {status: 0 for status in (
        STATUS_COVERED,
        STATUS_COVERED_LATE,
        STATUS_EVIDENCE_GAP,
        STATUS_FAILED,
        STATUS_MISSING,
    )}
    for row in rows:
        counts[row["status"]] += 1

    open_gaps = [row for row in rows if row["open"]]
    late_covered = [row for row in rows if row["late"]]
    open_gaps.sort(key=lambda row: row["due"])
    rows.sort(key=lambda row: (row["status"] != STATUS_MISSING, row["due"]))

    selected = rows if include_closed else [row for row in rows if row["open"] or row["late"]]
    return {
        "calendar_basis": "natural",
        "as_of": as_of,
        "requirements": [dict(req) for req in requirements],
        "counts": counts,
        "open_gap_count": len(open_gaps),
        "late_covered_count": len(late_covered),
        "gaps": open_gaps,
        "late_covered": late_covered,
        "items": selected,
    }
