"""``batterystore drills`` 子命令：演练台账的录入、排期与检查入口。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from ..errors import StoreError
from .compliance import compare_schedules, evaluate, gap_report
from .periods import parse_iso_date
from .register import DrillValidationError, hash_file
from .reports import (
    render_attempts,
    render_compare,
    render_gap_report,
    render_plans,
    render_requirements,
)
from .regulations import ACTION_LABELS, EVIDENCE_LABELS
from .service import build_register


def _as_text(text: str) -> None:
    print(text)


def _as_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _emit(args: argparse.Namespace, text: str, payload: object) -> None:
    (_as_json(payload) if getattr(args, "json", False) else _as_text(text))


def _root(args: argparse.Namespace) -> str:
    return getattr(args, "drill_root", "") or "var/state"


def _today() -> str:
    return date.today().isoformat()


def _resolve_requirement(register, unit_or_id: str):
    for requirement in register.requirements():
        if requirement.req_id == unit_or_id or requirement.unit == unit_or_id:
            return requirement
    raise DrillValidationError(f"未知的要求标识或适用单位: {unit_or_id}")


def _cmd_init(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    result = register.initialize()
    _emit(
        args,
        f"台账已就绪，内置周期要求 {result['tier_requirements_added']} 条（已存在则跳过）",
        result,
    )
    return 0


def _cmd_requirements(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    requirements = register.requirements()
    _emit(args, render_requirements(requirements), [item.as_dict() for item in requirements])
    return 0


def _cmd_add_requirement(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    req_id, result = register.add_requirement(
        unit=args.unit,
        scope=args.scope,
        cadence_months=args.cadence,
        basis=args.basis,
        start_period=args.start_period,
        note=args.note,
    )
    if result["existed"]:
        _emit(args, f"适用单位 {args.unit} 的要求已存在：{req_id}", {"req_id": req_id, "existed": True})
        return 0
    _emit(args, f"已登记周期要求 {req_id}（{args.scope}，{args.cadence} 个月）", {"req_id": req_id, "record": result["record"]})
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    as_of = parse_iso_date(args.as_of or _today())
    rows = evaluate(register, as_of)
    plans = register.plans(args.requirement and _resolve_requirement(register, args.requirement).req_id)
    if args.window:
        plans = [plan for plan in plans if plan["window_key"] == args.window]
    plans.sort(key=lambda plan: (plan["req_id"], plan["window_start"], plan["kind"]))
    _emit(args, render_plans(plans), plans)
    return 0


def _cmd_execute(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    results = {code: False for code in ACTION_LABELS}
    for token in args.actions or []:
        if "=" not in token:
            raise DrillValidationError(f"动作结果必须写成 动作=ok|fail：{token}")
        code, value = token.split("=", 1)
        if code not in ACTION_LABELS:
            raise DrillValidationError(f"未知的现场动作: {code}，可选 {sorted(ACTION_LABELS)}")
        results[code] = value in ("ok", "true", "1", "yes")
    record = register.record_attempt(
        plan_id=args.plan,
        executed_on=args.date,
        action_results=results,
        commander=args.commander,
        note=args.note,
    )
    qualified = register.attempt_qualified(record)
    payload = {"attempt": record, "qualified": qualified}
    if record["status"] == "failed":
        text = f"尝试 {record['attempt_id']} 已按失败留痕：{record['failed_actions']}，不计为完成"
    elif not qualified:
        text = f"尝试 {record['attempt_id']} 动作成功但证据未齐，暂不计覆盖"
    else:
        text = f"尝试 {record['attempt_id']} 合格，已覆盖对应窗口（计划 {record['plan_id']}）"
    _emit(args, text, payload)
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    if args.kind not in EVIDENCE_LABELS:
        raise DrillValidationError(f"未知证据类型: {args.kind}，可选 {sorted(EVIDENCE_LABELS)}")
    sha256 = args.sha256
    if not sha256:
        try:
            sha256 = hash_file(args.file)
        except OSError as error:
            raise DrillValidationError(f"读取证据文件失败: {args.file}（{error.strerror or error}）") from error
    record = register.attach_evidence(
        attempt_id=args.attempt,
        kind=args.kind,
        file_name=args.file,
        sha256=sha256,
    )
    attempt = register.attempt(args.attempt)
    _emit(
        args,
        f"证据已挂到 {args.attempt}：{EVIDENCE_LABELS[args.kind]} sha256={sha256[:12]}…"
        f"（尝试现状态：{'合格' if register.attempt_qualified(attempt) else '未合格'}）",
        record,
    )
    return 0


def _cmd_remediate(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    record = register.open_remediation(
        source_plan_id=args.plan,
        planned_on=args.date,
        reason=args.reason,
    )
    within = record["window_start"] <= args.date <= record["window_end"]
    if within:
        text = f"同窗口补救计划 {record['plan_id']} 已追加，原计划 {args.plan} 不变，执行合格即覆盖缺口"
    else:
        text = f"补救计划 {record['plan_id']} 已追加，但计划日已超出窗口，只能记整改、不能回溯覆盖"
    _emit(args, text, record)
    return 0


def _cmd_attempts(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    attempts = register.attempts()
    _emit(args, render_attempts(register, attempts), attempts)
    return 0


def _cmd_gaps(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    as_of = parse_iso_date(args.as_of or _today())
    report = gap_report(register, as_of, due_soon_days=args.due_soon)
    _emit(args, render_gap_report(report), report)
    return 1 if report["counts"]["gaps"] else 0


def _cmd_compare(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    anchor = parse_iso_date(args.anchor or _today())
    comparison = compare_schedules(register, anchor, count=args.count)
    _emit(args, render_compare(comparison), comparison)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    register = build_register(_root(args))
    result = register.verify_chain()
    _emit(args, f"哈希链完整：共 {result['records']} 条，尾哈希 {result['tail_hash'][:16]}…", result)
    return 0


def register_drill_parser(sub: argparse._SubParsersAction) -> None:
    """把 ``drills`` 子命令注册进总解析器。"""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--drill-root", default=None, help="演练台账存储根目录（默认 var/state）")
    common.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    drills = sub.add_parser("drills", help="消防演练合规台账", parents=[common])
    drill_sub = drills.add_subparsers(dest="drill_command", required=True)

    init = drill_sub.add_parser("init", help="建立台账并写入内置周期档", parents=[common])
    init.set_defaults(func=_cmd_init)

    requirements = drill_sub.add_parser("requirements", help="列出周期要求", parents=[common])
    requirements.set_defaults(func=_cmd_requirements)

    add_req = drill_sub.add_parser("add-requirement", help="登记一条周期要求", parents=[common])
    add_req.add_argument("--unit", required=True, help="适用单位标识，如 station1")
    add_req.add_argument("--scope", required=True, help="档级名称")
    add_req.add_argument("--cadence", required=True, type=int, choices=(1, 2, 3, 4, 6, 12), help="周期月数")
    add_req.add_argument("--basis", required=True, help="合规依据")
    add_req.add_argument("--start-period", required=True, help="起始周期 YYYY-MM")
    add_req.add_argument("--note", default="", help="备注")
    add_req.set_defaults(func=_cmd_add_requirement)

    plan = drill_sub.add_parser("plan", help="按自然周期排计划并列出", parents=[common])
    plan.add_argument("--requirement", help="只看某要求（ID 或适用单位）")
    plan.add_argument("--window", help="只看某窗口键 YYYY-Qn/YYYY-Hn/YYYY")
    plan.add_argument("--as-of", help="基准日 YYYY-MM-DD，默认今天（决定排到哪）")
    plan.set_defaults(func=_cmd_plan)

    execute = drill_sub.add_parser("execute", help="记录一次现场演练尝试", parents=[common])
    execute.add_argument("--plan", required=True, help="计划ID")
    execute.add_argument("--date", required=True, help="实际执行日 YYYY-MM-DD")
    execute.add_argument(
        "--actions",
        nargs="*",
        default=[],
        metavar="动作=ok|fail",
        help=f"现场联动动作结果，可选: {', '.join(ACTION_LABELS)}；缺省视为失败",
    )
    execute.add_argument("--commander", required=True, help="现场指挥人")
    execute.add_argument("--note", default="", help="失败现象/备注")
    execute.set_defaults(func=_cmd_execute)

    evidence = drill_sub.add_parser("evidence", help="登记演练证据（文件名+哈希）", parents=[common])
    evidence.add_argument("--attempt", required=True, help="尝试ID")
    evidence.add_argument("--kind", required=True, help=f"证据类型: {', '.join(EVIDENCE_LABELS)}")
    evidence.add_argument("--file", required=True, help="证据文件路径（算 SHA-256 并登记文件名）")
    evidence.add_argument("--sha256", help="直接给哈希（不读文件时用）")
    evidence.set_defaults(func=_cmd_evidence)

    remediate = drill_sub.add_parser("remediate", help="为失败/缺口计划追加补救计划", parents=[common])
    remediate.add_argument("--plan", required=True, help="原计划ID（不会被修改）")
    remediate.add_argument("--date", required=True, help="补救计划日 YYYY-MM-DD")
    remediate.add_argument("--reason", required=True, help="补救原因")
    remediate.set_defaults(func=_cmd_remediate)

    attempts = drill_sub.add_parser("attempts", help="列出全部演练尝试（含失败）", parents=[common])
    attempts.set_defaults(func=_cmd_attempts)

    gaps = drill_sub.add_parser("gaps", help="检查前缺口清单（自然周期口径）", parents=[common])
    gaps.add_argument("--as-of", help="基准日 YYYY-MM-DD，默认今天")
    gaps.add_argument("--due-soon", type=int, default=14, help="进行中窗口临近阈值天数")
    gaps.set_defaults(func=_cmd_gaps)

    compare = drill_sub.add_parser("compare", help="对比自然周期与滚动间隔排法", parents=[common])
    compare.add_argument("--anchor", help="锚点日（上次合格演练日）YYYY-MM-DD")
    compare.add_argument("--count", type=int, default=4, help="向后排几次")
    compare.set_defaults(func=_cmd_compare)

    verify = drill_sub.add_parser("verify", help="校验台账哈希链", parents=[common])
    verify.set_defaults(func=_cmd_verify)


def run_drill_command(args: argparse.Namespace) -> int:
    """命令异常的统一出口：领域/存储错误走退出码 2。"""

    try:
        return int(args.func(args))
    except (DrillValidationError, StoreError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
