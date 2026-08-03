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
from ..page import PageGate, XhsPage
from ..paths import AppPaths
from ..profiles import BrowserProfileStore
from ..tasks import TaskConfig, TaskStatus, TaskStore, TERMINAL_STATUSES
from .supervisor import EventBroker, TaskSupervisor


PUBLIC_DIR = Path(__file__).resolve().parent / "public"


class ConfigUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    clear_api_key: bool = False


class BrowserProfileCreate(BaseModel):
    name: str


class BrowserStart(BaseModel):
    profile_id: str


class TaskCreate(BaseModel):
    keyword: str
    max_notes: int = 10
    replies_min: int = 1
    replies_max: int = 2
    send_mode: Literal["approval", "auto"] = "approval"
    min_delay: float = 3
    max_delay: float = 7
    daily_write_limit: int = 30


class ApprovalAction(BaseModel):
    action: Literal["send", "edit", "skip", "pause"]
    text: str | None = None


class ResolveAction(BaseModel):
    result: Literal["sent", "not-sent"]


@dataclass(slots=True)
class WebContext:
    token: str
    paths: AppPaths
    config: AIConfigStore
    profiles: BrowserProfileStore
    browser: BrowserSession
    tasks: TaskStore
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
    profiles = BrowserProfileStore(app_paths)
    events = EventBroker()

    def project_evidence(record: dict[str, Any]) -> None:
        event_type = str(record.get("type") or "evidence")
        events.publish(
            event_type,
            **{key: value for key, value in record.items() if key != "type"},
        )

    browser = BrowserSession(app_paths, event_sink=project_evidence)
    tasks = TaskStore(app_paths.tasks_dir)
    task_supervisor = supervisor or TaskSupervisor(
        browser,
        tasks,
        events,
        config_loader=lambda: AIConfig.from_env_file(config.path),
    )
    context = WebContext(
        token=token,
        paths=app_paths,
        config=config,
        profiles=profiles,
        browser=browser,
        tasks=tasks,
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

    app = FastAPI(
        title="xhs-robot",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.context = context

    def browser_view() -> dict[str, Any]:
        status = browser.status()
        profile_id = status.get("profile_id")
        if profile_id:
            profile = profiles.load(str(profile_id))
            status["profile_name"] = profile.name
        else:
            status["profile_name"] = None
        return status

    def inspect_gate() -> tuple[PageGate, str | None]:
        def inspect(page, evidence):
            return XhsPage(page, evidence).check_gate()

        return browser.with_page(inspect)

    def require_task_browser(task_id: str) -> tuple[dict[str, Any], Any]:
        task = tasks.load(task_id)
        browser_state = browser.status()
        if not browser_state["running"]:
            raise HTTPException(status_code=409, detail="请先启动任务绑定的浏览器资料")
        if browser_state.get("profile_id") != task.config.profile_id:
            raise HTTPException(status_code=409, detail="当前浏览器资料与任务绑定资料不一致")
        return browser_state, task

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            provided = request.headers.get("x-xhs-token") or request.query_params.get(
                "token"
            )
            if not provided or not secrets.compare_digest(provided, token):
                return JSONResponse({"error": "本地控制台凭证无效"}, status_code=401)
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
        task_list = tasks.list()[:30]
        active = task_supervisor.snapshot()
        browser_state = browser_view()
        current_id = active["task_id"]
        if current_id is None:
            active_profile_id = browser_state.get("profile_id")
            current = next(
                (
                    item
                    for item in task_list
                    if item.status not in TERMINAL_STATUSES
                    and item.config.profile_id == active_profile_id
                ),
                None,
            ) or next(
                (item for item in task_list if item.status not in TERMINAL_STATUSES),
                None,
            )
        else:
            current = next(
                (item for item in task_list if item.id == current_id),
                None,
            )
        return {
            "configuration": config.read_public(),
            "browser": browser_state,
            "browser_profiles": [item.to_dict() for item in profiles.list()],
            "supervisor": active,
            "current_task": current.to_dict() if current else None,
            "tasks": [item.to_dict() for item in task_list],
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

    @app.get("/api/browser/profiles")
    def list_browser_profiles() -> list[dict[str, Any]]:
        return [item.to_dict() for item in profiles.list()]

    @app.post("/api/browser/profiles", status_code=201)
    def create_browser_profile(body: BrowserProfileCreate) -> dict[str, Any]:
        profile = profiles.create(body.name)
        events.publish(
            "browser_profile_created", profile_id=profile.id, profile_name=profile.name
        )
        return profile.to_dict()

    @app.post("/api/browser/start")
    def start_browser(body: BrowserStart) -> dict[str, Any]:
        profile = profiles.load(body.profile_id)
        active = task_supervisor.snapshot()
        if active["running"]:
            active_task = tasks.load(str(active["task_id"]))
            if active_task.config.profile_id != profile.id:
                raise RuntimeError("请先暂停当前任务，再切换浏览器资料")
        result = browser.start(profile)
        profile = profiles.mark_used(profile.id)
        events.publish("browser_updated")
        return {**result, "profile": profile.to_dict(), "browser": browser_view()}

    @app.post("/api/browser/stop")
    def stop_browser() -> dict[str, Any]:
        browser_state = browser.status()
        active_profile_id = browser_state.get("profile_id")
        active = task_supervisor.snapshot()
        task_id = active["task_id"]
        if task_id is not None and active_profile_id is not None:
            active_task = tasks.load(str(task_id))
            if active_task.config.profile_id != active_profile_id:
                raise RuntimeError("运行任务与当前浏览器资料不一致")
        if task_id is None and active_profile_id is not None:
            unfinished = tasks.find_unfinished(str(active_profile_id))
            task_id = unfinished.id if unfinished else None
        paused_task = None
        if task_id is not None:
            try:
                paused_task = task_supervisor.pause(
                    str(task_id), reason="浏览器已停止，任务暂停"
                )
            except ValueError:
                if tasks.load(str(task_id)).status not in TERMINAL_STATUSES:
                    raise
        result = browser.close()
        events.publish(
            "browser_stopped",
            closed=result["closed"],
            profile_id=active_profile_id,
            paused_task_id=paused_task.id if paused_task else None,
        )
        events.publish("browser_updated")
        return {
            **result,
            "browser": browser_view(),
            "paused_task": paused_task.to_dict() if paused_task else None,
        }

    @app.get("/api/browser/status")
    def browser_status() -> dict[str, Any]:
        return browser_view()

    @app.post("/api/browser/check")
    def check_browser() -> dict[str, Any]:
        gate, reason = inspect_gate()
        return {"gate": gate.value, "reason": reason}

    @app.post("/api/tasks", status_code=202)
    def create_task(body: TaskCreate) -> dict[str, Any]:
        if not config.read_public()["ready"]:
            raise HTTPException(status_code=409, detail="请先完成 AI 配置")
        browser_state = browser.status()
        if not browser_state["running"] or not browser_state.get("profile_id"):
            raise HTTPException(status_code=409, detail="请先启动小红书浏览器")
        profile_id = str(browser_state["profile_id"])
        existing = tasks.find_unfinished(profile_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"当前浏览器资料已有未完成任务 {existing.id}",
            )
        gate, reason = inspect_gate()
        if gate != PageGate.READY:
            raise HTTPException(
                status_code=409,
                detail=reason or "请先完成浏览器登录或安全验证",
            )
        task_config = TaskConfig(profile_id=profile_id, **body.model_dump())
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
        _, task = require_task_browser(task_id)
        gate, reason = inspect_gate()
        if gate != PageGate.READY:
            status = (
                TaskStatus.WAITING_LOGIN
                if gate == PageGate.LOGIN
                else TaskStatus.WAITING_HUMAN
            )
            message = reason or "页面仍要求人工处理"
            task_supervisor.wait_for_page(task_id, status=status, reason=message)
            raise HTTPException(status_code=409, detail=f"{message}；处理后再继续")
        return task_supervisor.start(task_id).to_dict()

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict[str, Any]:
        return task_supervisor.stop(task_id).to_dict()

    @app.post("/api/tasks/{task_id}/approval", status_code=202)
    def approve_task(task_id: str, body: ApprovalAction) -> dict[str, Any]:
        require_task_browser(task_id)
        return task_supervisor.approve(
            task_id, action=body.action, text=body.text
        ).to_dict()

    @app.post("/api/tasks/{task_id}/resolve")
    def resolve_task(task_id: str, body: ResolveAction) -> dict[str, Any]:
        require_task_browser(task_id)
        return task_supervisor.resolve(
            task_id, sent=body.result == "sent"
        ).to_dict()

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
