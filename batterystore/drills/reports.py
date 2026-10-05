"""检查报告的人类可读渲染。"""

from __future__ import annotations

import unicodedata

from .compliance import STATE_LABELS
from .regulations import ACTION_LABELS, EVIDENCE_LABELS


def display_width(text: str) -> int:
    """按终端宽度估算：全角字符占两列。"""

    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    """按显示宽度补齐到 ``width`` 列。"""

    raw = str(text)
    return raw + " " * max(0, width - display_width(raw))


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """渲染一张等宽文本表。"""

    widths = [display_width(head) for head in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))
    line = "  ".join(pad(head, widths[index]) for index, head in enumerate(headers))
    divider = "  ".join("-" * widths[index] for index in range(len(headers)))
    body = ["  ".join(pad(cell, widths[index]) for index, cell in enumerate(row)) for row in rows]
    return "\n".join([line, divider, *body]) if rows else line


def render_requirements(requirements: list) -> str:
    rows = [
        [
            item.req_id,
            item.unit,
            item.scope,
            f"{item.cadence_months}个月",
            item.start_period,
            item.basis,
        ]
        for item in requirements
    ]
    return render_table(["要求ID", "适用单位", "档级", "周期", "起始", "合规依据"], rows)


def render_plans(plans: list[dict]) -> str:
    rows = [
        [
            plan["plan_id"],
            plan["req_id"],
            plan["window_label"],
            plan["window_start"],
            plan["window_end"],
            plan["planned_on"],
            "原始" if plan["kind"] == "initial" else f"补救←{plan['source_plan_id']}",
        ]
        for plan in plans
    ]
    return render_table(["计划ID", "要求", "窗口", "起", "止", "计划日", "类型"], rows)


def render_attempts(register, attempts: list[dict]) -> str:
    rows = []
    for item in attempts:
        present = {ev["kind"] for ev in register.evidence_for(item["attempt_id"])}
        missing = [EVIDENCE_LABELS.get(code, code) for code in register.requirement(item["req_id"]).required_evidence if code not in present]
        failed = [ACTION_LABELS.get(code, code) for code in item["failed_actions"]]
        rows.append(
            [
                item["attempt_id"],
                item["plan_id"],
                item["executed_on"],
                "成功" if item["status"] == "success" else "失败",
                ",".join(failed) or "-",
                ",".join(missing) or "-",
                item["commander"],
            ]
        )
    return render_table(["尝试ID", "计划", "执行日", "动作", "失败动作", "缺证据", "指挥"], rows)


def render_gap_report(report: dict) -> str:
    lines = [
        f"合规检查缺口清单（基准日 {report['as_of']}，判定口径：自然周期）",
        "=" * 56,
        "窗口总数 {windows}｜已覆盖 {covered}｜缺口 {gaps}｜进行中 {open}".format(**report["counts"]),
    ]
    if report["gaps"]:
        lines.append("")
        lines.append("【必须说明的缺口】")
        rows = []
        for item in report["gaps"]:
            rows.append(
                [
                    item["unit"],
                    item["window_label"],
                    STATE_LABELS[item["state"]],
                    item["window_start"],
                    item["window_end"],
                    ",".join(item["missing_evidence_labels"]) or "-",
                    ",".join(item["late_qualified_attempts"]) or "-",
                ]
            )
        lines.append(
            render_table(
                ["单位", "窗口", "状态", "窗口起", "窗口止", "缺证据", "跨窗补做"],
                rows,
            )
        )
    if report["open_risks"]:
        lines.append("")
        lines.append("【进行中窗口风险（尚可同窗口补救）】")
        rows = []
        for item in report["open_risks"]:
            rows.append(
                [
                    item["unit"],
                    item["window_label"],
                    STATE_LABELS[item["state"]],
                    str(item["days_left"]),
                    "是" if item["due_soon"] else "否",
                    ",".join(item["failed_action_labels"]) or "-",
                ]
            )
        lines.append(render_table(["单位", "窗口", "状态", "剩余天", "临近", "失败动作"], rows))
    if report["rolling_warnings"]:
        lines.append("")
        lines.append("【滚动间隔加严预警（不改变自然周期判定）】")
        rows = [
            [
                item["unit"],
                item["from"],
                item["to"],
                f"{item['gap_months']}/{item['limit_months']}",
                item.get("next_due", "-"),
            ]
            for item in report["rolling_warnings"]
        ]
        lines.append(render_table(["单位", "上次合格", "本次/今日", "间隔/限值(月)", "应到期"], rows))
    if not report["gaps"] and not report["open_risks"]:
        lines.append("")
        lines.append("全部已到期窗口均已覆盖。")
    return "\n".join(lines)


def render_compare(comparison: dict) -> str:
    lines = [f"排期口径对比（锚点 {comparison['anchor']}，后 {comparison['count']} 次）", "=" * 56]
    rows = []
    for item in comparison["rows"]:
        rows.append(
            [
                item["unit"],
                f"{item['cadence_months']}个月",
                "→".join(item["calendar_due"]),
                "→".join(item["rolling_due"]),
            ]
        )
    lines.append(render_table(["单位", "档级", "自然周期", "滚动间隔"], rows))
    return "\n".join(lines)
