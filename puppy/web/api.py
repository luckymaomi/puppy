from __future__ import annotations

import asyncio
import json
import queue
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..ai import AIProvider
from ..browser import BrowserSession
from ..config import AIConfig, AIConfigStore, ConfigurationError
from ..observations import ObservationStore
from ..paths import AppPaths
from ..platforms import HumanInterventionRequired, create_adapter
from ..tasks import TaskConfig, TaskStore, TERMINAL_STATUSES
from .supervisor import EventBroker, TaskSupervisor


PUBLIC_DIR = Path(__file__).resolve().parent / "public"


class ConfigUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    clear_api_key: bool = False


class BrowserStart(BaseModel):
    platform: Literal["xiaohongshu", "bilibili"]


class TaskCreate(BaseModel):
    platform: Literal["xiaohongshu", "bilibili"]
    keyword: str = Field(default="", max_length=80)
    resource_type: Literal["note", "video", "article"]
    stop_mode: Literal["count", "duration", "continuous"] = "count"
    max_items: int | None = Field(default=20, ge=1, le=1000)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    comments_limit: int = Field(default=20, ge=0, le=100)
    min_delay: float = Field(default=2.0, ge=0.5, le=120)
    max_delay: float = Field(default=5.0, ge=0.5, le=120)


@dataclass(slots=True)
class WebContext:
    token: str
    paths: AppPaths
    config: AIConfigStore
    browser: BrowserSession
    tasks: TaskStore
    observations: ObservationStore
    events: EventBroker
    supervisor: TaskSupervisor


