from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from .browser import BrowserSession
from .evidence import EvidenceStore
from .observations import ObservationStore
from .platforms import HumanInterventionRequired, PageInteractionError, ResourceLink, create_adapter
from .tasks import TaskState, TaskStatus, TaskStore, TERMINAL_STATUSES


class PauseRun(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RunResult:
    task: TaskState
    message: str


class TaskRunner:
    def __init__(
        self,
        session: BrowserSession,
        store: TaskStore,
        observations: ObservationStore,
        control_status: Callable[[str], tuple[TaskStatus, str] | None] | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.observations = observations
        self.control_status = control_status
        self.random = random.SystemRandom()
        self._run_started_at: float | None = None
        self._elapsed_base = 0.0

    def run(self, task_id: str) -> RunResult:
        started = time.monotonic()
        self._run_started_at = started
        message = "任务已停止"
        with self.store.lock(task_id):
            task = self.store.load(task_id)
            self._elapsed_base = task.elapsed_seconds
            if task.status in TERMINAL_STATUSES:
                return RunResult(task, f"任务已经处于终态: {task.status.value}")
            session_issue = self._session_issue(task)
            if session_issue is not None:
                task.set_status(TaskStatus.PAUSED, reason=session_issue)
                self.store.save(task)
                return RunResult(task, session_issue)
            task.set_status(TaskStatus.RUNNING)
            self.store.save(task)
            try:
                result = self.session.with_page(
                    lambda page, evidence: self._run_on_page(task, page, evidence)
                )
                message = result.message
            except KeyboardInterrupt:
                task.set_status(TaskStatus.PAUSED, reason="用户中断")
                self.store.save(task)
                message = "任务已暂停"
            except HumanInterventionRequired as exc:
                task.set_status(TaskStatus.WAITING_HUMAN, reason=str(exc))
                self.store.save(task)
                message = f"等待人工处理: {exc}"
            except PageInteractionError as exc:
                task.set_status(TaskStatus.PAUSED, reason="页面结构或结果需要复核", error=str(exc))
                self.store.save(task)
                message = f"页面需要复核，任务已暂停: {exc}"
            except PauseRun as exc:
                latest = self.store.load(task.id)
                if latest.status == TaskStatus.RUNNING:
                    latest.set_status(TaskStatus.PAUSED, reason=str(exc))
                    self.store.save(latest)
                message = str(exc)
            except Exception as exc:
                latest = self.store.load(task.id)
                requested = self._requested(task.id)
                if requested is not None:
                    latest.set_status(requested[0], reason=requested[1])
                elif not self.session.status().get("running"):
                    latest.set_status(
                        TaskStatus.PAUSED,
                        reason="匿名浏览器已关闭；重新启动同一平台浏览器后可继续",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    latest.set_status(
                        TaskStatus.FAILED,
                        reason="匿名漫游执行器失败",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                self.store.save(latest)
                message = latest.stop_reason or "任务失败"
            finally:
                latest = self.store.load(task.id)
                latest.elapsed_seconds = self._elapsed_base + max(0.0, time.monotonic() - started)
                self.store.save(latest)
        return RunResult(self.store.load(task_id), message)

    def _run_on_page(self, task: TaskState, page: Any, evidence: EvidenceStore) -> RunResult:
        evidence.bind(task_id=task.id, platform=task.config.platform)
        evidence.event(
            "task_execution_started",
            keyword=task.config.keyword,
            resource_type=task.config.resource_type,
            stop_mode=task.config.stop_mode,
            max_items=task.config.max_items,
            duration_minutes=task.config.duration_minutes,
        )
        adapter = create_adapter(
            task.config.platform,
            page,
            evidence,
            comments_limit=task.config.comments_limit,
        )
        adapter.prepare(task.config.keyword, task.config.resource_type)
        no_progress_rounds = 0
        page_count = task.page_count

        while True:
            self._honor_external_control(task)
            limit_reason = self._limit_reason(task)
            if limit_reason is not None:
                return self._complete(task, evidence, limit_reason)

            links = adapter.discover()
            self._merge_discovered(task, links)
            next_link = self._next_visible(task, links)
            if next_link is not None:
                task.current_resource_id = next_link.resource_id
                self.store.save(task)
                adapter.open(next_link)
                observation = adapter.observe(next_link)
                path = self.observations.save(observation)
                adapter.close(next_link)
                if next_link.resource_id not in task.processed_resource_ids:
                    task.processed_resource_ids.append(next_link.resource_id)
                task.current_resource_id = None
                task.observation_count += 1
                task.visible_comment_count += len(observation.comments)
                task.last_observation_path = str(path.resolve())
                task.page_count = page_count
                self._sync_elapsed(task)
                self.store.save(task)
                evidence.event(
                    "observation_saved",
                    resource_id=next_link.resource_id,
                    resource_type=next_link.resource_type,
                    observation_path=str(path.resolve()),
                    processed_count=len(task.processed_resource_ids),
                )
                no_progress_rounds = 0
                self._delay(task)
                continue

            before_discovered = len(task.discovered_resource_ids)
            advanced = adapter.advance()
            if advanced.page_changed:
                page_count += 1
                task.page_count = page_count
                self.store.save(task)
            if advanced.source_exhausted:
                return self._complete(task, evidence, "当前公开来源已浏览完")
            refreshed = adapter.discover()
            self._merge_discovered(task, refreshed)
            gained = len(task.discovered_resource_ids) > before_discovered
            no_progress_rounds = 0 if gained or advanced.moved else no_progress_rounds + 1
            if no_progress_rounds >= 6:
                return self._complete(task, evidence, "连续 6 轮没有新增公开内容")

    def _complete(self, task: TaskState, evidence: EvidenceStore, reason: str) -> RunResult:
        task.set_status(TaskStatus.COMPLETE, reason=reason)
        self.store.save(task)
        evidence.event(
            "task_completed",
            processed_count=len(task.processed_resource_ids),
            observation_count=task.observation_count,
            visible_comment_count=task.visible_comment_count,
            reason=reason,
        )
        evidence.write_summary(task.to_dict())
        return RunResult(task, f"漫游完成，已保存 {task.observation_count} 条观察")

    def _limit_reason(self, task: TaskState) -> str | None:
        if task.config.stop_mode == "count" and task.config.max_items is not None:
            if len(task.processed_resource_ids) >= task.config.max_items:
                return "达到指定内容数量"
        if task.config.stop_mode == "duration" and task.config.duration_minutes is not None:
            current_run = (
                time.monotonic() - self._run_started_at
                if self._run_started_at is not None
                else 0.0
            )
            if self._elapsed_base + current_run >= task.config.duration_minutes * 60:
                return "达到指定漫游时长"
        return None

    def _merge_discovered(self, task: TaskState, links: list[ResourceLink]) -> None:
        known = set(task.discovered_resource_ids)
        for link in links:
            if link.resource_id not in known:
                task.discovered_resource_ids.append(link.resource_id)
                known.add(link.resource_id)
        self.store.save(task)

    @staticmethod
    def _next_visible(task: TaskState, links: list[ResourceLink]) -> ResourceLink | None:
        processed = set(task.processed_resource_ids)
        return next((link for link in links if link.resource_id not in processed), None)

    def _honor_external_control(self, task: TaskState) -> None:
        requested = self._requested(task.id)
        if requested is None:
            return
        status, reason = requested
        task.set_status(status, reason=reason)
        self.store.save(task)
        raise PauseRun(reason)

    def _requested(self, task_id: str) -> tuple[TaskStatus, str] | None:
        return self.control_status(task_id) if self.control_status is not None else None

    def _delay(self, task: TaskState) -> None:
        remaining = self.random.uniform(task.config.min_delay, task.config.max_delay)
        while remaining > 0:
            self._honor_external_control(task)
            interval = min(0.25, remaining)
            time.sleep(interval)
            remaining -= interval

    def _session_issue(self, task: TaskState) -> str | None:
        status = self.session.status()
        if not status.get("running"):
            return "匿名浏览器未启动"
        if status.get("platform") != task.config.platform:
            return "当前匿名浏览器平台与任务平台不一致"
        return None

    def _sync_elapsed(self, task: TaskState) -> None:
        if self._run_started_at is not None:
            task.elapsed_seconds = self._elapsed_base + max(
                0.0, time.monotonic() - self._run_started_at
            )
