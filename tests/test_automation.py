from puppy.automation import TaskRunner
from puppy.evidence import EvidenceStore
from puppy.observations import CommentObservation, Observation, ObservationStore
from puppy.platforms import AdvanceResult, HumanInterventionRequired, ResourceLink
from puppy.tasks import TaskConfig, TaskStatus, TaskStore


class FakeSession:
    def __init__(self, evidence_root, platform="xiaohongshu") -> None:
        self.platform = platform
        self.running = True
        self.evidence_root = evidence_root

    def status(self):
        return {"running": self.running, "platform": self.platform if self.running else None}

    def with_page(self, callback):
        return callback(object(), EvidenceStore(self.evidence_root))


class FakeAdapter:
    def __init__(self, resources, *, exhaust_after=True) -> None:
        self.resources = list(resources)
        self.exhaust_after = exhaust_after
        self.opened = []
        self.closed = []

    def prepare(self, keyword, resource_type):
        self.keyword = keyword
        self.resource_type = resource_type

    def discover(self):
        return list(self.resources)

    def advance(self):
        return AdvanceResult(moved=False, source_exhausted=self.exhaust_after)

    def open(self, link):
        self.opened.append(link.resource_id)

    def observe(self, link):
        return Observation(
            platform="xiaohongshu",
            resource_type="note",
            resource_id=link.resource_id,
            source_url=f"https://www.xiaohongshu.com/explore/{link.resource_id}",
            metadata={"title": link.title},
            content=f"正文 {link.resource_id}",
            comments=(CommentObservation("用户", "公开评论"),),
        )

    def close(self, link):
        self.closed.append(link.resource_id)


def note(number):
    resource_id = f"{number:024x}"
    return ResourceLink(resource_id, "note", f"/explore/{resource_id}", f"笔记 {number}")


def test_count_wander_saves_unique_observations_without_writes(tmp_path, monkeypatch) -> None:
    adapter = FakeAdapter([note(1), note(2), note(3)])
    monkeypatch.setattr("puppy.automation.create_adapter", lambda *args, **kwargs: adapter)
    store = TaskStore(tmp_path / "tasks")
    observations = ObservationStore(tmp_path / "observations")
    task = store.create(TaskConfig(platform="xiaohongshu", max_items=2, min_delay=.5, max_delay=.5))
    runner = TaskRunner(FakeSession(tmp_path / "evidence"), store, observations)
    runner._delay = lambda _: None

    result = runner.run(task.id)

    assert result.task.status == TaskStatus.COMPLETE
    assert result.task.processed_resource_ids == [note(1).resource_id, note(2).resource_id]
    assert result.task.observation_count == 2
    assert result.task.visible_comment_count == 2
    assert adapter.opened == adapter.closed == result.task.processed_resource_ids
    assert len(observations.list()) == 2


def test_resume_skips_already_processed_resource(tmp_path, monkeypatch) -> None:
    adapter = FakeAdapter([note(1), note(2)])
    monkeypatch.setattr("puppy.automation.create_adapter", lambda *args, **kwargs: adapter)
    store = TaskStore(tmp_path / "tasks")
    observations = ObservationStore(tmp_path / "observations")
    task = store.create(TaskConfig(platform="xiaohongshu", max_items=2, min_delay=.5, max_delay=.5))
    task.processed_resource_ids.append(note(1).resource_id)
    task.observation_count = 1
    store.save(task)
    runner = TaskRunner(FakeSession(tmp_path / "evidence"), store, observations)
    runner._delay = lambda _: None

    result = runner.run(task.id)

    assert result.task.processed_resource_ids == [note(1).resource_id, note(2).resource_id]
    assert adapter.opened == [note(2).resource_id]


def test_platform_gate_waits_for_human_without_marking_content_processed(tmp_path, monkeypatch) -> None:
    class BlockedAdapter(FakeAdapter):
        def prepare(self, keyword, resource_type):
            raise HumanInterventionRequired("安全验证")

    monkeypatch.setattr(
        "puppy.automation.create_adapter",
        lambda *args, **kwargs: BlockedAdapter([]),
    )
    store = TaskStore(tmp_path / "tasks")
    task = store.create(TaskConfig(platform="xiaohongshu"))
    runner = TaskRunner(
        FakeSession(tmp_path / "evidence"),
        store,
        ObservationStore(tmp_path / "observations"),
    )

    result = runner.run(task.id)

    assert result.task.status == TaskStatus.WAITING_HUMAN
    assert result.task.observation_count == 0
    assert result.task.processed_resource_ids == []