def create_app(
    token: str,
    *,
    paths: AppPaths | None = None,
    config_store: AIConfigStore | None = None,
    supervisor: TaskSupervisor | None = None,
) -> FastAPI:
    app_paths = paths or AppPaths()
    app_paths.ensure()
    config = config_store or AIConfigStore()
    config.ensure()
    events = EventBroker()

    def project_evidence(record: dict[str, Any]) -> None:
        event_type = str(record.get("type") or "evidence")
        events.publish(event_type, **{key: value for key, value in record.items() if key != "type"})

    browser = BrowserSession(app_paths, event_sink=project_evidence)
    tasks = TaskStore(app_paths.tasks_dir)
    observations = ObservationStore(app_paths.observations_dir)
    task_supervisor = supervisor or TaskSupervisor(browser, tasks, observations, events)
    context = WebContext(
        token=token,
        paths=app_paths,
        config=config,
        browser=browser,
        tasks=tasks,
        observations=observations,
        events=events,
        supervisor=task_supervisor,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            try:
                task_supervisor.shutdown()
            finally:
                browser.close()

    app = FastAPI(title="Puppy", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.context = context

    def inspect_browser() -> tuple[str, str | None]:
        browser_state = browser.status()
        if not browser_state.get("running"):
            return "stopped", "匿名浏览器未启动"

        def inspect(page, evidence):
            adapter = create_adapter(
                str(browser_state["platform"]), page, evidence, comments_limit=0
            )
            try:
                adapter.guard()
                return "ready", None
            except HumanInterventionRequired as exc:
                return "human", str(exc)

        return browser.with_page(inspect)

    def require_task_browser(task_id: str):
        task = tasks.load(task_id)
        browser_state = browser.status()
        if not browser_state.get("running"):
            raise HTTPException(status_code=409, detail="请先启动匿名浏览器")
        if browser_state.get("platform") != task.config.platform:
            raise HTTPException(status_code=409, detail="当前浏览器平台与任务平台不一致")
        return browser_state, task

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            provided = request.headers.get("x-puppy-token") or request.query_params.get("token")
            if not provided or not secrets.compare_digest(provided, token):
                return JSONResponse({"error": "本地工作台凭证无效"}, status_code=401)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                host = request.headers.get("host")
                if origin and host and origin != f"http://{host}":
                    return JSONResponse({"error": "请求来源不匹配"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ConfigurationError)
    async def configuration_error(_: Request, exc: ConfigurationError):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def runtime_error(_: Request, exc: RuntimeError):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "index.html")

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        task_list = tasks.list()[:50]
        active = task_supervisor.snapshot()
        current = next(
            (item for item in task_list if item.id == active["task_id"]),
            None,
        ) or next(
            (item for item in task_list if item.status not in TERMINAL_STATUSES),
            None,
        )
        return {
            "configuration": config.read_public(),
            "browser": browser.status(),
            "supervisor": active,
            "current_task": current.to_dict() if current else None,
            "tasks": [item.to_dict() for item in task_list],
            "observations": observations.list(limit=30),
            "events": events.history(),
        }

    @app.put("/api/config")
    def save_config(body: ConfigUpdate) -> dict[str, Any]:
        result = config.save(body.values, clear_api_key=body.clear_api_key)
        events.publish("configuration_updated")
        return result

    @app.post("/api/provider/probe")
    def probe_provider() -> dict[str, object]:
        return AIProvider(AIConfig.from_env_file(config.path)).health()

    @app.post("/api/provider/generate-probe")
    def generate_probe() -> dict[str, object]:
        return AIProvider(AIConfig.from_env_file(config.path)).health(generate=True)

    @app.get("/api/models")
    def list_models() -> dict[str, Any]:
        runtime = AIConfig.from_env_file(config.path)
        models = AIProvider(runtime).list_model_ids()
        return {"count": len(models), "models": models, "selected": runtime.model}

    @app.post("/api/browser/start")
    def start_browser(body: BrowserStart) -> dict[str, Any]:
        existing = tasks.find_unfinished()
        if existing is not None and existing.config.platform != body.platform:
            raise HTTPException(
                status_code=409,
                detail=f"未结束任务 {existing.id} 绑定了其他平台",
            )
        result = browser.start(body.platform)
        events.publish("browser_updated", platform=body.platform)
        return {**result, "browser": browser.status()}

    @app.post("/api/browser/stop")
    def stop_browser() -> dict[str, Any]:
        active = task_supervisor.snapshot()
        unfinished = tasks.find_unfinished()
        paused_task = None
        task_id = active["task_id"] or (unfinished.id if unfinished else None)
        if task_id is not None:
            paused_task = task_supervisor.pause(str(task_id), reason="匿名浏览器已停止，漫游暂停")
        result = browser.close()
        events.publish(
            "browser_stopped",
            platform=result.get("platform"),
            paused_task_id=paused_task.id if paused_task else None,
            session_data_removed=result.get("session_data_removed", False),
        )
        events.publish("browser_updated")
        return {**result, "browser": browser.status(), "paused_task": paused_task.to_dict() if paused_task else None}

    @app.get("/api/browser/status")
    def browser_status() -> dict[str, Any]:
        return browser.status()

    @app.post("/api/browser/check")
    def check_browser() -> dict[str, Any]:
        gate, reason = inspect_browser()
        return {"gate": gate, "reason": reason}

    @app.post("/api/tasks", status_code=202)
    def create_task(body: TaskCreate) -> dict[str, Any]:
        browser_state = browser.status()
        if not browser_state.get("running"):
            raise HTTPException(status_code=409, detail="请先启动匿名浏览器")
        if browser_state.get("platform") != body.platform:
            raise HTTPException(status_code=409, detail="浏览器平台与任务平台不一致")
        task_config = TaskConfig(**body.model_dump())
        task_config.validate()
        return task_supervisor.create_and_start(task_config).to_dict()

    @app.get("/api/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return [item.to_dict() for item in tasks.list()[:100]]

    @app.get("/api/tasks/{task_id}")
    def read_task(task_id: str) -> dict[str, Any]:
        return tasks.load(task_id).to_dict()

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str) -> dict[str, Any]:
        return task_supervisor.pause(task_id).to_dict()

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    def resume_task(task_id: str) -> dict[str, Any]:
        require_task_browser(task_id)
        gate, reason = inspect_browser()
        if gate != "ready":
            task_supervisor.wait_for_page(task_id, reason=reason or "页面需要人工处理")
            raise HTTPException(status_code=409, detail=f"{reason or '页面需要人工处理'}；处理后再继续")
        return task_supervisor.start(task_id).to_dict()

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict[str, Any]:
        return task_supervisor.stop(task_id).to_dict()

    @app.get("/api/observations")
    def list_observations(limit: int = 100) -> list[dict[str, Any]]:
        return observations.list(limit=limit)

    @app.get("/api/events")
    async def event_stream(request: Request, after: int = 0) -> StreamingResponse:
        if after < 0:
            raise HTTPException(status_code=422, detail="事件序号不能小于 0")
        subscriber = events.subscribe(after_sequence=after)

        async def generate():
            try:
                yield _sse({"type": "connected"})
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.to_thread(subscriber.get, True, 2)
                    except queue.Empty:
                        event = {"type": "heartbeat"}
                    yield _sse(event)
            finally:
                events.unsubscribe(subscriber)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.mount("/assets", StaticFiles(directory=PUBLIC_DIR), name="assets")
    return app


def _sse(value: dict[str, Any]) -> str:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n"
