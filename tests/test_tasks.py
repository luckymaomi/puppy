import pytest

from puppy.tasks import TaskConfig, TaskState, TaskStatus, TaskStore


def test_task_store_round_trip_keeps_anonymous_wander_contract(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(
        TaskConfig(
            platform="bilibili",
            keyword="机器人总动员",
            resource_type="video",
            stop_mode="continuous",
        )
    )
    task.observation_count = 3
    task.visible_comment_count = 7
    store.save(task)

    loaded = store.load(task.id)
    assert loaded.config.platform == "bilibili"
    assert loaded.config.max_items is None
    assert loaded.observation_count == 3
    assert loaded.visible_comment_count == 7


@pytest.mark.parametrize(
    "config",
    [
        TaskConfig(platform="unknown"),
        TaskConfig(platform="bilibili", keyword="", resource_type="video"),
        TaskConfig(platform="xiaohongshu", resource_type="video"),
        TaskConfig(platform="xiaohongshu", stop_mode="count", max_items=0),
        TaskConfig(platform="xiaohongshu", stop_mode="duration", duration_minutes=None),
        TaskConfig(platform="xiaohongshu", comments_limit=101),
    ],
)
def test_task_config_rejects_invalid_platform_or_stop_contract(config: TaskConfig) -> None:
    with pytest.raises(ValueError):
        config.validate()


def test_xiaohongshu_can_wander_recommendations_without_keyword() -> None:
    config = TaskConfig(platform="xiaohongshu", keyword="", resource_type="note")
    config.validate()
    assert config.keyword == ""


def test_only_one_unfinished_wander_exists_for_the_shared_browser(tmp_path) -> None:
    store = TaskStore(tmp_path)
    first = store.create(TaskConfig(platform="xiaohongshu"))
    first.set_status(TaskStatus.PAUSED, reason="稍后继续")
    store.save(first)

    with pytest.raises(ValueError, match="已有未结束"):
        store.create(TaskConfig(platform="bilibili", keyword="AI", resource_type="video"))

    first.set_status(TaskStatus.STOPPED, reason="不再继续")
    store.save(first)
    second = store.create(TaskConfig(platform="bilibili", keyword="AI", resource_type="video"))
    assert second.id != first.id


def test_terminal_task_cannot_resume() -> None:
    state = TaskState(
        "20260804-120000-a1b2c3d4",
        TaskConfig(platform="xiaohongshu"),
    )
    state.set_status(TaskStatus.COMPLETE)
    with pytest.raises(ValueError, match="终态任务"):
        state.set_status(TaskStatus.RUNNING)
