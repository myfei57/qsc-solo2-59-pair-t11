"""消防演练合规台账：周期排期、执行留痕、证据留存与检查前缺口清单。"""

from __future__ import annotations

from .compliance import compare_schedules, evaluate, gap_report, rolling_warnings
from .ledger import ChainBrokenError, HashLedger, digest_payload
from .periods import (
    SUPPORTED_CADENCES,
    NaturalWindow,
    add_months,
    iter_natural_windows,
    parse_iso_date,
    rolling_due_dates,
    window_for_date,
)
from .register import (
    ATTEMPT_FAILED,
    ATTEMPT_SUCCESS,
    PLAN_INITIAL,
    PLAN_REMEDY,
    DrillRegister,
    DrillValidationError,
    hash_file,
)
from .service import build_register

__all__ = [
    "ATTEMPT_FAILED",
    "ATTEMPT_SUCCESS",
    "PLAN_INITIAL",
    "PLAN_REMEDY",
    "SUPPORTED_CADENCES",
    "ChainBrokenError",
    "DrillRegister",
    "DrillValidationError",
    "HashLedger",
    "NaturalWindow",
    "add_months",
    "build_register",
    "compare_schedules",
    "digest_payload",
    "evaluate",
    "gap_report",
    "hash_file",
    "iter_natural_windows",
    "parse_iso_date",
    "rolling_due_dates",
    "rolling_warnings",
    "window_for_date",
]
