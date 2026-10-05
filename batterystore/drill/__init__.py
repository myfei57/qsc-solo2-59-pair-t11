"""消防演练合规管理：自然周期排期、执行留痕、证据柜与检查前缺口清单。"""

from __future__ import annotations

from .evidence import EvidenceVault
from .period import (
    FREQUENCIES,
    compare_calendars,
    iter_period_keys,
    natural_schedule,
    parse_date,
    period_window,
    rolling_due_after,
)
from .program import CALENDAR_NATURAL, POLICY, DrillProgram
from .snapshot import (
    STATUS_COVERED,
    STATUS_COVERED_LATE,
    STATUS_EVIDENCE_GAP,
    STATUS_FAILED,
    STATUS_MISSING,
    DrillItem,
    build_report,
    evaluate_item,
)

__all__ = [
    "CALENDAR_NATURAL",
    "FREQUENCIES",
    "POLICY",
    "DrillItem",
    "DrillProgram",
    "EvidenceVault",
    "STATUS_COVERED",
    "STATUS_COVERED_LATE",
    "STATUS_EVIDENCE_GAP",
    "STATUS_FAILED",
    "STATUS_MISSING",
    "build_report",
    "compare_calendars",
    "evaluate_item",
    "iter_period_keys",
    "natural_schedule",
    "parse_date",
    "period_window",
    "rolling_due_after",
]
