from fastapi.testclient import TestClient

from puppy.config import AIConfigStore
from puppy.observations import Observation
from puppy.paths import AppPaths
from puppy.tasks import TaskConfig, TaskStatus, TaskStore
from puppy.web.api import create_app
from puppy.web.supervisor import EventBroker


VALID_ENV = """PUPPY_AI_PROVIDER=siliconflow
PUPPY_AI_API_KEY=sk-web-test-value
PUPPY_AI_BASE_URL=https://api.siliconflow.cn/v1
PUPPY_AI_MODEL=Pro/zai-org/GLM-5.1
PUPPY_AI_API_STYLE=chat_completions
PUPPY_AI_REQUEST_TIMEOUT_SECONDS=60
PUPPY_AI_MAX_OUTPUT_TOKENS=300
"""


class FakeSupervisor:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.paused: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def snapshot(self):
        return {"running": False, "task_id": None}

    def create_and_start(self, config):
        task = self.store.create(config)
        return self.start(task.id)

    def start(self, task_id: str):
        self.started.append(task_id)
        task = self.store.load(task_id)
        task.set_status(TaskStatus.RUNNING)
        self.store.save(task)
        return task

    def pause(self, task_id: str, *, reason: str = "用户从工作台暂停漫游"):
        self.paused.append((task_id, reason))
        task = self.store.load(task_id)
        task.set_status(TaskStatus.PAUSED, reason=reason)
        self.store.save(task)
        return task

    def wait_for_page(self, task_id: str, *, reason: str):
        task = self.store.load(task_id)
        task.set_status(TaskStatus.WAITING_HUMAN, reason=reason)
        self.store.save(task)
        return task

    def stop(self, task_id: str):
        self.stopped.append(task_id)
        task = self.store.load(task_id)
        task.set_status(TaskStatus.STOPPED, reason="用户停止任务")
        self.store.save(task)
        return task

    def shutdown(self) -> None:
        pass


def web_fixture(tmp_path, *, with_task=True):
    paths = AppPaths(tmp_path / "runtime")
    paths.ensure()
    store = TaskStore(paths.tasks_dir)
    task = None
    if with_task:
        task = store.create(TaskConfig(platform="xiaohongshu"))
        task.set_status(TaskStatus.WAITING_HUMAN, reason="安全验证")
        store.save(task)
    env_file = tmp_path / ".env"
    example_file = tmp_path / ".env.example"
    env_file.write_text(VALID_ENV, encoding="utf-8")
    example_file.write_text(VALID_ENV, encoding="utf-8")
    supervisor = FakeSupervisor(store)
    app = create_app(
        "local-token",
        paths=paths,
        config_store=AIConfigStore(env_file, example_file),
        supervisor=supervisor,
    )
    return app, task, supervisor


def test_bootstrap_requires_token_and_projects_anonymous_task(tmp_path) -> None:
    app, task, _ = web_fixture(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/bootstrap").status_code == 401
        response = client.get("/api/bootstrap", headers={"x-puppy-token": "local-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_task"]["id"] == task.id
    assert payload["current_task"]["config"]["platform"] == "xiaohongshu"
    assert "sk-web-test-value" not in response.text


def test_resume_rechecks_public_page_gate(tmp_path) -> None:
    app, task, supervisor = web_fixture(tmp_path)
    app.state.context.browser.status = lambda: {"running": True, "platform": "xiaohongshu"}
    app.state.context.browser.with_page = lambda callback: ("human", "安全验证")

    with TestClient(app) as client:
        blocked = client.post(
            f"/api/tasks/{task.id}/resume",
            headers={"x-puppy-token": "local-token"},
        )
        assert blocked.status_code == 409
        assert supervisor.started == []

        app.state.context.browser.with_page = lambda callback: ("ready", None)
        resumed = client.post(
            f"/api/tasks/{task.id}/resume",
            headers={"x-puppy-token": "local-token"},
        )

    assert resumed.status_code == 202
    assert supervisor.started == [task.id]


def test_stopping_browser_pauses_wander_and_reports_session_cleanup(tmp_path) -> None:
    app, task, supervisor = web_fixture(tmp_path)
    app.state.context.browser.status = lambda: {"running": True, "platform": "xiaohongshu"}
    app.state.context.browser.close = lambda: {
        "closed": True,
        "already_stopped": False,
        "browser_pid": 1234,
        "platform": "xiaohongshu",
        "session_data_removed": True,
    }

    with TestClient(app) as client:
        response = client.post("/api/browser/stop", headers={"x-puppy-token": "local-token"})

    assert response.status_code == 200
    assert response.json()["session_data_removed"] is True
    assert response.json()["paused_task"]["status"] == "paused"
    assert supervisor.paused == [(task.id, "匿名浏览器已停止，漫游暂停")]


def test_create_task_requires_matching_anonymous_browser_not_ai_readiness(tmp_path) -> None:
    app, _, supervisor = web_fixture(tmp_path, with_task=False)
    app.state.context.browser.status = lambda: {"running": True, "platform": "bilibili"}
    body = {
        "platform": "bilibili",
        "keyword": "机器人总动员",
        "resource_type": "video",
        "stop_mode": "count",
        "max_items": 5,
        "duration_minutes": None,
        "comments_limit": 10,
        "min_delay": 2,
        "max_delay": 5,
    }

    with TestClient(app) as client:
        response = client.post(
            "/api/tasks",
            json=body,
            headers={"x-puppy-token": "local-token"},
        )

    assert response.status_code == 202
    assert response.json()["config"]["platform"] == "bilibili"
    assert supervisor.started == [response.json()["id"]]


def test_bootstrap_returns_local_observations(tmp_path) -> None:
    app, _, _ = web_fixture(tmp_path, with_task=False)
    app.state.context.observations.save(
        Observation(
            platform="xiaohongshu",
            resource_type="note",
            resource_id="64d73b70c2133c0001abcd12",
            source_url="https://www.xiaohongshu.com/explore/64d73b70c2133c0001abcd12",
            metadata={"title": "公开笔记"},
            content="正文",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/bootstrap", headers={"x-puppy-token": "local-token"})

    assert response.json()["observations"][0]["metadata"]["title"] == "公开笔记"


def test_event_stream_replays_only_confirmed_events_after_last_sequence() -> None:
    broker = EventBroker()
    broker.publish("search_complete", task_id="task-1")
    broker.publish("observation_saved", task_id="task-1", resource_id="BV1")

    subscriber = broker.subscribe(after_sequence=1)
    replayed = subscriber.get_nowait()

    assert replayed["sequence"] == 2
    assert replayed["type"] == "observation_saved"
    assert subscriber.empty()
