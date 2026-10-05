"""消防演练合规台账。

排期口径的选择（合规依据见 :meth:`DrillProgram.policy` 与 :mod:`.period`）：

1. 《消防法》第十六条与公安部令第 61 号第四十条的频率表述是"每年 / 每半年
   至少一次"，监督检查按自然年（季度）核对台账，所以正式计划**只按自然周期**
   排，计划项与检查单元一一对应；
2. "距上次实际执行不超过一个周期"的滚动口径只保证间隔，保证不了某个自然季度
   里有演练——跨年时 12 月的义务要等下一年的计划才覆盖得到。系统保留
   :meth:`DrillProgram.compare_calendars` 对照视图说明差异，但不拿它排正式计划。

台账事件全部走只追加流水：要求登记、计划签发、执行批次、证据追加四类事件，
没有更新和删除入口。原始计划项一旦签发即冻结；动作失败的批次保留为失败记录，
不计完成；同一周期补做只追加 ``remediation`` 批次挂回原计划项，缺口闭合后原
计划与失败批次都原样保留。
"""

from __future__ import annotations

from typing import Any, Callable

from ..clock import Clock
from ..errors import InterlockError
from ..store.journal import JsonlJournal
from .evidence import EvidenceVault
from .period import (
    FREQUENCIES,
    compare_calendars,
    natural_schedule,
    parse_date,
    period_label,
    period_window,
    to_iso,
    today,
)
from .snapshot import DrillItem, build_report, evaluate_item

CALENDAR_NATURAL = "natural"

EVENT_REQUIREMENT = "requirement_registered"
EVENT_PLAN = "plan_issued"
EVENT_RUN = "drill_run"
EVENT_EVIDENCE = "evidence_attached"

# 站内默认登记的两档周期（更高档包含更低档义务，另存为独立计划项方便逐档核查）。
DEFAULT_REQUIREMENTS = (
    {
        "code": "statute_semiannual_linkage",
        "title": "灭火和应急疏散联动演练（法规底线：每半年一次）",
        "frequency": "half_year",
        "zone": "Z1",
        "basis": "公安部令第61号 第四十条：消防安全重点单位至少每半年进行一次演练",
    },
    {
        "code": "enhanced_quarterly_linkage",
        "title": "消防联动实操演练（站内控标：每季度一次）",
        "frequency": "quarterly",
        "zone": "Z1",
        "basis": "站内依据 GB/T 38315-2019 加严为每季度一次，并保留每次联动动作记录",
    },
)

# 排期口径选型说明，检查时可直接导出。
POLICY = {
    "selected_calendar": CALENDAR_NATURAL,
    "references": [
        {
            "document": "中华人民共和国消防法（2021年修正）",
            "article": "第十六条第（六）项",
            "point": "单位应当组织进行有针对性的消防演练",
        },
        {
            "document": "机关、团体、企业、事业单位消防安全管理规定（公安部令第61号，2019年修正）",
            "article": "第四十条",
            "point": "消防安全重点单位至少每半年进行一次演练；其他单位至少每年组织一次演练",
        },
        {
            "document": "GB/T 38315-2019 社会单位灭火和应急疏散预案编制及实施导则",
            "article": "演练实施与评估章节",
            "point": "演练应有方案、记录与评估，现场联动动作与影像签到等资料归档留存",
        },
    ],
    "rationale": [
        "法规频率以自然年/自然半年为单位，监督检查按自然周期核对台账；自然周期计划与检查单元一一对应",
        "同一自然周期内的补做直接挂回该周期计划项，周期是否闭合可以直接判定，原始计划不随执行时间移动",
        "证据固定挂在自然周期项下，跨年检查时不存在'这次演练算哪年的'口径争议",
    ],
    "rolling_not_accepted": (
        "滚动口径只保证相邻两次演练间隔不超过周期长度，保证不了每个自然季度内有演练；"
        "锚点随实际执行日后移，跨年时12月的义务要等下一年的计划才覆盖得到，"
        "自然年台账会出现空窗。系统仅提供对照视图，不作为正式排期。"
    ),
}

