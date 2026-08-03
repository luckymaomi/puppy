from datetime import datetime

import pytest

from xhs_robot.tasks import PendingDraft, TaskConfig, TaskState, TaskStatus, TaskStore


def test_task_store_round_trip_and_daily_write_limit_input(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(TaskConfig(profile_id="default", keyword="AI Agent", send_mode="approval"))
    task.register_write("comment")
    store.save(task)

    loaded = store.load(task.id)
    assert loaded.config.keyword == "AI Agent"
    assert loaded.comment_count == 1
    assert store.daily_write_count(datetime.now().date().isoformat()) == 1


@pytest.mark.parametrize(
    "config",
    [
        TaskConfig(profile_id="default", keyword=""),
        TaskConfig(profile_id="default", keyword="ok", max_notes=0),
        TaskConfig(profile_id="default", keyword="ok", replies_min=2, replies_max=1),
        TaskConfig(profile_id="default", keyword="ok", send_mode="unknown"),
    ],
)
def test_task_config_rejects_unsafe_or_ambiguous_limits(config: TaskConfig) -> None:
    with pytest.raises(ValueError):
        config.validate()


def test_terminal_task_cannot_be_resumed() -> None:
    state = TaskState("20260802-120000-abcd", TaskConfig(profile_id="default", keyword="test"))
    state.set_status(TaskStatus.COMPLETE)
    with pytest.raises(ValueError, match="终态任务"):
        state.set_status(TaskStatus.RUNNING)


def test_uncertain_write_requires_explicit_resolution_before_retry() -> None:
    state = TaskState("20260802-120000-abcd", TaskConfig(profile_id="default", keyword="test"))
    state.pending_draft = PendingDraft(
        kind="comment", text="待发送内容", note_id="64d73b70c2133c0001abcd12"
    )
    state.begin_write()

    assert state.write_in_flight is not None
    state.resolve_write(sent=False)
    assert state.write_in_flight is None
    assert state.pending_draft is not None
    assert state.comment_count == 0

    state.begin_write()
    state.resolve_write(sent=True)
    assert state.pending_draft is None
    assert state.comment_count == 1


def test_each_browser_profile_has_only_one_unfinished_task(tmp_path) -> None:
    store = TaskStore(tmp_path)
    first = store.create(TaskConfig(profile_id="default", keyword="第一个任务"))
    first.set_status(TaskStatus.PAUSED, reason="稍后继续")
    store.save(first)

    with pytest.raises(ValueError, match="已有未结束任务"):
        store.create(TaskConfig(profile_id="default", keyword="重复任务"))

    first.set_status(TaskStatus.STOPPED, reason="不再继续")
    store.save(first)
    second = store.create(TaskConfig(profile_id="default", keyword="新任务"))

    assert second.id != first.id
