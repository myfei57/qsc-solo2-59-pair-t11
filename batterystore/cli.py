"""命令行入口。

六个子命令覆盖日常使用：

``serve``    启动控制台 HTTP 服务，可选地在启动后探测接口；
``demo``     在本地跑一遍完整流程（启动 → 并网切换 → 温度降额 → 均衡 → 充电 →
             放电 → 消防联动 → 复位 → 并发探测），用于交付前的真实运行检查；
``status``   打印当前状态快照；
``config``   打印生效配置；
``alerts``   打印按最新编号路由后的告警；
``reset``    清掉落盘的平台计数；
``drill``    消防演练合规台账：口径依据、要求登记、自然周期排期、按计划执行、
             失败后补救、证据追加与检查前缺口清单。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .app import Platform, build_platform
from .clock import ManualClock, SystemClock
from .config import config_to_mapping, default_config, load_config
from .console.server import ConsoleServer, probe_endpoints
from .errors import StoreError

DEFAULT_PROBE_PATHS = ("/api/status", "/api/readiness", "/api/topology", "/api/audit", "/api/racks")


def _build(args: argparse.Namespace) -> Platform:
    config = load_config(args.config) if getattr(args, "config", None) else default_config()
    return build_platform(
        config,
        storage_root=getattr(args, "storage_root", None),
        rack_count=getattr(args, "racks", 2),
    )


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _cmd_serve(args: argparse.Namespace) -> int:
    platform = _build(args)
    server = ConsoleServer(platform, host=args.host, port=args.port, web_root=args.web_root)
    base_url = server.start()
    payload: dict = {
        "listening": base_url,
        "routes": server.api.routes(),
        "pages": server.pages.index(),
        "pages_ready": server.pages.exists(),
    }
    if args.probe:
        payload["probe"] = probe_endpoints(base_url, list(DEFAULT_PROBE_PATHS))
    _dump(payload)
    if args.duration > 0:
        try:
            time.sleep(args.duration)
        except KeyboardInterrupt:  # pragma: no cover - 交互路径
            pass
        server.stop()
        return 0
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:  # pragma: no cover - 交互路径
        server.stop()
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    clock = ManualClock() if args.fast else SystemClock()
    config_path = getattr(args, "config", None)
    config = load_config(config_path) if config_path else default_config()
    platform = build_platform(
        config,
        clock=clock,
        storage_root=getattr(args, "storage_root", None),
        rack_count=args.racks,
    )
    # 演示从干净基线起步：先清掉上一轮落盘的计数与运行态。
    platform.reset_state()

    startup = platform.start()
    tie = platform.transfer_grid_tie(power_kw=args.power_kw, frequency_hz=50.0)
    derate = platform.sample_temperature(temperature_c=args.temperature_c, dt=5.0)
    platform.disturb_cell(cell_index=0, delta_v=0.04)
    balancing = platform.balance(max_steps=args.balance_steps)
    charging = platform.charge(steps=args.steps, dt=600.0)
    discharging = platform.discharge(power_kw=args.discharge_kw, dt=600.0)
    cycles = [platform.cycle(dt=5.0) for _ in range(3)]
    drill = platform.fire_drill(zone="Z1", smoke_ppm=1500.0, temperature_c=30.0, confirm_dt=5.0)
    release = platform.release_fire()
    calibration = platform.calibrate_soc(68.0, "demo")
    probe = platform.concurrency_probe(rounds=args.rounds)
    fault_probe = platform.concurrency_fault_probe(rounds=8)
    alarms = platform.alerts()
    shutdown = platform.stop()

    server = ConsoleServer(platform, port=0, web_root=args.web_root)
    base_url = server.start()
    endpoints = probe_endpoints(base_url, list(DEFAULT_PROBE_PATHS))
    pages = server.pages.index()
    server.stop()

    report = {
        "startup": startup,
        "grid_tie": {"ok": tie["ok"], "steps": [step["action"] for step in tie["steps"]]},
        "derate": {"charge_limit_ratio": derate["charge_limit_ratio"]},
        "balance": {"ok": balancing["ok"], "violations": balancing["violations"]},
        "charge": {"ok": charging["ok"], "violations": charging["violations"]},
        "discharge": {"ok": discharging["ok"], "violations": discharging["violations"]},
        "cycles_run": len(cycles),
        "last_cycle": cycles[-1],
        "fire_drill": {"ok": drill["ok"], "steps": [step["action"] for step in drill["steps"]]},
        "fire_release": release,
        "calibration": calibration,
        "concurrency": probe,
        "concurrency_fault": fault_probe,
        "alerts": alarms,
        "shutdown": shutdown,
        "probe": endpoints,
        "pages": pages,
        "audit": platform.audit.summary(),
    }
    _dump(report)

    failures = [name for name, item in endpoints.items() if not item["ok"]]
    failures.extend(name for name, item in (("grid_tie", tie), ("charge", charging), ("discharge", discharging)) if not item["ok"])
    if not drill["ok"]:
        failures.append("fire_drill")
    if not probe["ok"]:
        failures.append("concurrency_probe")
    if not fault_probe["ok"]:
        failures.append("fault_probe")
    if not all(item["present"] for item in pages):
        failures.append("pages_missing")
    if failures:
        print(f"检查未通过: {failures}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    platform = _build(args)
    payload = platform.status()
    payload["topology"] = platform.topology_summary()
    payload["capacity"] = platform.capacity().as_dict()
    payload["quota"] = platform.quota_snapshot()
    payload["trend"] = platform.trend_snapshot()
    _dump(payload)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    config_path = getattr(args, "config", None)
    config = load_config(config_path) if config_path else default_config()
    _dump(config_to_mapping(config))
    return 0


def _cmd_alerts(args: argparse.Namespace) -> int:
    platform = _build(args)
    _dump({"alerts": platform.alerts(), "mapping": platform.rack_mapping()})
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    platform = _build(args)
    _dump(platform.reset_state())
    return 0


def _cmd_drill_policy(args: argparse.Namespace) -> int:
    _dump(_build(args).drill_policy())
    return 0


def _cmd_drill_register_defaults(args: argparse.Namespace) -> int:
    _dump({"requirements": _build(args).drill_register_defaults()})
    return 0


def _cmd_drill_register(args: argparse.Namespace) -> int:
    platform = _build(args)
    _dump(
        platform.drill_register(
            code=args.code, title=args.title, frequency=args.frequency, zone=args.zone, basis=args.basis
        )
    )
    return 0


def _cmd_drill_plan(args: argparse.Namespace) -> int:
    platform = _build(args)
    requirement_id = getattr(args, "requirement_id", None)
    _dump(platform.drill_plan(args.start, args.end, requirement_id))
    return 0


def _cmd_drill_plans(args: argparse.Namespace) -> int:
    requirement_id = getattr(args, "requirement_id", None)
    _dump({"plans": _build(args).drill_plans(requirement_id)})
    return 0


def _cmd_drill_run(args: argparse.Namespace) -> int:
    platform = _build(args)
    outcome = platform.drill_run_scheduled(
        args.item_id,
        smoke_ppm=args.smoke_ppm,
        temperature_c=args.temperature_c,
        confirm_dt=args.confirm_dt,
    )
    _dump(outcome)
    return 0 if outcome["ok"] else 1


def _cmd_drill_remediate(args: argparse.Namespace) -> int:
    platform = _build(args)
    outcome = platform.drill_run_remediation(
        args.item_id,
        smoke_ppm=args.smoke_ppm,
        temperature_c=args.temperature_c,
        confirm_dt=args.confirm_dt,
        note=args.note,
    )
    _dump(outcome)
    return 0 if outcome["ok"] else 1


def _cmd_drill_gaps(args: argparse.Namespace) -> int:
    as_of = getattr(args, "as_of", None)
    _dump(_build(args).drill_gaps(as_of))
    return 0


def _cmd_drill_report(args: argparse.Namespace) -> int:
    as_of = getattr(args, "as_of", None)
    _dump(_build(args).drill_report(as_of))
    return 0


def _cmd_drill_runs(args: argparse.Namespace) -> int:
    _dump({"runs": _build(args).drill_runs()})
    return 0


def _cmd_drill_item(args: argparse.Namespace) -> int:
    _dump(_build(args).drill_item(args.item_id))
    return 0


def _cmd_drill_evidence(args: argparse.Namespace) -> int:
    platform = _build(args)
    with open(args.file, "rb") as handle:
        data = handle.read()
    _dump(
        platform.drill_attach_evidence(
            args.run_id,
            data,
            filename=args.filename or os.path.basename(args.file),
            content_type=args.content_type,
            note=args.note,
        )
    )
    return 0


def _cmd_drill_compare(args: argparse.Namespace) -> int:
    platform = _build(args)
    years = [int(part) for part in args.years.split(",") if part]
    _dump(platform.drill_compare_calendars(args.frequency, args.anchor, years))
    return 0


def _cmd_drill_current(args: argparse.Namespace) -> int:
    _dump(_build(args).drill_current_period(args.frequency))
    return 0



def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="JSON 配置文件路径", default=argparse.SUPPRESS)
    common.add_argument("--storage-root", help="覆盖状态存储目录", default=argparse.SUPPRESS)
    common.add_argument("--racks", type=int, default=2, help="簇数量")
    common.add_argument("--web-root", default="web", help="控制台页面目录")
    parser = argparse.ArgumentParser(
        prog="batterystore", description="电化学储能电站簇级联控服务", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动控制台服务", parents=[common])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--duration", type=float, default=0.0, help="运行多少秒后退出，0 表示常驻")
    serve.add_argument("--probe", action="store_true", help="启动后探测接口")
    serve.set_defaults(func=_cmd_serve)

    demo = sub.add_parser("demo", help="跑一遍完整流程", parents=[common])
    demo.add_argument("--power-kw", type=float, default=120.0, help="并网输出功率")
    demo.add_argument("--discharge-kw", type=float, default=200.0, help="放电功率")
    demo.add_argument("--temperature-c", type=float, default=46.0, help="演示温度采集")
    demo.add_argument("--steps", type=int, default=3, help="充电电流阶梯数")
    demo.add_argument("--balance-steps", type=int, default=400, help="均衡最大步数")
    demo.add_argument("--rounds", type=int, default=24, help="并发探测轮数")
    demo.add_argument("--fast", action="store_true", help="使用可推进时钟，跳过真实等待")
    demo.set_defaults(func=_cmd_demo)

    status = sub.add_parser("status", help="打印状态快照", parents=[common])
    status.set_defaults(func=_cmd_status)

    config = sub.add_parser("config", help="打印生效配置", parents=[common])
    config.set_defaults(func=_cmd_config)

    alerts = sub.add_parser("alerts", help="打印路由后的告警", parents=[common])
    alerts.set_defaults(func=_cmd_alerts)

    reset = sub.add_parser("reset", help="清掉落盘的平台计数", parents=[common])
    reset.set_defaults(func=_cmd_reset)

    drill = sub.add_parser("drill", help="消防演练合规台账", parents=[common])
    drill_sub = drill.add_subparsers(dest="drill_command", required=True)

    drill_policy = drill_sub.add_parser("policy", help="查看排期口径与合规依据", parents=[common])
    drill_policy.set_defaults(func=_cmd_drill_policy)

    drill_defaults = drill_sub.add_parser("register-defaults", help="登记默认两档周期要求", parents=[common])
    drill_defaults.set_defaults(func=_cmd_drill_register_defaults)

    drill_register = drill_sub.add_parser("register", help="登记一档周期要求", parents=[common])
    drill_register.add_argument("--code", required=True)
    drill_register.add_argument("--title", required=True)
    drill_register.add_argument("--frequency", required=True, choices=("monthly", "quarterly", "half_year", "yearly"))
    drill_register.add_argument("--zone", default="Z1")
    drill_register.add_argument("--basis", default="")
    drill_register.set_defaults(func=_cmd_drill_register)

    drill_plan = drill_sub.add_parser("plan", help="按自然周期签发计划", parents=[common])
    drill_plan.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    drill_plan.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    drill_plan.add_argument("--requirement-id", default=argparse.SUPPRESS)
    drill_plan.set_defaults(func=_cmd_drill_plan)

    drill_plans = drill_sub.add_parser("plans", help="列出计划与执行记录", parents=[common])
    drill_plans.add_argument("--requirement-id", default=argparse.SUPPRESS)
    drill_plans.set_defaults(func=_cmd_drill_plans)

    drill_run = drill_sub.add_parser("run", help="按原始计划执行一次联动演练", parents=[common])
    drill_run.add_argument("item_id")
    drill_run.add_argument("--smoke-ppm", type=float, default=1500.0, dest="smoke_ppm")
    drill_run.add_argument("--temperature-c", type=float, default=30.0, dest="temperature_c")
    drill_run.add_argument("--confirm-dt", type=float, default=5.0, dest="confirm_dt")
    drill_run.set_defaults(func=_cmd_drill_run)

    drill_remediate = drill_sub.add_parser("remediate", help="对缺口周期追加补救演练", parents=[common])
    drill_remediate.add_argument("item_id")
    drill_remediate.add_argument("--smoke-ppm", type=float, default=1500.0, dest="smoke_ppm")
    drill_remediate.add_argument("--temperature-c", type=float, default=30.0, dest="temperature_c")
    drill_remediate.add_argument("--confirm-dt", type=float, default=5.0, dest="confirm_dt")
    drill_remediate.add_argument("--note", default="")
    drill_remediate.set_defaults(func=_cmd_drill_remediate)

    drill_gaps = drill_sub.add_parser("gaps", help="检查前缺口清单", parents=[common])
    drill_gaps.add_argument("--as-of", default=argparse.SUPPRESS, help="检查基准日 YYYY-MM-DD")
    drill_gaps.set_defaults(func=_cmd_drill_gaps)

    drill_report = drill_sub.add_parser("report", help="全量合规评估", parents=[common])
    drill_report.add_argument("--as-of", default=argparse.SUPPRESS, help="评估基准日 YYYY-MM-DD")
    drill_report.set_defaults(func=_cmd_drill_report)

    drill_runs = drill_sub.add_parser("runs", help="列出全部执行批次（含失败）", parents=[common])
    drill_runs.set_defaults(func=_cmd_drill_runs)

    drill_item = drill_sub.add_parser("item", help="查看单条计划项明细", parents=[common])
    drill_item.add_argument("item_id")
    drill_item.set_defaults(func=_cmd_drill_item)

    drill_evidence = drill_sub.add_parser("evidence", help="向执行批次追加现场证据文件", parents=[common])
    drill_evidence.add_argument("run_id")
    drill_evidence.add_argument("file")
    drill_evidence.add_argument("--filename")
    drill_evidence.add_argument("--content-type", default="application/octet-stream", dest="content_type")
    drill_evidence.add_argument("--note", default="")
    drill_evidence.set_defaults(func=_cmd_drill_evidence)

    drill_compare = drill_sub.add_parser("compare", help="对照自然周期与滚动口径覆盖月份", parents=[common])
    drill_compare.add_argument("--frequency", default="quarterly", choices=("monthly", "quarterly", "half_year", "yearly"))
    drill_compare.add_argument("--anchor", required=True, help="上次实际执行日 YYYY-MM-DD")
    drill_compare.add_argument("--years", required=True, help="自然年，逗号分隔，如 2025,2026")
    drill_compare.set_defaults(func=_cmd_drill_compare)

    drill_current = drill_sub.add_parser("current", help="查看当前所属自然检查周期", parents=[common])
    drill_current.add_argument("--frequency", default="quarterly", choices=("monthly", "quarterly", "half_year", "yearly"))
    drill_current.set_defaults(func=_cmd_drill_current)
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。"""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except StoreError as error:
        print(json.dumps(error.as_record(), ensure_ascii=False), file=sys.stderr)
        return 2
