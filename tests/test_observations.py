import pytest

from puppy.observations import CommentObservation, Observation, ObservationStore


def test_observation_store_updates_the_same_platform_resource_in_place(tmp_path) -> None:
    store = ObservationStore(tmp_path)
    first = Observation(
        platform="bilibili",
        resource_type="video",
        resource_id="BV12C411W7CM",
        source_url="https://www.bilibili.com/video/BV12C411W7CM?token=secret",
        metadata={"title": "第一次"},
        content="简介",
    )
    second = Observation(
        platform="bilibili",
        resource_type="video",
        resource_id="BV12C411W7CM",
        source_url="https://www.bilibili.com/video/BV12C411W7CM",
        metadata={"title": "第二次"},
        content="更新简介",
        comments=(CommentObservation("用户", "评论"),),
    )

    assert store.save(first) == store.save(second)
    loaded = store.list()
    assert len(loaded) == 1
    assert loaded[0]["metadata"]["title"] == "第二次"
    assert len(loaded[0]["comments"]) == 1


def test_observation_store_rejects_unsafe_resource_id(tmp_path) -> None:
    store = ObservationStore(tmp_path)
    with pytest.raises(ValueError, match="资源 ID"):
        store.save(
            Observation(
                platform="xiaohongshu",
                resource_type="note",
                resource_id="../outside",
                source_url="https://www.xiaohongshu.com/explore",
                metadata={},
                content="",
            )
        )
