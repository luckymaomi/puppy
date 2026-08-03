from __future__ import annotations

import hashlib
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .ai import AIContentGenerator, GenerationContext, GenerationError, validate_draft
from .browser import BrowserSession
from .config import AIConfig
from .evidence import EvidenceStore
from .page import (
    HumanInterventionRequired,
    InteractionUncertain,
    NoteContext,
    PageGate,
    VisibleComment,
    XhsPage,
)
from .tasks import PendingDraft, TaskState, TaskStatus, TaskStore, TERMINAL_STATUSES


class ContentGenerator(Protocol):
    def generate_comment(self, context: GenerationContext) -> str: ...

    def generate_reply(self, context: GenerationContext) -> str: ...


class PauseRun(Exception):
    pass


class SkipItem(Exception):
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
        ai_config: AIConfig | None = None,
        generator_factory: Any = AIContentGenerator,
        control_status: Callable[[str], tuple[TaskStatus, str] | None] | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.ai_config = ai_config
        self.generator_factory = generator_factory
        self.control_status = control_status
        self.random = random.SystemRandom()

    def run(self, task_id: str) -> RunResult:
        with self.store.lock(task_id):
            task = self.store.load(task_id)
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
                return self.session.with_page(
                    lambda page, evidence: self._run_with_stop_evidence(
                        task, page, evidence
                    )
                )
            except KeyboardInterrupt:
                task.set_status(TaskStatus.PAUSED, reason="用户中断")
                self.store.save(task)
                return RunResult(task, "任务已暂停，可使用 resume 继续")
            except GenerationError as exc:
                stopped = self._external_stop_result(task_id)
                if stopped is not None:
                    return stopped
                task.set_status(TaskStatus.PAUSED, reason="AI 服务不可用", error=str(exc))
                self.store.save(task)
                return RunResult(task, f"AI 服务不可用，任务已暂停: {exc}")
            except HumanInterventionRequired as exc:
                stopped = self._external_stop_result(task_id)
                if stopped is not None:
                    return stopped
                status = (
                    TaskStatus.WAITING_LOGIN
                    if exc.gate == PageGate.LOGIN
                    else TaskStatus.WAITING_HUMAN
                )
                task.set_status(status, reason=str(exc))
                self.store.save(task)
                return RunResult(task, f"任务等待人工处理: {exc}")
            except InteractionUncertain as exc:
                stopped = self._external_stop_result(task_id)
                if stopped is not None:
                    return stopped
                if self._browser_is_stopped():
                    task.set_status(
                        TaskStatus.PAUSED,
                        reason="浏览器已关闭；重新启动任务绑定的浏览器后可继续",
                        error=str(exc),
                    )
                    self.store.save(task)
                    return RunResult(task, "浏览器已关闭，任务已暂停")
                task.set_status(TaskStatus.WAITING_HUMAN, reason="页面结果不确定", error=str(exc))
                self.store.save(task)
                return RunResult(task, f"页面结果不确定，已停止且不会自动重试: {exc}")
            except PauseRun as exc:
                if task.status == TaskStatus.RUNNING:
                    task.set_status(TaskStatus.PAUSED, reason=str(exc))
                self.store.save(task)
                return RunResult(task, str(exc))
            except Exception as exc:
                stopped = self._external_stop_result(task_id)
                if stopped is not None:
                    return stopped
                if self._browser_is_stopped():
                    task.set_status(
                        TaskStatus.PAUSED,
                        reason="浏览器已关闭；重新启动任务绑定的浏览器后可继续",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self.store.save(task)
                    return RunResult(task, "浏览器已关闭，任务已暂停")
                task.set_status(TaskStatus.FAILED, reason="执行器失败", error=f"{type(exc).__name__}: {exc}")
                self.store.save(task)
                return RunResult(task, f"任务失败: {type(exc).__name__}: {exc}")

    def _run_with_stop_evidence(
        self, task: TaskState, page: Any, evidence: EvidenceStore
    ) -> RunResult:
        evidence.bind(task_id=task.id)
        evidence.event(
            "task_execution_started",
            keyword=task.config.keyword,
            max_notes=task.config.max_notes,
            send_mode=task.config.send_mode,
        )
        try:
            return self._run_on_page(task, page, evidence)
        except BaseException as exc:
            evidence.event(
                "task_execution_stopped",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            try:
                evidence.save_viewport(f"stopped-{int(time.time() * 1000)}", page)
            except Exception:
                pass
            raise

    def _run_on_page(
        self, task: TaskState, raw_page: Any, evidence: EvidenceStore
    ) -> RunResult:
        page = XhsPage(raw_page, evidence)
        if task.write_in_flight is not None:
            raise InteractionUncertain(
                "上次写入结果尚未裁决，请检查当前页面后运行 resolve"
            )
        gate, reason = page.check_gate()
        if gate == PageGate.LOGIN:
            task.set_status(TaskStatus.WAITING_LOGIN, reason=reason)
            self.store.save(task)
            return RunResult(task, "浏览器等待人工登录；登录后使用 resume 继续")
        if gate == PageGate.HUMAN:
            raise HumanInterventionRequired(reason or "页面要求人工接管")

        if self.ai_config is None:
            raise GenerationError("当前任务没有加载 .env AI 配置")
        generator = self.generator_factory(self.ai_config)
        if not task.discovered_note_ids and not task.current_note_id:
            self._merge_discovered(task, page.search(task.config.keyword))
            self.store.save(task)

        while len(task.processed_note_ids) < task.config.max_notes:
            self._honor_external_control(task)
            page.require_ready()
            note_id = task.current_note_id or self._next_note(task, page)
            task.current_note_id = note_id
            self.store.save(task)

            if note_id not in raw_page.url and raw_page.locator(
                ".note-detail-mask:visible"
            ).count() == 0:
                page.open_note(note_id)
            context = page.read_note_context(note_id)
            try:
                self._process_note(task, page, context, generator)
            except SkipItem:
                evidence.event("note_skipped", note_id=note_id)

            page.close_detail(note_id)
            if note_id not in task.processed_note_ids:
                task.processed_note_ids.append(note_id)
            task.current_note_id = None
            task.pending_draft = None
            self.store.save(task)

        task.set_status(TaskStatus.COMPLETE, reason="达到任务笔记上限")
        self.store.save(task)
        evidence.event(
            "task_completed",
            processed_count=len(task.processed_note_ids),
            comment_count=task.comment_count,
            reply_count=task.reply_count,
        )
        evidence.write_summary(task.to_dict())
        return RunResult(task, f"任务完成，已处理 {len(task.processed_note_ids)} 篇笔记")

    def _next_note(self, task: TaskState, page: XhsPage) -> str:
        processed = set(task.processed_note_ids)
        for note_id in task.discovered_note_ids:
            if note_id not in processed:
                return note_id

        if not task.discovered_note_ids:
            raise InteractionUncertain("搜索结果中没有识别到可处理的笔记，未执行写入")

        stagnant_rounds = 0
        previous_count = len(task.discovered_note_ids)
        for _ in range(30):
            metrics = page.scroll_results()
            self._merge_discovered(task, page.collect_note_links())
            self.store.save(task)
            for note_id in task.discovered_note_ids:
                if note_id not in processed:
                    return note_id
            if len(task.discovered_note_ids) == previous_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
                previous_count = len(task.discovered_note_ids)
            if metrics["at_bottom"] and not metrics["moved"] and stagnant_rounds >= 3:
                task.set_status(TaskStatus.COMPLETE, reason="搜索结果已稳定到底且无新增笔记")
                self.store.save(task)
                raise PauseRun("搜索结果已处理完毕")
        raise PauseRun("搜索结果仍在增量加载，已达到单次 30 轮滚动上限")

    def _merge_discovered(self, task: TaskState, links: list[Any]) -> None:
        known = set(task.discovered_note_ids)
        for link in links:
            if link.note_id not in known:
                task.discovered_note_ids.append(link.note_id)
                known.add(link.note_id)

    def _process_note(
        self,
        task: TaskState,
        page: XhsPage,
        context: NoteContext,
        generator: ContentGenerator,
    ) -> None:
        evidence = getattr(page, "evidence", None)
        if context.note_id not in task.commented_note_ids:
            draft = self._comment_draft(task, context, generator, evidence)
            if draft is None:
                raise SkipItem()
            self._before_write(task)
            task.begin_write()
            self.store.save(task)
            page.submit_comment(draft)
            task.resolve_write(sent=True)
            self.store.save(task)
            self._delay(task)
            context = page.read_note_context(context.note_id)

        target_count = task.reply_targets.get(context.note_id)
        if target_count is None:
            target_count = self.random.randint(
                task.config.replies_min, task.config.replies_max
            )
            task.reply_targets[context.note_id] = target_count
            self.store.save(task)

        replied_here = sum(
            1
            for key in task.replied_comment_keys
            if key.startswith(f"{context.note_id}:")
        )
        remaining = target_count - replied_here
        if remaining <= 0:
            return
        candidates = [
            item
            for item in context.comments
            if self._comment_key(context.note_id, item) not in task.replied_comment_keys
        ]
        if len(candidates) < remaining:
            raise InteractionUncertain(
                f"需要回复 {remaining} 条，但当前只识别到 {len(candidates)} 条可用评论"
            )
        for comment in self.random.sample(candidates, remaining):
            self._honor_external_control(task)
            draft = self._reply_draft(task, context, comment, generator, evidence)
            key = self._comment_key(context.note_id, comment)
            if draft is None:
                task.replied_comment_keys.append(key)
                self.store.save(task)
                continue
            self._before_write(task)
            task.begin_write()
            self.store.save(task)
            page.activate_reply(comment)
            page.submit_reply(draft)
            task.resolve_write(sent=True)
            self.store.save(task)
            self._delay(task)

    def _comment_draft(
        self,
        task: TaskState,
        context: NoteContext,
        generator: ContentGenerator,
        evidence: EvidenceStore | None,
    ) -> str | None:
        pending = task.pending_draft
        if pending and pending.kind == "comment" and pending.note_id == context.note_id:
            draft = pending.text
        else:
            if evidence is not None:
                evidence.event(
                    "generation_requested", kind="comment", note_id=context.note_id
                )
            draft = generator.generate_comment(
                GenerationContext(
                    note_text=context.text,
                    comments=[item.text for item in context.comments],
                )
            )
            task.pending_draft = PendingDraft("comment", draft, context.note_id)
            self.store.save(task)
            if evidence is not None:
                evidence.event(
                    "draft_ready", kind="comment", note_id=context.note_id, text=draft
                )
        return self._approve(task, draft, "comment", context.text)

    def _reply_draft(
        self,
        task: TaskState,
        context: NoteContext,
        comment: VisibleComment,
        generator: ContentGenerator,
        evidence: EvidenceStore | None,
    ) -> str | None:
        pending = task.pending_draft
        if (
            pending
            and pending.kind == "reply"
            and pending.note_id == context.note_id
            and pending.target_text == comment.text
        ):
            draft = pending.text
        else:
            if evidence is not None:
                evidence.event(
                    "generation_requested",
                    kind="reply",
                    note_id=context.note_id,
                    comment_id=comment.comment_id,
                )
            draft = generator.generate_reply(
                GenerationContext(
                    note_text=context.text,
                    comments=[item.text for item in context.comments],
                    target_comment=comment.text,
                )
            )
            task.pending_draft = PendingDraft(
                "reply",
                draft,
                context.note_id,
                comment.comment_id,
                comment.text,
                self._comment_key(context.note_id, comment),
            )
            self.store.save(task)
            if evidence is not None:
                evidence.event(
                    "draft_ready",
                    kind="reply",
                    note_id=context.note_id,
                    comment_id=comment.comment_id,
                    text=draft,
                )
        return self._approve(task, draft, "reply", comment.text)

    def _approve(
        self, task: TaskState, draft: str, kind: str, target_text: str
    ) -> str | None:
        if task.config.send_mode == "auto":
            return draft
        if task.pending_draft and task.pending_draft.approval_status == "approved":
            task.set_status(TaskStatus.RUNNING)
            self.store.save(task)
            return draft
        if task.pending_draft and task.pending_draft.approval_status == "skipped":
            task.pending_draft = None
            task.set_status(TaskStatus.RUNNING)
            self.store.save(task)
            return None
        task.set_status(TaskStatus.WAITING_APPROVAL, reason="等待批准草稿")
        self.store.save(task)
        if not sys.stdin.isatty():
            raise PauseRun("当前终端不可交互，草稿已保存；请在控制台批准")

        label = "笔记评论" if kind == "comment" else "评论回复"
        print(f"\n[{label}] 上下文: {target_text[:240]}")
        print(f"草稿: {draft}")
        while True:
            choice = input("发送 [Enter/y]、编辑 [e]、跳过 [s]、暂停 [p]: ").strip().lower()
            if choice in {"", "y", "yes"}:
                if task.pending_draft:
                    task.pending_draft.approval_status = "approved"
                task.set_status(TaskStatus.RUNNING)
                self.store.save(task)
                return draft
            if choice == "e":
                limit = 120 if kind == "comment" else 80
                draft = validate_draft(input("新内容: "), limit)
                if task.pending_draft:
                    task.pending_draft.text = draft
                self.store.save(task)
                print(f"更新后的草稿: {draft}")
                continue
            if choice == "s":
                task.pending_draft = None
                task.set_status(TaskStatus.RUNNING)
                self.store.save(task)
                return None
            if choice == "p":
                task.set_status(TaskStatus.PAUSED, reason="用户在批准时暂停")
                self.store.save(task)
                raise PauseRun("任务已在草稿批准阶段暂停")
            print("请输入 y、e、s 或 p")

    def _before_write(self, task: TaskState) -> None:
        self._honor_external_control(task)
        if self.store.daily_write_count() >= task.config.daily_write_limit:
            raise PauseRun(f"已达到每日写入硬上限 {task.config.daily_write_limit}")

    def _honor_external_control(self, task: TaskState) -> None:
        requested = self.control_status(task.id) if self.control_status else None
        if requested is not None:
            status, reason = requested
            task.set_status(status, reason=reason)
            raise PauseRun(reason)
        current = self.store.load(task.id)
        if current.status == TaskStatus.STOPPED:
            task.set_status(
                TaskStatus.STOPPED, reason=current.stop_reason or "用户停止任务"
            )
            raise PauseRun("任务已停止")
        if current.status == TaskStatus.PAUSED and task.status != TaskStatus.PAUSED:
            reason = current.stop_reason or "用户暂停"
            task.set_status(TaskStatus.PAUSED, reason=reason)
            raise PauseRun(reason)

    def _delay(self, task: TaskState) -> None:
        time.sleep(self.random.uniform(task.config.min_delay, task.config.max_delay))

    def _external_stop_result(self, task_id: str) -> RunResult | None:
        requested = self.control_status(task_id) if self.control_status else None
        current = self.store.load(task_id)
        if requested is not None and current.status not in TERMINAL_STATUSES:
            status, reason = requested
            current.set_status(status, reason=reason)
            self.store.save(current)
            return RunResult(current, reason)
        if current.status == TaskStatus.PAUSED:
            return RunResult(current, current.stop_reason or "任务已暂停")
        if current.status == TaskStatus.STOPPED:
            return RunResult(current, current.stop_reason or "任务已停止")
        return None

    def _session_issue(self, task: TaskState) -> str | None:
        read_status = getattr(self.session, "status", None)
        if not callable(read_status):
            return None
        status = read_status()
        if not status.get("running"):
            return "任务绑定的浏览器未启动"
        if status.get("profile_id") != task.config.profile_id:
            return "当前浏览器资料与任务绑定资料不一致"
        return None

    def _browser_is_stopped(self) -> bool:
        read_status = getattr(self.session, "status", None)
        if not callable(read_status):
            return False
        try:
            return not bool(read_status().get("running"))
        except Exception:
            return False

    @staticmethod
    def _comment_key(note_id: str, comment: VisibleComment) -> str:
        value = f"{comment.author}\0{comment.text}".encode("utf-8")
        return f"{note_id}:{hashlib.sha256(value).hexdigest()[:20]}"
