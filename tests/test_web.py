from fastapi.testclient import TestClient

from puppy.config import AIConfigStore
from puppy.page import PageGate
from puppy.paths import AppPaths
from puppy.tasks import TaskConfig, TaskStatus, TaskStore
from puppy.web.api import create_app
from puppy.web.supervisor import EventBroker


VALID_ENV = """XHS_PROVIDER=siliconflow
XHS_API_KEY=sk-web-test-value
XHS_BASE_URL=https://api.siliconflow.cn/v1
XHS_MODEL=Pro/zai-org/GLM-5.1
XHS_API_STYLE=chat_completions
XHS_REQUEST_TIMEOUT_SECONDS=60
XHS_MAX_OUTPUT_TOKENS=300
"""


class FakeSupervisor:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.paused: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def snapshot(self):
        return {"running": False, "task_id": None}

    def start(self, task_id: str):
        self.started.append(task_id)
        task = self.store.load(task_id)
        task.set_status(TaskStatus.RUNNING)
        self.store.save(task)
        return task

    def pause(self, task_id: str, *, reason: str = "用户从控制台请求暂停"):
        self.paused.append((task_id, reason))
        task = self.store.load(task_id)
        task.set_status(TaskStatus.PAUSED, reason=reason)
        self.store.save(task)
        return task

    def wait_for_page(self, task_id: str, *, status: TaskStatus, reason: str):
        task = self.store.load(task_id)
        task.set_status(status, reason=reason)
        self.store.save(task)
        return task

    def stop(self, task_id: str):
        self.stopped.append(task_id)
        task = self.store.load(task_id)
        task.set_status(TaskStatus.STOPPED, reason="用户从控制台停止任务")
        self.store.save(task)
        return task

    def shutdown(self) -> None:
        pass


def web_fixture(tmp_path):
    paths = AppPaths(tmp_path / "runtime")
    store = TaskStore(paths.tasks_dir)
    task = store.create(TaskConfig(profile_id="default", keyword="AI Agent"))
    task.set_status(TaskStatus.WAITING_HUMAN, reason="验证码")
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


def test_bootstrap_requires_token_and_projects_waiting_human_task(tmp_path) -> None:
    app, task, _ = web_fixture(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/bootstrap").status_code == 401
        response = client.get(
            "/api/bootstrap", headers={"x-puppy-token": "local-token"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_task"]["id"] == task.id
    assert payload["current_task"]["status"] == "waiting_human"
    assert "sk-web-test-value" not in response.text


def test_human_confirmation_rechecks_gate_before_resuming(tmp_path) -> None:
    app, task, supervisor = web_fixture(tmp_path)
    app.state.context.browser.status = lambda: {
        "running": True,
        "profile_id": "default",
    }
    app.state.context.browser.with_page = lambda callback: (PageGate.HUMAN, "验证码")

    with TestClient(app) as client:
        blocked = client.post(
            f"/api/tasks/{task.id}/resume",
            headers={"x-puppy-token": "local-token"},
        )
        assert blocked.status_code == 409
        assert supervisor.started == []

        app.state.context.browser.with_page = lambda callback: (PageGate.READY, None)
        resumed = client.post(
            f"/api/tasks/{task.id}/resume",
            headers={"x-puppy-token": "local-token"},
        )

    assert resumed.status_code == 202
    assert supervisor.started == [task.id]


def test_stopping_browser_pauses_current_task_and_closes_session(tmp_path) -> None:
    app, task, supervisor = web_fixture(tmp_path)
    other = app.state.context.tasks.create(
        TaskConfig(profile_id="other-profile", keyword="另一个账号的任务")
    )
    other.set_status(TaskStatus.WAITING_HUMAN, reason="等待另一个账号")
    app.state.context.tasks.save(other)
    running = {"value": True}

    def close_browser():
        running["value"] = False
        return {
            "closed": True,
            "already_stopped": False,
            "browser_pid": 1234,
            "profile_id": "default",
        }

    app.state.context.browser.close = close_browser
    app.state.context.browser.status = lambda: {
        "running": running["value"],
        "profile_id": "default" if running["value"] else None,
    }

    with TestClient(app) as client:
        response = client.post(
            "/api/browser/stop",
            headers={"x-puppy-token": "local-token"},
        )

    assert response.status_code == 200
    assert response.json()["browser"]["running"] is False
    assert response.json()["paused_task"]["status"] == "paused"
    assert supervisor.paused == [(task.id, "浏览器已停止，任务暂停")]
    assert app.state.context.tasks.load(task.id).status == TaskStatus.PAUSED
    assert app.state.context.tasks.load(other.id).status == TaskStatus.WAITING_HUMAN


def test_stopping_task_is_terminal_and_does_not_stop_browser(tmp_path) -> None:
    app, task, supervisor = web_fixture(tmp_path)
    app.state.context.browser.status = lambda: {
        "running": True,
        "profile_id": "default",
    }

    with TestClient(app) as client:
        response = client.post(
            f"/api/tasks/{task.id}/stop",
            headers={"x-puppy-token": "local-token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert supervisor.stopped == [task.id]


def test_event_stream_replays_only_confirmed_events_after_last_sequence() -> None:
    broker = EventBroker()
    broker.publish("search_submitted", task_id="task-1")
    broker.publish("search_complete", task_id="task-1", result_count=5)

    subscriber = broker.subscribe(after_sequence=1)
    replayed = subscriber.get_nowait()

    assert replayed["sequence"] == 2
    assert replayed["type"] == "search_complete"
    assert replayed["result_count"] == 5
    assert subscriber.empty()
