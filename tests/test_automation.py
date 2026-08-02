from xhs_robot.ai import GenerationError
from xhs_robot.automation import TaskRunner
from xhs_robot.page import NoteContext, VisibleComment
from xhs_robot.tasks import TaskConfig, TaskStatus, TaskStore


class FakeGenerator:
    def generate_comment(self, context):
        return "和笔记内容相关的评论"

    def generate_reply(self, context):
        return f"针对“{context.target_comment}”的回复"


class FakePage:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.replies: list[str] = []
        self.active_comment = None

    def submit_comment(self, text: str) -> None:
        self.comments.append(text)

    def activate_reply(self, comment: VisibleComment) -> None:
        self.active_comment = comment

    def submit_reply(self, text: str) -> None:
        assert self.active_comment is not None
        self.replies.append(text)

    def read_note_context(self, note_id: str) -> NoteContext:
        assert self.context.note_id == note_id
        return self.context


def test_resume_does_not_duplicate_verified_note_or_comment_writes(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(
        TaskConfig(
            keyword="AI Agent",
            send_mode="auto",
            replies_min=2,
            replies_max=2,
            min_delay=1,
            max_delay=1,
        )
    )
    runner = TaskRunner(session=None, store=store)
    runner._delay = lambda _: None
    page = FakePage()
    context = NoteContext(
        note_id="64d73b70c2133c0001abcd12",
        text="笔记正文",
        comments=(
            VisibleComment("comment-1", "第一条评论", "用户甲"),
            VisibleComment("comment-2", "第二条评论", "用户乙"),
            VisibleComment("comment-3", "第三条评论", "用户丙"),
        ),
        media="image",
    )
    page.context = context

    runner._process_note(task, page, context, FakeGenerator())
    runner._process_note(task, page, context, FakeGenerator())

    assert page.comments == ["和笔记内容相关的评论"]
    assert len(page.replies) == 2
    assert task.comment_count == 1
    assert task.reply_count == 2


def test_ai_failure_pauses_task_for_recovery(tmp_path) -> None:
    class FailingSession:
        def with_page(self, callback):
            raise GenerationError("service unavailable")

    store = TaskStore(tmp_path)
    task = store.create(TaskConfig(keyword="AI Agent"))
    result = TaskRunner(FailingSession(), store).run(task.id)

    assert result.task.status == TaskStatus.PAUSED
    assert result.task.last_error == "service unavailable"
