from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .ai import AIProvider
from .automation import TaskRunner
from .browser import BrowserSession
from .config import AIConfig
from .paths import AppPaths
from .probe import inspect_page, perform_action, save_snapshot
from .tasks import TaskConfig, TaskStatus, TaskStore, TERMINAL_STATUSES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="小红书可观察机器人：本地控制台、批准或自动发送、异常立即停机"
    )
    subparsers = parser.add_subparsers(dest="command")

    health = subparsers.add_parser("health", help="检查 .env、Provider、模型与 API 连通性")
    health.add_argument(
        "--generate", action="store_true", help="额外执行一次真实的最小生成请求"
    )

    subparsers.add_parser("models", help="列出当前 API Key 实际可见的全部模型")

    subparsers.add_parser("status", help="读取当前浏览器状态")

    snapshot = subparsers.add_parser("snapshot", help="保存当前视口和脱敏页面证据")
    snapshot.add_argument("--stage", required=True)
    snapshot.add_argument("--full-page", action="store_true", help="仅用于不保留滚动位置的基线")

    inspect = subparsers.add_parser("inspect", help="读取当前页面结构并分配本轮临时 ID")
    inspect.add_argument(
        "kind", choices=("interactive", "scroll", "links", "detail", "frames")
    )

    act = subparsers.add_parser("act", help="对刚观察到的临时元素执行一个动作")
    act.add_argument(
        "action",
        choices=("click", "click-point", "fill", "press", "scroll", "hover", "back", "wait"),
    )
    act.add_argument("--id", dest="probe_id")
    act.add_argument("--value")
    act.add_argument("--delta", type=int, default=700)
    act.add_argument("--x", type=float)
    act.add_argument("--y", type=float)

    run = subparsers.add_parser(
        "run", help="创建并运行关键词任务；默认每条草稿都需批准"
    )
    run.add_argument("--keyword", required=True)
    run.add_argument("--max-notes", type=int, default=10)
    run.add_argument("--replies-min", type=int, default=1)
    run.add_argument("--replies-max", type=int, default=2)
    run.add_argument("--send-mode", choices=("approval", "auto"), default="approval")
    run.add_argument("--min-delay", type=float, default=3.0)
    run.add_argument("--max-delay", type=float, default=7.0)
    run.add_argument("--daily-write-limit", type=int, default=30)

    resume = subparsers.add_parser("resume", help="人工处理完成后继续现有任务")
    resume.add_argument("task_id")

    tasks = subparsers.add_parser("tasks", help="列出本地任务状态")
    tasks.add_argument("--limit", type=int, default=20)

    task = subparsers.add_parser("task", help="读取单个任务")
    task.add_argument("task_id")

    pause = subparsers.add_parser("pause", help="请求暂停任务")
    pause.add_argument("task_id")

    stop = subparsers.add_parser("stop", help="停止任务，停止后不可继续")
    stop.add_argument("task_id")

    resolve = subparsers.add_parser("resolve", help="人工裁决上一次结果不确定的写入")
    resolve.add_argument("task_id")
    resolve.add_argument("--result", required=True, choices=("sent", "not-sent"))
    return parser


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        if args.command is None:
            from .web.server import run_console

            return run_console()
        paths = AppPaths()
        session = BrowserSession(paths)
        store = TaskStore(paths.tasks_dir)
        if args.command == "health":
            config = AIConfig.from_env_file()
            print_json(AIProvider(config).health(generate=args.generate))
        elif args.command == "models":
            config = AIConfig.from_env_file()
            model_ids = AIProvider(config).list_model_ids()
            print_json(
                {
                    "config": config.public_dict(),
                    "count": len(model_ids),
                    "models": model_ids,
                }
            )
        elif args.command == "status":
            print_json(session.status())
        elif args.command == "snapshot":
            print_json(
                session.with_page(
                    lambda page, evidence: save_snapshot(
                        page, evidence, args.stage, args.full_page
                    )
                )
            )
        elif args.command == "inspect":
            print_json(
                session.with_page(
                    lambda page, evidence: inspect_page(page, evidence, args.kind)
                )
            )
        elif args.command == "act":
            print_json(
                session.with_page(
                    lambda page, evidence: perform_action(
                        page,
                        evidence,
                        args.action,
                        args.probe_id,
                        args.value,
                        args.delta,
                        args.x,
                        args.y,
                    )
                )
            )
        elif args.command == "run":
            ai_config = AIConfig.from_env_file()
            browser_status = session.status()
            if not browser_status["running"] or not browser_status.get("profile_id"):
                raise RuntimeError("没有正在运行的浏览器资料，请先从工作台启动浏览器")
            profile_id = str(browser_status["profile_id"])
            existing = store.find_unfinished(profile_id)
            if existing is not None:
                raise RuntimeError(
                    f"当前浏览器资料已有未结束任务 {existing.id}；请继续或停止该任务"
                )
            config = TaskConfig(
                profile_id=profile_id,
                keyword=args.keyword,
                max_notes=args.max_notes,
                replies_min=args.replies_min,
                replies_max=args.replies_max,
                send_mode=args.send_mode,
                min_delay=args.min_delay,
                max_delay=args.max_delay,
                daily_write_limit=args.daily_write_limit,
            )
            task = store.create(config)
            result = TaskRunner(session, store, ai_config).run(task.id)
            print(result.message)
            print_json(result.task.to_dict())
            return 0 if result.task.status != TaskStatus.FAILED else 1
        elif args.command == "resume":
            task = store.load(args.task_id)
            if task.status in TERMINAL_STATUSES:
                raise ValueError(f"终态任务 {task.status.value} 不能继续")
            if task.write_in_flight is not None:
                raise ValueError("任务存在未决写入，请先裁决结果")
            browser_status = session.status()
            if not browser_status["running"]:
                raise RuntimeError("浏览器未运行")
            if browser_status.get("profile_id") != task.config.profile_id:
                raise RuntimeError("当前浏览器资料与任务绑定资料不一致")
            ai_config = AIConfig.from_env_file()
            result = TaskRunner(session, store, ai_config).run(args.task_id)
            print(result.message)
            print_json(result.task.to_dict())
            return 0 if result.task.status != TaskStatus.FAILED else 1
        elif args.command == "tasks":
            if not 1 <= args.limit <= 100:
                raise ValueError("--limit 必须在 1 到 100 之间")
            print_json([task.to_dict() for task in store.list()[: args.limit]])
        elif args.command == "task":
            print_json(store.load(args.task_id).to_dict())
        elif args.command in {"pause", "stop"}:
            task = store.load(args.task_id)
            if task.status in TERMINAL_STATUSES:
                raise ValueError(f"终态任务 {task.status.value} 不能再修改")
            status = TaskStatus.PAUSED if args.command == "pause" else TaskStatus.STOPPED
            reason = "用户请求暂停" if args.command == "pause" else "用户停止任务"
            task.set_status(status, reason=reason)
            store.save(task)
            print_json(task.to_dict())
        elif args.command == "resolve":
            task = store.load(args.task_id)
            task.resolve_write(sent=args.result == "sent")
            task.set_status(TaskStatus.PAUSED, reason=f"未决写入已裁决为 {args.result}")
            store.save(task)
            print_json(task.to_dict())
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
