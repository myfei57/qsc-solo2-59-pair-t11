"""消防演练合规台账。

台账把五类事实区分清楚，全部通过哈希链追加，不提供任何改写入口：

``requirement``  周期要求（适用单位、档级、法定依据）；
``plan``         按自然周期排出的原始计划；同一要求同一窗口只有一条原始计划，
                 补救计划以独立条目追加，原计划永不被改；
``attempt``      一次现场演练尝试：现场联动动作全部成功才可能合格，动作失败的
                 尝试保留为失败记录，绝不计为完成；
``evidence``     证据文件登记（只登记文件名与内容 SHA-256，证据原件单独归档）；
``remediation``  补救关系：同周期补做挂到失败计划的窗口上覆盖缺口；跨窗口补做
                 只记整改，不回溯覆盖。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..ids import IdFactory
from ..store.jsonfile import JsonStore
from .ledger import HashLedger
from .periods import NaturalWindow, parse_iso_date, validate_cadence
from .regulations import (
    DEFAULT_REQUIRED_ACTIONS,
    DEFAULT_REQUIRED_EVIDENCE,
    DEFAULT_TIERS,
)

EVENT_REQUIREMENT = "requirement"
EVENT_PLAN = "plan"
EVENT_ATTEMPT = "attempt"
EVENT_EVIDENCE = "evidence"
EVENT_REMEDIATION = "remediation"

PLAN_INITIAL = "initial"
PLAN_REMEDY = "remedy"

ATTEMPT_SUCCESS = "success"
ATTEMPT_FAILED = "failed"


class DrillValidationError(ValueError):
    """台账写入参数不合法。"""


@dataclass(frozen=True)
class DrillRequirement:
    """一条演练周期要求。"""

    req_id: str
    unit: str
    scope: str
    cadence_months: int
    basis: str
    start_period: str
    required_actions: tuple[str, ...]
    required_evidence: tuple[str, ...]
    active: bool

    def as_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "unit": self.unit,
            "scope": self.scope,
            "cadence_months": self.cadence_months,
            "basis": self.basis,
            "start_period": self.start_period,
            "required_actions": list(self.required_actions),
            "required_evidence": list(self.required_evidence),
            "active": self.active,
        }


def hash_file(path: str) -> str:
    """计算证据文件内容的 SHA-256。"""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DrillRegister:
    """消防演练合规台账（只追加）。"""

    def __init__(self, store: JsonStore, ledger: HashLedger) -> None:
        self._store = store
        self._ledger = ledger
        self._plan_ids = IdFactory("plan")
        self._attempt_ids = IdFactory("att")
        self._req_ids = IdFactory("req")
        self._records: list[dict] = list(ledger.records())
        self._requirements: dict[str, DrillRequirement] = {}
        self._plans: list[dict] = []
        self._attempts: list[dict] = []
        self._evidence: list[dict] = []
        self._remediations: list[dict] = []
        for record in self._records:
            self._index(record)

    def initialize(self) -> dict:
        """幂等建立台账并写入四档内置周期要求。"""

        created = 0
        for tier in DEFAULT_TIERS:
            _, outcome = self.add_requirement(
                unit=tier["code"],
                scope=tier["name"],
                cadence_months=tier["cadence_months"],
                basis=tier["basis"],
                start_period=f"{date.today().year - 1}-01",
                note=tier["note"],
            )
            if not outcome["existed"]:
                created += 1
        self._store.write("drill_ledger_meta", {"initialized": True})
        return {"tier_requirements_added": created, "meta": "drill_ledger_meta"}

    def add_requirement(
        self,
        *,
        unit: str,
        scope: str,
        cadence_months: int,
        basis: str,
        start_period: str,
        note: str = "",
        actions: tuple[str, ...] | None = None,
        evidence: tuple[str, ...] | None = None,
    ) -> tuple[str, dict]:
        """登记一条周期要求；相同适用单位已存在则幂等返回。"""

        cadence = validate_cadence(cadence_months)
        for existing in self._requirements.values():
            if existing.unit == unit and existing.active:
                return existing.req_id, {"existed": True}
        req_id = self._req_ids.next()
        requirement = DrillRequirement(
            req_id=req_id,
            unit=unit,
            scope=scope,
            cadence_months=cadence,
            basis=basis,
            start_period=start_period,
            required_actions=tuple(actions or DEFAULT_REQUIRED_ACTIONS),
            required_evidence=tuple(evidence or DEFAULT_REQUIRED_EVIDENCE),
            active=True,
        )
        record = self._append(
            EVENT_REQUIREMENT,
            {**requirement.as_dict(), "note": note},
        )
        return req_id, {"existed": False, "record": record}

    def ensure_plan(self, requirement: DrillRequirement, window: NaturalWindow) -> dict:
        """确保某要求某窗口有且只有一条原始计划（幂等）。"""

        existing = self.find_initial_plan(requirement.req_id, window.key)
        if existing is not None:
            return existing
        planned_on = window.end.isoformat()
        record = self._append(
            EVENT_PLAN,
            {
                "plan_id": self._plan_ids.next(),
                "req_id": requirement.req_id,
                "window_key": window.key,
                "window_label": window.label,
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
                "planned_on": planned_on,
                "kind": PLAN_INITIAL,
                "source_plan_id": "",
                "cadence_months": requirement.cadence_months,
            },
        )
        return record

    def find_initial_plan(self, req_id: str, window_key: str) -> dict | None:
        """取某要求某窗口的原始计划。"""

        for plan in self._plans:
            if (
                plan["req_id"] == req_id
                and plan["window_key"] == window_key
                and plan["kind"] == PLAN_INITIAL
            ):
                return plan
        return None

    def record_attempt(
        self,
        *,
        plan_id: str,
        executed_on: str,
        action_results: dict[str, bool],
        commander: str,
        note: str = "",
    ) -> dict:
        """记录一次现场演练尝试。

        ``action_results`` 缺项的必选动作按失败处理；任一必选动作失败则整体为
        失败，尝试照样留痕但不计完成。
        """

        plan = self._plan(plan_id)
        requirement = self.requirement(plan["req_id"])
        day = parse_iso_date(executed_on)
        results = {code: bool(action_results.get(code, False)) for code in requirement.required_actions}
        passed = all(results.values())
        status = ATTEMPT_SUCCESS if passed else ATTEMPT_FAILED
        failed_actions = [code for code, ok in results.items() if not ok]
        attempt_id = self._attempt_ids.next()
        record = self._append(
            EVENT_ATTEMPT,
            {
                "attempt_id": attempt_id,
                "plan_id": plan_id,
                "req_id": requirement.req_id,
                "executed_on": day.isoformat(),
                "action_results": results,
                "failed_actions": failed_actions,
                "commander": commander,
                "note": note,
                "status": status,
            },
        )
        return record

    def attach_evidence(
        self,
        *,
        attempt_id: str,
        kind: str,
        file_name: str,
        sha256: str,
    ) -> dict:
        """把一份证据挂到某次尝试上，登记类型、文件名与内容哈希。"""

        attempt = self.attempt(attempt_id)
        requirement = self.requirement(attempt["req_id"])
        if kind not in requirement.required_evidence:
            raise DrillValidationError(f"{kind} 不在该要求的必需证据清单内")
        for item in self._evidence:
            if item["attempt_id"] == attempt_id and item["kind"] == kind:
                raise DrillValidationError("该类型证据已登记，证据不可覆盖")
        record = self._append(
            EVENT_EVIDENCE,
            {
                "evidence_id": f"evd-{attempt_id[4:]}-{len(self._evidence) + 1:02d}",
                "attempt_id": attempt_id,
                "req_id": attempt["req_id"],
                "kind": kind,
                "file_name": file_name,
                "sha256": sha256,
            },
        )
        return record

    def open_remediation(self, *, source_plan_id: str, planned_on: str, reason: str) -> dict:
        """为失败/缺口计划追加一条同窗口补救计划，不动原始计划。"""

        source = self._plan(source_plan_id)
        if source["kind"] == PLAN_REMEDY:
            raise DrillValidationError("补救计划上不再派生补救，请对原计划补做")
        window = NaturalWindow(
            key=source["window_key"],
            label=source["window_label"],
            start=parse_iso_date(source["window_start"]),
            end=parse_iso_date(source["window_end"]),
        )
        day = parse_iso_date(planned_on)
        within = window.contains(day)
        record = self._append(
            EVENT_PLAN,
            {
                "plan_id": self._plan_ids.next(),
                "req_id": source["req_id"],
                "window_key": source["window_key"],
                "window_label": source["window_label"],
                "window_start": source["window_start"],
                "window_end": source["window_end"],
                "planned_on": day.isoformat(),
                "kind": PLAN_REMEDY,
                "source_plan_id": source_plan_id,
                "cadence_months": source["cadence_months"],
            },
        )
        self._append(
            EVENT_REMEDIATION,
            {
                "source_plan_id": source_plan_id,
                "remedy_plan_id": record["plan_id"],
                "reason": reason,
                "within_window": within,
            },
        )
        return record

    def requirement(self, req_id: str) -> DrillRequirement:
        """按标识取周期要求。"""

        if req_id not in self._requirements:
            raise DrillValidationError(f"未知的周期要求: {req_id}")
        return self._requirements[req_id]

    def requirements(self) -> list[DrillRequirement]:
        """返回全部生效中的周期要求。"""

        return [item for item in self._requirements.values() if item.active]

    def plans(self, req_id: str | None = None) -> list[dict]:
        """返回计划（可按要求过滤）。"""

        return [dict(plan) for plan in self._plans if req_id is None or plan["req_id"] == req_id]

    def attempts(self) -> list[dict]:
        """返回全部演练尝试（含失败记录）。"""

        return [dict(item) for item in self._attempts]

    def attempt(self, attempt_id: str) -> dict:
        """按标识取尝试。"""

        for item in self._attempts:
            if item["attempt_id"] == attempt_id:
                return dict(item)
        raise DrillValidationError(f"未知的演练尝试: {attempt_id}")

    def evidence_for(self, attempt_id: str) -> list[dict]:
        """取某次尝试已登记的证据。"""

        return [dict(item) for item in self._evidence if item["attempt_id"] == attempt_id]

    def remediations(self) -> list[dict]:
        """返回全部补救关系。"""

        return [dict(item) for item in self._remediations]

    def attempt_qualified(self, attempt: dict) -> bool:
        """尝试是否合规合格：现场动作全部成功且必需证据齐全。"""

        if attempt["status"] != ATTEMPT_SUCCESS:
            return False
        requirement = self.requirement(attempt["req_id"])
        present = {item["kind"] for item in self._evidence if item["attempt_id"] == attempt["attempt_id"]}
        return all(kind in present for kind in requirement.required_evidence)

    def verify_chain(self) -> dict:
        """校验哈希链完整性。"""

        return self._ledger.verify()

    def _plan(self, plan_id: str) -> dict:
        for plan in self._plans:
            if plan["plan_id"] == plan_id:
                return plan
        raise DrillValidationError(f"未知的演练计划: {plan_id}")

    def _append(self, event_type: str, payload: dict[str, Any]) -> dict:
        record = self._ledger.append(event_type, payload)
        self._index(record)
        return dict(record)

    def _index(self, record: dict) -> None:
        event_type = record["event_type"]
        if event_type == EVENT_REQUIREMENT:
            self._requirements[record["req_id"]] = DrillRequirement(
                req_id=record["req_id"],
                unit=record["unit"],
                scope=record["scope"],
                cadence_months=record["cadence_months"],
                basis=record["basis"],
                start_period=record["start_period"],
                required_actions=tuple(record["required_actions"]),
                required_evidence=tuple(record["required_evidence"]),
                active=bool(record["active"]),
            )
        elif event_type == EVENT_PLAN:
            self._plans.append(dict(record))
        elif event_type == EVENT_ATTEMPT:
            self._attempts.append(dict(record))
        elif event_type == EVENT_EVIDENCE:
            self._evidence.append(dict(record))
        elif event_type == EVENT_REMEDIATION:
            self._remediations.append(dict(record))
