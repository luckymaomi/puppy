from __future__ import annotations

import hashlib
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol

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
    ) -> None:
        self.session = session
        self.store = store
        self.ai_config = ai_config
        self.generator_factory = generator_factory
        self.random = random.SystemRandom()

    def run(self, task_id: str) -> RunResult:
        with self.store.lock(task_id):
            task = self.store.load(task_id)
            if task.status in TERMINAL_STATUSES:
                return RunResult(task, f"任务已经处于终态: {task.status.value}")
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
                task.set_status(TaskStatus.PAUSED, reason="AI 服务不可用", error=str(exc))
                self.store.save(task)
                return RunResult(task, f"AI 服务不可用，任务已暂停: {exc}")
            except HumanInterventionRequired as exc:
                status = (
                    TaskStatus.WAITING_LOGIN
                    if exc.gate == PageGate.LOGIN
                    else TaskStatus.WAITING_HUMAN
                )
                task.set_status(status, reason=str(exc))
                self.store.save(task)
                return RunResult(task, f"任务等待人工处理: {exc}")
            except InteractionUncertain as exc:
                task.set_status(TaskStatus.WAITING_HUMAN, reason="页面结果不确定", error=str(exc))
                self.store.save(task)
                return RunResult(task, f"页面结果不确定，已停止且不会自动重试: {exc}")
            except PauseRun as exc:
                if task.status == TaskStatus.RUNNING:
                    task.set_status(TaskStatus.PAUSED, reason=str(exc))
                self.store.save(task)
                return RunResult(task, str(exc))
            except Exception as exc:
                task.set_status(TaskStatus.FAILED, reason="执行器失败", error=f"{type(exc).__name__}: {exc}")
                self.store.save(task)
                return RunResult(task, f"任务失败: {type(exc).__name__}: {exc}")

    def _run_with_stop_evidence(
        self, task: TaskState, page: Any, evidence: EvidenceStore
    ) -> RunResult:
        try:
            return self._run_on_page(task, page, evidence)
        except BaseException:
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
            page.search(task.config.keyword)
            self._merge_discovered(task, page.collect_note_links())

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
        evidence.write_summary(task.to_dict())
        return RunResult(task, f"任务完成，已处理 {len(task.processed_note_ids)} 篇笔记")

    def _next_note(self, task: TaskState, page: XhsPage) -> str:
        processed = set(task.processed_note_ids)
        for note_id in task.discovered_note_ids:
            if note_id not in processed:
                return note_id

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
        if context.note_id not in task.commented_note_ids:
            draft = self._comment_draft(task, context, generator)
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
            draft = self._reply_draft(task, context, comment, generator)
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
        self, task: TaskState, context: NoteContext, generator: ContentGenerator
    ) -> str | None:
        pending = task.pending_draft
        if pending and pending.kind == "comment" and pending.note_id == context.note_id:
            draft = pending.text
        else:
            draft = generator.generate_comment(
                GenerationContext(
                    note_text=context.text,
                    comments=[item.text for item in context.comments],
                )
            )
            task.pending_draft = PendingDraft("comment", draft, context.note_id)
            self.store.save(task)
        return self._review(task, draft, "comment", context.text)

    def _reply_draft(
        self,
        task: TaskState,
        context: NoteContext,
        comment: VisibleComment,
        generator: ContentGenerator,
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
        return self._review(task, draft, "reply", comment.text)

    def _review(
        self, task: TaskState, draft: str, kind: str, target_text: str
    ) -> str | None:
        if task.config.send_mode == "auto":
            return draft
        task.set_status(TaskStatus.WAITING_REVIEW, reason="等待终端审核草稿")
        self.store.save(task)
        if not sys.stdin.isatty():
            raise PauseRun("当前终端不可交互，草稿已保存；请在交互终端使用 resume")

        label = "笔记评论" if kind == "comment" else "评论回复"
        print(f"\n[{label}] 上下文: {target_text[:240]}")
        print(f"草稿: {draft}")
        while True:
            choice = input("发送 [Enter/y]、编辑 [e]、跳过 [s]、暂停 [p]: ").strip().lower()
            if choice in {"", "y", "yes"}:
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
                task.set_status(TaskStatus.PAUSED, reason="用户在审核时暂停")
                self.store.save(task)
                raise PauseRun("任务已在草稿审核阶段暂停")
            print("请输入 y、e、s 或 p")

    def _before_write(self, task: TaskState) -> None:
        self._honor_external_control(task)
        if self.store.daily_write_count() >= task.config.daily_write_limit:
            raise PauseRun(f"已达到每日写入硬上限 {task.config.daily_write_limit}")

    def _honor_external_control(self, task: TaskState) -> None:
        current = self.store.load(task.id)
        if current.status == TaskStatus.CANCELLED:
            task.set_status(TaskStatus.CANCELLED, reason="用户取消")
            raise PauseRun("任务已取消")
        if current.status == TaskStatus.PAUSED and task.status != TaskStatus.PAUSED:
            task.set_status(TaskStatus.PAUSED, reason="用户暂停")
            raise PauseRun("任务已暂停")

    def _delay(self, task: TaskState) -> None:
        time.sleep(self.random.uniform(task.config.min_delay, task.config.max_delay))

    @staticmethod
    def _comment_key(note_id: str, comment: VisibleComment) -> str:
        value = f"{comment.author}\0{comment.text}".encode("utf-8")
        return f"{note_id}:{hashlib.sha256(value).hexdigest()[:20]}"
