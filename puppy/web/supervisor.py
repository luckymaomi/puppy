from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..ai import validate_draft
from ..automation import TaskRunner
from ..browser import BrowserSession
from ..config import AIConfig, ConfigurationError
from ..tasks import TaskConfig, TaskState, TaskStatus, TaskStore, TERMINAL_STATUSES


class EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=200)
        self._sequence = 0

    def subscribe(self, after_sequence: int = 0) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            for event in self._history:
                if event["sequence"] > after_sequence:
                    subscriber.put_nowait(dict(event))
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event_type: str, **data: Any) -> None:
        payload = dict(data)
        event_time = payload.pop(
            "time", datetime.now().astimezone().isoformat(timespec="seconds")
        )
        payload.pop("type", None)
        payload.pop("sequence", None)
        with self._lock:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "time": event_time,
                "type": event_type,
                **payload,
            }
            self._history.append(event)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._history]


class TaskSupervisor:
    def __init__(
        self,
        session: BrowserSession,
        store: TaskStore,
        events: EventBroker,
        runner_factory: Callable[..., TaskRunner] = TaskRunner,
        config_loader: Callable[[], AIConfig] = AIConfig.from_env_file,
    ) -> None:
        self.session = session
        self.store = store
        self.events = events
        self.runner_factory = runner_factory
        self.config_loader = config_loader
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._active_task_id: str | None = None
        self._requests: dict[str, tuple[TaskStatus, str]] = {}

    def create_and_start(self, config: TaskConfig) -> TaskState:
        task = self.store.create(config)
        self.start(task.id)
        return self.store.load(task.id)

    def start(self, task_id: str) -> TaskState:
        task = self.store.load(task_id)
        if task.status in TERMINAL_STATUSES:
            raise ValueError(f"终态任务 {task.status.value} 不能继续")
        if task.write_in_flight is not None:
            raise ValueError("任务存在未决写入，请先裁决结果")
        browser = self.session.status()
        if not browser.get("running"):
            raise RuntimeError("任务绑定的浏览器未启动")
        if browser.get("profile_id") != task.config.profile_id:
            raise RuntimeError("当前浏览器资料与任务绑定资料不一致")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(f"任务 {self._active_task_id} 正在运行")
            self._requests.pop(task_id, None)
            self._active_task_id = task_id
            self._thread = threading.Thread(
                target=self._run,
                args=(task_id,),
                name=f"xhs-task-{task_id}",
                daemon=True,
            )
            self._thread.start()
        return self.store.load(task_id)

    def pause(
        self, task_id: str, *, reason: str = "用户从控制台请求暂停"
    ) -> TaskState:
        task = self._editable_task(task_id)
        self._set_request(task_id, TaskStatus.PAUSED, reason)
        task.set_status(TaskStatus.PAUSED, reason=reason)
        self.store.save(task)
        self._publish_task("task_updated", task)
        return task

    def stop(self, task_id: str) -> TaskState:
        task = self._editable_task(task_id)
        reason = "用户从控制台停止任务"
        self._set_request(task_id, TaskStatus.STOPPED, reason)
        task.set_status(TaskStatus.STOPPED, reason=reason)
        self.store.save(task)
        self._publish_task("task_updated", task)
        return task

    def resolve(self, task_id: str, *, sent: bool) -> TaskState:
        task = self.store.load(task_id)
        task.resolve_write(sent=sent)
        task.set_status(TaskStatus.PAUSED, reason="未决写入已人工裁决")
        self.store.save(task)
        self.events.publish(
            "write_resolved",
            task_id=task.id,
            result="sent" if sent else "not-sent",
        )
        self._publish_task("task_updated", task)
        return task

    def wait_for_page(
        self, task_id: str, *, status: TaskStatus, reason: str
    ) -> TaskState:
        if status not in {TaskStatus.WAITING_LOGIN, TaskStatus.WAITING_HUMAN}:
            raise ValueError("页面等待状态无效")
        task = self._editable_task(task_id)
        task.set_status(status, reason=reason)
        self.store.save(task)
        self._publish_task("task_updated", task)
        return task

    def approve(self, task_id: str, *, action: str, text: str | None = None) -> TaskState:
        task = self.store.load(task_id)
        draft = task.pending_draft
        if task.status != TaskStatus.WAITING_APPROVAL or draft is None:
            raise ValueError("任务当前没有等待批准的草稿")
        if action not in {"send", "edit", "skip", "pause"}:
            raise ValueError("未知批准动作")
        if action == "pause":
            return self.pause(task_id)
        if action == "edit":
            limit = 120 if draft.kind == "comment" else 80
            draft.text = validate_draft(text or "", limit)
        draft.approval_status = "skipped" if action == "skip" else "approved"
        task.set_status(TaskStatus.PAUSED, reason="草稿批准已处理")
        self.store.save(task)
        self.events.publish(
            "draft_reviewed",
            task_id=task.id,
            action=action,
            kind=draft.kind,
        )
        self._publish_task("task_updated", task)
        self.start(task_id)
        return self.store.load(task_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            task_id = self._active_task_id if running else None
        return {"running": running, "task_id": task_id}

    def shutdown(self) -> None:
        snapshot = self.snapshot()
        if snapshot["running"] and snapshot["task_id"]:
            try:
                task_id = str(snapshot["task_id"])
                task = self._editable_task(task_id)
                reason = "工作台关闭"
                self._set_request(task_id, TaskStatus.PAUSED, reason)
                task.set_status(TaskStatus.PAUSED, reason=reason)
                self.store.save(task)
                self._publish_task("task_updated", task)
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=8)

    def _run(self, task_id: str) -> None:
        self.events.publish("task_started", task_id=task_id)
        try:
            config = self.config_loader()
            runner = self.runner_factory(
                self.session,
                self.store,
                config,
                control_status=self._requested_status,
            )
            runner.run(task_id)
        except ConfigurationError as exc:
            task = self.store.load(task_id)
            if task.status not in TERMINAL_STATUSES and task.status != TaskStatus.PAUSED:
                task.set_status(TaskStatus.PAUSED, reason="AI 配置不可用", error=str(exc))
                self.store.save(task)
        except Exception as exc:
            task = self.store.load(task_id)
            if task.status != TaskStatus.PAUSED and task.status not in TERMINAL_STATUSES:
                task.set_status(
                    TaskStatus.FAILED,
                    reason="控制台任务执行失败",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.store.save(task)
        finally:
            finished = self.store.load(task_id)
            with self._lock:
                self._active_task_id = None
                self._thread = None
            self.events.publish(
                "task_finished",
                task_id=task_id,
                status=finished.status.value,
                reason=finished.stop_reason,
                comment_count=finished.comment_count,
                reply_count=finished.reply_count,
            )

    def _requested_status(self, task_id: str) -> tuple[TaskStatus, str] | None:
        with self._lock:
            return self._requests.get(task_id)

    def _set_request(self, task_id: str, status: TaskStatus, reason: str) -> None:
        with self._lock:
            self._requests[task_id] = (status, reason)

    def _publish_task(self, event_type: str, task: TaskState) -> None:
        self.events.publish(
            event_type,
            task_id=task.id,
            status=task.status.value,
            reason=task.stop_reason,
            comment_count=task.comment_count,
            reply_count=task.reply_count,
        )

    def _editable_task(self, task_id: str) -> TaskState:
        task = self.store.load(task_id)
        if task.status in TERMINAL_STATUSES:
            raise ValueError(f"终态任务 {task.status.value} 不能修改")
        return task
