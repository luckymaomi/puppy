from __future__ import annotations

import json
import msvcrt
import re
import secrets
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator


TASK_ID_PATTERN = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
PLATFORMS = {"xiaohongshu", "bilibili"}
RESOURCE_TYPES = {"xiaohongshu": {"note"}, "bilibili": {"video", "article"}}
STOP_MODES = {"count", "duration", "continuous"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


TERMINAL_STATUSES = {TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.STOPPED}


@dataclass(slots=True)
class TaskConfig:
    platform: str
    keyword: str = ""
    resource_type: str = "note"
    stop_mode: str = "count"
    max_items: int | None = 20
    duration_minutes: int | None = None
    comments_limit: int = 20
    min_delay: float = 2.0
    max_delay: float = 5.0

    def validate(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError("平台只能是 xiaohongshu 或 bilibili")
        if not isinstance(self.keyword, str):
            raise ValueError("关键词必须是字符串")
        self.keyword = self.keyword.strip()
        if len(self.keyword) > 80:
            raise ValueError("关键词最多 80 个字符")
        if self.platform == "bilibili" and not self.keyword:
            raise ValueError("哔哩哔哩匿名漫游需要搜索关键词")
        allowed_resources = RESOURCE_TYPES[self.platform]
        if self.resource_type not in allowed_resources:
            raise ValueError(f"{self.platform} 不支持资源类型 {self.resource_type}")
        if self.stop_mode not in STOP_MODES:
            raise ValueError("停止方式只能是 count、duration 或 continuous")
        if self.stop_mode == "count":
            if not isinstance(self.max_items, int) or not 1 <= self.max_items <= 1000:
                raise ValueError("指定数量必须在 1 到 1000 之间")
        else:
            self.max_items = None
        if self.stop_mode == "duration":
            if not isinstance(self.duration_minutes, int) or not 1 <= self.duration_minutes <= 1440:
                raise ValueError("指定时长必须在 1 到 1440 分钟之间")
        else:
            self.duration_minutes = None
        if not isinstance(self.comments_limit, int) or not 0 <= self.comments_limit <= 100:
            raise ValueError("每条内容保存的可见评论数必须在 0 到 100 之间")
        if not 0.5 <= self.min_delay <= self.max_delay <= 120:
            raise ValueError("浏览间隔必须满足 0.5 <= 最小秒数 <= 最大秒数 <= 120")


@dataclass(slots=True)
class TaskState:
    id: str
    config: TaskConfig
    status: TaskStatus = TaskStatus.CREATED
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    stop_reason: str | None = None
    last_error: str | None = None
    current_resource_id: str | None = None
    discovered_resource_ids: list[str] = field(default_factory=list)
    processed_resource_ids: list[str] = field(default_factory=list)
    observation_count: int = 0
    visible_comment_count: int = 0
    page_count: int = 0
    elapsed_seconds: float = 0.0
    last_observation_path: str | None = None

    def set_status(
        self,
        status: TaskStatus,
        *,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        if self.status in TERMINAL_STATUSES and status != self.status:
            raise ValueError(f"终态任务 {self.status} 不能切换到 {status}")
        self.status = status
        self.stop_reason = reason
        self.last_error = error
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskState":
        config = TaskConfig(**value["config"])
        config.validate()
        return cls(
            id=value["id"],
            config=config,
            status=TaskStatus(value.get("status", TaskStatus.CREATED)),
            created_at=value.get("created_at", now_iso()),
            updated_at=value.get("updated_at", now_iso()),
            stop_reason=value.get("stop_reason"),
            last_error=value.get("last_error"),
            current_resource_id=value.get("current_resource_id"),
            discovered_resource_ids=list(value.get("discovered_resource_ids", [])),
            processed_resource_ids=list(value.get("processed_resource_ids", [])),
            observation_count=int(value.get("observation_count", 0)),
            visible_comment_count=int(value.get("visible_comment_count", 0)),
            page_count=int(value.get("page_count", 0)),
            elapsed_seconds=float(value.get("elapsed_seconds", 0.0)),
            last_observation_path=value.get("last_observation_path"),
        )


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, config: TaskConfig) -> TaskState:
        config.validate()
        existing = self.find_unfinished()
        if existing is not None:
            raise ValueError(f"已有未结束的漫游任务 {existing.id}")
        task_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        task = TaskState(id=task_id, config=config)
        self.save(task)
        return task

    def save(self, task: TaskState) -> None:
        task.updated_at = now_iso()
        path = self._path(task.id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def load(self, task_id: str) -> TaskState:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"任务不存在: {task_id}")
        return TaskState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[TaskState]:
        tasks = [
            TaskState.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in self.root.glob("*.json")
        ]
        return sorted(tasks, key=lambda task: task.created_at, reverse=True)

    def find_unfinished(self) -> TaskState | None:
        return next(
            (task for task in self.list() if task.status not in TERMINAL_STATUSES),
            None,
        )

    @contextmanager
    def lock(self, task_id: str) -> Iterator[None]:
        lock_path = self.root / f".{task_id}.lock"
        with lock_path.open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"任务 {task_id} 已在另一个线程中运行") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _path(self, task_id: str) -> Path:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError("任务 ID 格式无效")
        return self.root / f"{task_id}.json"