Runner = Callable[..., dict]


class DrillProgram:
    """消防演练合规台账（追加式流水 + 证据柜）。"""

    def __init__(self, clock: Clock, journal: JsonlJournal, evidence: EvidenceVault) -> None:
        self._clock = clock
        self._journal = journal
        self._evidence = evidence
        self._requirements: dict[str, dict] = {}
        self._items: dict[str, DrillItem] = {}
        self._requirement_seq = 0
        self._plan_seq = 0
        self._run_seq = 0
        self._replay()

    @property
    def evidence(self) -> EvidenceVault:
        return self._evidence

    def _replay(self) -> None:
        for event in self._journal.read_all():
            event_type = event.get("type")
            if event_type == EVENT_REQUIREMENT:
                requirement = dict(event["requirement"])
                self._requirements[requirement["requirement_id"]] = requirement
                self._requirement_seq = max(self._requirement_seq, int(requirement["requirement_id"].split("-")[1]))
            elif event_type == EVENT_PLAN:
                raw = dict(event["item"])
                item = DrillItem(**raw)
                self._items[item.item_id] = item
                self._plan_seq = max(self._plan_seq, int(item.item_id.split("-")[1]))
            elif event_type == EVENT_RUN:
                item = self._items[event["item_id"]]
                run = dict(event["run"])
                run.setdefault("evidence", [])
                item.runs.append(run)
                self._run_seq = max(self._run_seq, int(run["run_id"].split("-")[1]))
            elif event_type == EVENT_EVIDENCE:
                item = self._items[event["item_id"]]
                for run in item.runs:
                    if run["run_id"] == event["run_id"]:
                        run.setdefault("evidence", []).append(dict(event["evidence"]))

    def _append(self, event: dict) -> None:
        self._journal.append({"ts": self._clock.now(), **event})

    def register_default_requirements(self) -> list[dict]:
        """登记站内两档默认周期要求（已存在的档位跳过）。"""

        registered: list[dict] = []
        for spec in DEFAULT_REQUIREMENTS:
            if not any(req["code"] == spec["code"] for req in self._requirements.values()):
                registered.append(
                    self.register_requirement(
                        spec["code"], spec["title"], spec["frequency"], spec["zone"], spec["basis"]
                    )
                )
        return registered

    def register_requirement(self, code: str, title: str, frequency: str, zone: str = "Z1", basis: str = "") -> dict:
        """登记一档周期要求（要求本身也是追加事件，不在原记录上改写）。"""

        if frequency not in FREQUENCIES:
            raise InterlockError(f"未知演练频率: {frequency}", component="drill")
        code = str(code).strip()
        title = str(title).strip()
        if not code or not title:
            raise InterlockError("要求编码与名称不能为空", component="drill")
        if any(req["code"] == code for req in self._requirements.values()):
            raise InterlockError(f"演练要求已登记，禁止重复建档: {code}", component="drill")
        self._requirement_seq += 1
        requirement = {
            "requirement_id": f"REQ-{self._requirement_seq:03d}",
            "code": code,
            "title": title,
            "frequency": frequency,
            "zone": str(zone),
            "basis": str(basis),
            "calendar": CALENDAR_NATURAL,
        }
        self._append({"type": EVENT_REQUIREMENT, "requirement": requirement})
        self._requirements[requirement["requirement_id"]] = requirement
        return dict(requirement)

    def generate_plans(self, start_iso: str, end_iso: str, requirement_id: str | None = None) -> dict:
        """按自然周期为一档或全部要求签发计划项（幂等：同周期已签发则跳过）。"""

        start = parse_date(start_iso)
        end = parse_date(end_iso)
        if end < start:
            raise InterlockError("排期结束日期不能早于开始日期", component="drill")
        requirements = self._select_requirements(requirement_id)
        created: list[dict] = []
        skipped: list[str] = []
        for requirement in requirements:
            frequency = requirement["frequency"]
            existing = {item.period for item in self._items.values() if item.requirement_id == requirement["requirement_id"]}
            for slot in natural_schedule(frequency, start, end):
                if slot["period"] in existing:
                    skipped.append(f"{requirement['code']}:{slot['period']}")
                    continue
                self._plan_seq += 1
                item = DrillItem(
                    item_id=f"PLAN-{self._plan_seq:06d}",
                    requirement_id=requirement["requirement_id"],
                    title=requirement["title"],
                    frequency=frequency,
                    period=slot["period"],
                    period_start=slot["period_start"],
                    period_end=slot["period_end"],
                    due=slot["due"],
                    zone=requirement["zone"],
                    basis=requirement["basis"],
                    plan_ts=self._clock.now(),
                )
                self._append({"type": EVENT_PLAN, "item": item.as_dict()})
                self._items[item.item_id] = item
                created.append(item.as_dict())
        return {"created": created, "skipped": skipped, "calendar": CALENDAR_NATURAL}

    def _select_requirements(self, requirement_id: str | None) -> list[dict]:
        if requirement_id is None:
            return [dict(req) for req in self._requirements.values()]
        if requirement_id not in self._requirements:
            raise InterlockError(f"演练要求不存在: {requirement_id}", component="drill")
        return [dict(self._requirements[requirement_id])]

    def run_scheduled(
        self,
        item_id: str,
        runner: Runner,
        *,
        smoke_ppm: float = 1500.0,
        temperature_c: float = 30.0,
        confirm_dt: float = 5.0,
    ) -> dict:
        """按原始计划执行一次现场联动；真实动作失败时记失败批次，不计完成。"""

        item = self._require_item(item_id)
        scheduled = [run for run in item.runs if run["kind"] == "scheduled"]
        if scheduled:
            raise InterlockError(
                f"周期 {item.period} 的原始计划已执行（{scheduled[0]['run_id']}），"
                "原始计划不得重跑；如有缺口请追加补救演练",
                component="drill",
            )
        report = runner(
            zone=item.zone, smoke_ppm=smoke_ppm, temperature_c=temperature_c, confirm_dt=confirm_dt
        )
        run = self._record_run(item, "scheduled", report, remediated_run=None)
        return {"run": run, "report": report}

    def run_remediation(
        self,
        item_id: str,
        runner: Runner,
        *,
        smoke_ppm: float = 1500.0,
        temperature_c: float = 30.0,
        confirm_dt: float = 5.0,
        note: str = "",
    ) -> dict:
        """对同一周期的缺口追加一次补救演练（不改原计划，新增补救批次）。"""

        item = self._require_item(item_id)
        if not item.runs:
            raise InterlockError(
                "该周期尚无任何执行记录，应先按原始计划执行，不能直接登记补救", component="drill"
            )
        verdict = evaluate_item(item, self._evidence.verify)
        if not verdict["open"]:
            raise InterlockError(
                f"周期 {item.period} 已闭合（{verdict['status']}），无需追加补救", component="drill"
            )
        target = item.runs[-1]["run_id"]
        report = runner(
            zone=item.zone, smoke_ppm=smoke_ppm, temperature_c=temperature_c, confirm_dt=confirm_dt
        )
        run = self._record_run(item, "remediation", report, remediated_run=target, note=note)
        return {"run": run, "report": report, "remediated_run": target}

    def _record_run(
        self, item: DrillItem, kind: str, report: dict[str, Any], *, remediated_run: str | None, note: str = ""
    ) -> dict:
        self._run_seq += 1
        run = {
            "run_id": f"RUN-{self._run_seq:06d}",
            "item_id": item.item_id,
            "period": item.period,
            "kind": kind,
            "date": to_iso(today(self._clock.now())),
            "ts": self._clock.now(),
            "zone": item.zone,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
            "remediated_run": remediated_run,
            "note": note,
            "evidence": [],
        }
        self._append({"type": EVENT_RUN, "item_id": item.item_id, "run": {k: v for k, v in run.items() if k != "evidence"}})
        item.runs.append(run)
        # 成功与否都自动固化联动报告：失败批次的真实动作链同样是检查材料。
        descriptor = self._evidence.attach_report(run["run_id"], report)
        self._append(
            {
                "type": EVENT_EVIDENCE,
                "item_id": item.item_id,
                "run_id": run["run_id"],
                "evidence": descriptor,
            }
        )
        run["evidence"].append(descriptor)
        return dict(run)

    def attach_evidence(
        self, run_id: str, data: bytes, *, filename: str, content_type: str = "application/octet-stream", note: str = ""
    ) -> dict:
        """向已执行批次追加现场证据（照片、签到表等），只追加不替换。"""

        item, run = self._require_run(run_id)
        descriptor = self._evidence.attach_bytes(
            run_id, data, filename=filename, content_type=content_type, note=note
        )
        self._append(
            {
                "type": EVENT_EVIDENCE,
                "item_id": item.item_id,
                "run_id": run_id,
                "evidence": descriptor,
            }
        )
        run["evidence"].append(descriptor)
        return dict(descriptor)

    def _require_item(self, item_id: str) -> DrillItem:
        if item_id not in self._items:
            raise InterlockError(f"演练计划项不存在: {item_id}", component="drill")
        return self._items[item_id]

    def _require_run(self, run_id: str) -> tuple[DrillItem, dict]:
        for item in self._items.values():
            for run in item.runs:
                if run["run_id"] == run_id:
                    return item, run
        raise InterlockError(f"演练执行批次不存在: {run_id}", component="drill")

    def requirements(self) -> list[dict]:
        """返回已登记的周期要求。"""

        return [dict(req) for req in self._requirements.values()]

    def plans(self, requirement_id: str | None = None) -> list[dict]:
        """返回计划项及其执行批次（原始计划与追加记录分开呈现）。"""

        items = self._items.values()
        if requirement_id is not None:
            self._select_requirements(requirement_id)
            items = [item for item in items if item.requirement_id == requirement_id]
        return [item.as_dict() for item in sorted(items, key=lambda entry: (entry.frequency, entry.period))]

    def runs(self) -> list[dict]:
        """返回全部执行批次（含失败批次），按时间升序。"""

        collected = [dict(run) for item in self._items.values() for run in item.runs]
        return sorted(collected, key=lambda run: run["ts"])

    def item_detail(self, item_id: str) -> dict:
        """返回单个计划项的明细与当前评估结论。"""

        item = self._require_item(item_id)
        result = evaluate_item(item, self._evidence.verify)
        return {"item": item.as_dict(), "evaluation": result}

    def evaluate(self, as_of_iso: str | None = None) -> dict:
        """全量合规评估（含已闭合项）。"""

        return build_report(self.requirements(), list(self._items.values()), self._evidence.verify, as_of_iso, include_closed=True)

    def gaps(self, as_of_iso: str | None = None) -> dict:
        """检查前缺口清单：未闭合缺口 + 超期补做闭合项，按到期日升序。"""

        return build_report(self.requirements(), list(self._items.values()), self._evidence.verify, as_of_iso)

    def compare_calendars(self, frequency: str, anchor_iso: str, years: list[int]) -> dict:
        """导出自然周期与滚动口径的跨年覆盖对照（不落台账）。"""

        if frequency not in FREQUENCIES:
            raise InterlockError(f"未知演练频率: {frequency}", component="drill")
        return compare_calendars(frequency, parse_date(anchor_iso), [int(year) for year in years])

    def policy(self) -> dict:
        """返回排期口径选型与合规依据。"""

        return dict(POLICY)

    def current_periods(self, frequency: str, now_ts: float | None = None) -> dict:
        """返回某频率当前所处的自然周期，供巡检核对'现在属于哪个检查单元'。"""

        if frequency not in FREQUENCIES:
            raise InterlockError(f"未知演练频率: {frequency}", component="drill")
        day = today(now_ts if now_ts is not None else self._clock.now())
        months = {"monthly": 1, "quarterly": 3, "half_year": 6, "yearly": 12}[frequency]
        index = (day.month - 1) // months + 1
        label = period_label(frequency, day.year, index)
        start, end = period_window(frequency, day.year, index)
        return {"frequency": frequency, "period": label, "start": to_iso(start), "end": to_iso(end)}
