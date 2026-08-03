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


PROFILE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_LOGIN = "waiting_login"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


TERMINAL_STATUSES = {TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.STOPPED}


@dataclass(slots=True)
class TaskConfig:
    profile_id: str
    keyword: str
    max_notes: int = 10
    replies_min: int = 1
    replies_max: int = 2
    send_mode: str = "approval"
    min_delay: float = 3.0
    max_delay: float = 7.0
    daily_write_limit: int = 30

    def validate(self) -> None:
        if not isinstance(self.profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(
            self.profile_id
        ):
            raise ValueError("任务绑定的浏览器资料 ID 无效")
        if not isinstance(self.keyword, str):
            raise ValueError("关键词必须是字符串")
        self.keyword = self.keyword.strip()
        if not self.keyword or len(self.keyword) > 80:
            raise ValueError("关键词长度必须在 1 到 80 个字符之间")
        if not 1 <= self.max_notes <= 100:
            raise ValueError("每个任务最多处理 1 到 100 篇笔记")
        if not 0 <= self.replies_min <= self.replies_max <= 3:
            raise ValueError("随机回复数必须满足 0 <= 最小值 <= 最大值 <= 3")
        if self.send_mode not in {"approval", "auto"}:
            raise ValueError("发送模式只能是 approval 或 auto")
        if not 1 <= self.min_delay <= self.max_delay <= 120:
            raise ValueError("操作间隔必须满足 1 <= 最小秒数 <= 最大秒数 <= 120")
        if not 1 <= self.daily_write_limit <= 200:
            raise ValueError("每日写入上限必须在 1 到 200 之间")


@dataclass(slots=True)
class PendingDraft:
    kind: str
    text: str
    note_id: str
    comment_id: str | None = None
    target_text: str | None = None
    dedupe_key: str | None = None
    approval_status: str = "pending"


@dataclass(slots=True)
class TaskState:
    id: str
    config: TaskConfig
    status: TaskStatus = TaskStatus.CREATED
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    stop_reason: str | None = None
    last_error: str | None = None
    current_note_id: str | None = None
    processed_note_ids: list[str] = field(default_factory=list)
    discovered_note_ids: list[str] = field(default_factory=list)
    commented_note_ids: list[str] = field(default_factory=list)
    replied_comment_keys: list[str] = field(default_factory=list)
    reply_targets: dict[str, int] = field(default_factory=dict)
    comment_count: int = 0
    reply_count: int = 0
    write_events: list[str] = field(default_factory=list)
    pending_draft: PendingDraft | None = None
    write_in_flight: PendingDraft | None = None

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

    def register_write(self, kind: str) -> None:
        self.write_events.append(now_iso())
        if kind == "comment":
            self.comment_count += 1
        elif kind == "reply":
            self.reply_count += 1
        else:
            raise ValueError(f"未知写入类型: {kind}")
        self.updated_at = now_iso()

    def begin_write(self) -> None:
        if self.pending_draft is None:
            raise ValueError("没有可进入写入阶段的草稿")
        if self.write_in_flight is not None:
            raise ValueError("已有未决写入，必须先确认结果")
        self.write_in_flight = PendingDraft(**asdict(self.pending_draft))
        self.updated_at = now_iso()

    def resolve_write(self, sent: bool) -> None:
        draft = self.write_in_flight
        if draft is None:
            raise ValueError("任务没有未决写入")
        if sent:
            if draft.kind == "comment":
                if draft.note_id not in self.commented_note_ids:
                    self.commented_note_ids.append(draft.note_id)
            elif draft.kind == "reply":
                if not draft.dedupe_key:
                    raise ValueError("未决回复缺少去重键")
                if draft.dedupe_key not in self.replied_comment_keys:
                    self.replied_comment_keys.append(draft.dedupe_key)
            else:
                raise ValueError(f"未知写入类型: {draft.kind}")
            self.register_write(draft.kind)
            self.pending_draft = None
        self.write_in_flight = None
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskState":
        config = TaskConfig(**value["config"])
        config.validate()
        draft_value = value.get("pending_draft")
        in_flight_value = value.get("write_in_flight")
        return cls(
            id=value["id"],
            config=config,
            status=TaskStatus(value.get("status", TaskStatus.CREATED)),
            created_at=value.get("created_at", now_iso()),
            updated_at=value.get("updated_at", now_iso()),
            stop_reason=value.get("stop_reason"),
            last_error=value.get("last_error"),
            current_note_id=value.get("current_note_id"),
            processed_note_ids=list(value.get("processed_note_ids", [])),
            discovered_note_ids=list(value.get("discovered_note_ids", [])),
            commented_note_ids=list(value.get("commented_note_ids", [])),
            replied_comment_keys=list(value.get("replied_comment_keys", [])),
            reply_targets={
                str(key): int(item) for key, item in value.get("reply_targets", {}).items()
            },
            comment_count=int(value.get("comment_count", 0)),
            reply_count=int(value.get("reply_count", 0)),
            write_events=list(value.get("write_events", [])),
            pending_draft=PendingDraft(**draft_value) if draft_value else None,
            write_in_flight=(
                PendingDraft(**in_flight_value) if in_flight_value else None
            ),
        )


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, config: TaskConfig) -> TaskState:
        config.validate()
        existing = self.find_unfinished(config.profile_id)
        if existing is not None:
            raise ValueError(
                f"浏览器资料 {config.profile_id} 已有未结束任务 {existing.id}"
            )
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

    def find_unfinished(self, profile_id: str) -> TaskState | None:
        if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(
            profile_id
        ):
            raise ValueError("浏览器资料 ID 格式无效")
        return next(
            (
                task
                for task in self.list()
                if task.config.profile_id == profile_id
                and task.status not in TERMINAL_STATUSES
            ),
            None,
        )

    def daily_write_count(self, day: str | None = None) -> int:
        expected = day or datetime.now().astimezone().date().isoformat()
        return sum(
            1
            for task in self.list()
            for timestamp in task.write_events
            if timestamp[:10] == expected
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
                raise RuntimeError(f"任务 {task_id} 已在另一个进程中运行") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _path(self, task_id: str) -> Path:
        if not task_id or any(char not in "0123456789-abcdef" for char in task_id):
            raise ValueError("任务 ID 格式无效")
        return self.root / f"{task_id}.json"
