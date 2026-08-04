from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .ai import AIProvider
from .automation import TaskRunner
from .browser import BrowserSession
from .config import AIConfig
from .observations import ObservationStore
from .paths import AppPaths
from .probe import inspect_page, perform_action, save_snapshot
from .tasks import TaskConfig, TaskStatus, TaskStore, TERMINAL_STATUSES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Puppy 匿名漫游器：只读取小红书与哔哩哔哩公开页面并保存本地观察"
    )
    subparsers = parser.add_subparsers(dest="command")

    health = subparsers.add_parser("health", help="检查可选 AI 配置与 Provider 连通性")
    health.add_argument("--generate", action="store_true", help="额外执行一次真实的最小生成请求")
    subparsers.add_parser("models", help="列出当前 API Key 可见模型")
    subparsers.add_parser("status", help="读取匿名浏览器状态")

    browser_start = subparsers.add_parser("browser-start", help="启动非持久匿名浏览器")
    browser_start.add_argument("--platform", required=True, choices=("xiaohongshu", "bilibili"))
    subparsers.add_parser("browser-stop", help="关闭匿名浏览器并删除本次会话资料")

    snapshot = subparsers.add_parser("snapshot", help="保存当前视口和脱敏页面证据")
    snapshot.add_argument("--stage", required=True)
    snapshot.add_argument("--full-page", action="store_true", help="仅用于不保留滚动位置的基线")

    inspect = subparsers.add_parser("inspect", help="读取当前页面结构并分配本轮临时 ID")
    inspect.add_argument("kind", choices=("interactive", "scroll", "links", "detail", "frames"))

    act = subparsers.add_parser("act", help="对刚观察到的临时元素执行一个探针动作")
    act.add_argument(
        "action",
        choices=("click", "click-point", "fill", "press", "scroll", "hover", "back", "wait"),
    )
    act.add_argument("--id", dest="probe_id")
    act.add_argument("--value")
    act.add_argument("--delta", type=int, default=700)
    act.add_argument("--x", type=float)
    act.add_argument("--y", type=float)

    run = subparsers.add_parser("run", help="创建并运行匿名只读漫游任务")
    run.add_argument("--platform", required=True, choices=("xiaohongshu", "bilibili"))
    run.add_argument("--keyword", default="")
    run.add_argument("--resource-type", choices=("note", "video", "article"))
    run.add_argument("--stop-mode", choices=("count", "duration", "continuous"), default="count")
    run.add_argument("--max-items", type=int, default=20)
    run.add_argument("--duration-minutes", type=int)
    run.add_argument("--comments-limit", type=int, default=20)
    run.add_argument("--min-delay", type=float, default=2.0)
    run.add_argument("--max-delay", type=float, default=5.0)

    resume = subparsers.add_parser("resume", help="继续暂停的匿名漫游任务")
    resume.add_argument("task_id")
    tasks = subparsers.add_parser("tasks", help="列出本地漫游任务")
    tasks.add_argument("--limit", type=int, default=20)
    observations = subparsers.add_parser("observations", help="列出最近本地观察")
    observations.add_argument("--limit", type=int, default=20)
    task = subparsers.add_parser("task", help="读取单个漫游任务")
    task.add_argument("task_id")
    pause = subparsers.add_parser("pause", help="请求暂停漫游")
    pause.add_argument("task_id")
    stop = subparsers.add_parser("stop", help="停止漫游，停止后不可继续")
    stop.add_argument("task_id")
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
        paths.ensure()
        session = BrowserSession(paths)
        store = TaskStore(paths.tasks_dir)
        observations = ObservationStore(paths.observations_dir)
        if args.command == "health":
            print_json(AIProvider(AIConfig.from_env_file()).health(generate=args.generate))
        elif args.command == "models":
            config = AIConfig.from_env_file()
            model_ids = AIProvider(config).list_model_ids()
            print_json({"config": config.public_dict(), "count": len(model_ids), "models": model_ids})
        elif args.command == "status":
            print_json(session.status())
        elif args.command == "browser-start":
            print_json(session.start(args.platform))
        elif args.command == "browser-stop":
            print_json(session.close())
        elif args.command == "snapshot":
            print_json(
                session.with_page(
                    lambda page, evidence: save_snapshot(page, evidence, args.stage, args.full_page)
                )
            )
        elif args.command == "inspect":
            print_json(session.with_page(lambda page, evidence: inspect_page(page, evidence, args.kind)))
        elif args.command == "act":
            print_json(
                session.with_page(
                    lambda page, evidence: perform_action(
                        page, evidence, args.action, args.probe_id, args.value, args.delta, args.x, args.y
                    )
                )
            )
        elif args.command == "run":
            browser_status = session.status()
            if not browser_status.get("running"):
                raise RuntimeError("匿名浏览器未启动")
            if browser_status.get("platform") != args.platform:
                raise RuntimeError("匿名浏览器平台与任务平台不一致")
            resource_type = args.resource_type or ("note" if args.platform == "xiaohongshu" else "video")
            config = TaskConfig(
                platform=args.platform,
                keyword=args.keyword,
                resource_type=resource_type,
                stop_mode=args.stop_mode,
                max_items=args.max_items,
                duration_minutes=args.duration_minutes,
                comments_limit=args.comments_limit,
                min_delay=args.min_delay,
                max_delay=args.max_delay,
            )
            task = store.create(config)
            result = TaskRunner(session, store, observations).run(task.id)
            print(result.message)
            print_json(result.task.to_dict())
            return 0 if result.task.status != TaskStatus.FAILED else 1
        elif args.command == "resume":
            task = store.load(args.task_id)
            if task.status in TERMINAL_STATUSES:
                raise ValueError(f"终态任务 {task.status.value} 不能继续")
            result = TaskRunner(session, store, observations).run(task.id)
            print(result.message)
            print_json(result.task.to_dict())
            return 0 if result.task.status != TaskStatus.FAILED else 1
        elif args.command == "tasks":
            if not 1 <= args.limit <= 100:
                raise ValueError("--limit 必须在 1 到 100 之间")
            print_json([task.to_dict() for task in store.list()[: args.limit]])
        elif args.command == "observations":
            print_json(observations.list(limit=args.limit))
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
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
