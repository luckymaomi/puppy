from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

from .evidence import EvidenceStore, redact_url
from .paths import AppPaths
from .profiles import BrowserProfile


EXPLORE_URL = "https://www.xiaohongshu.com/explore"
T = TypeVar("T")


def endpoint_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/json/version", timeout=0.5) as response:
            return response.status == 200
    except (URLError, TimeoutError, OSError):
        return False


class BrowserSession:
    def __init__(
        self,
        paths: AppPaths,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.paths = paths
        self.event_sink = event_sink
        self._process: subprocess.Popen[bytes] | None = None

    def start(
        self, profile: BrowserProfile, cdp_port: int = 9229
    ) -> dict[str, Any]:
        if not 1024 <= cdp_port <= 65535:
            raise ValueError("CDP 端口必须在 1024 到 65535 之间")
        endpoint = f"http://127.0.0.1:{cdp_port}"
        if endpoint_ready(endpoint):
            if self.paths.session_file.is_file():
                session = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
                if (
                    session.get("endpoint") == endpoint
                    and session.get("profile_id") == profile.id
                ):
                    return {**session, "already_running": True}
            raise RuntimeError("已有其他浏览器资料正在运行，请先停止当前浏览器")

        self.paths.ensure()
        browser_data_dir = self.paths.browser_data_dir.resolve()
        browser_data_dir.mkdir(parents=True, exist_ok=True)
        evidence = EvidenceStore(self.paths.evidence_dir, event_sink=self.event_sink)

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
        if not executable.exists():
            raise RuntimeError("Playwright Chromium 可执行文件不存在，请先安装浏览器")

        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            [
                str(executable),
                f"--remote-debugging-port={cdp_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={browser_data_dir}",
                f"--profile-directory={profile.chromium_directory}",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1440,900",
                "--lang=zh-CN",
                EXPLORE_URL,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        self._process = process

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Chromium 启动失败，退出码 {process.returncode}")
            if endpoint_ready(endpoint):
                break
            time.sleep(0.25)
        else:
            raise TimeoutError("等待 Chromium 调试端口超时")

        session = {
            "endpoint": endpoint,
            "cdp_port": cdp_port,
            "browser_pid": process.pid,
            "profile_id": profile.id,
            "chromium_directory": profile.chromium_directory,
            "browser_data_dir": str(browser_data_dir),
            "evidence_dir": str(evidence.run_dir.resolve()),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "already_running": False,
        }
        temporary = self.paths.session_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.paths.session_file)
        evidence.bind(profile_id=profile.id)
        evidence.event("browser_started", pid=process.pid, url=EXPLORE_URL)
        return session

    def load(self) -> dict[str, Any]:
        if not self.paths.session_file.exists():
            raise RuntimeError("没有当前浏览器会话，请先运行 `python app.py` 打开工作台")
        session = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
        endpoint = session.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint_ready(endpoint):
            raise RuntimeError("当前浏览器调试端口不可用；浏览器可能已关闭")
        return session

    def status(self) -> dict[str, Any]:
        if not self.paths.session_file.is_file():
            return {"running": False}
        try:
            session = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"running": False}
        endpoint = session.get("endpoint")
        running = isinstance(endpoint, str) and endpoint_ready(endpoint)
        return {
            "running": running,
            "endpoint": endpoint if running else None,
            "cdp_port": session.get("cdp_port"),
            "browser_pid": session.get("browser_pid") if running else None,
            "profile_id": session.get("profile_id") if running else None,
            "started_at": session.get("started_at"),
        }

    def close(self, timeout_seconds: float = 8.0) -> dict[str, Any]:
        if timeout_seconds <= 0:
            raise ValueError("关闭浏览器超时必须大于 0 秒")
        if not self.paths.session_file.is_file():
            return {"closed": False, "already_stopped": True}
        try:
            session = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.paths.session_file.unlink(missing_ok=True)
            return {"closed": False, "already_stopped": True}

        endpoint = session.get("endpoint")
        browser_pid = session.get("browser_pid")
        profile_id = session.get("profile_id")
        running = isinstance(endpoint, str) and endpoint_ready(endpoint)
        if running:
            playwright = sync_playwright().start()
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                browser.new_browser_cdp_session().send("Browser.close")
            except Exception as exc:
                if endpoint_ready(endpoint):
                    raise RuntimeError("关闭项目 Chromium 失败") from exc
            finally:
                playwright.stop()

            deadline = time.monotonic() + timeout_seconds
            while endpoint_ready(endpoint) and time.monotonic() < deadline:
                time.sleep(0.1)
            if endpoint_ready(endpoint):
                raise TimeoutError("等待项目 Chromium 退出超时")

        managed = self._process
        if (
            managed is not None
            and managed.pid == browser_pid
            and managed.poll() is None
        ):
            try:
                managed.wait(timeout=1)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/PID", str(managed.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                try:
                    managed.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError("项目 Chromium 进程树未能完整退出") from exc

        self.paths.session_file.unlink(missing_ok=True)
        self._process = None
        return {
            "closed": running,
            "already_stopped": not running,
            "browser_pid": browser_pid,
            "profile_id": profile_id,
        }

    def with_page(self, callback: Callable[[Any, EvidenceStore], T]) -> T:
        session = self.load()
        evidence = EvidenceStore(
            self.paths.evidence_dir,
            run_dir=Path(session["evidence_dir"]),
            event_sink=self.event_sink,
        )
        evidence.bind(profile_id=session["profile_id"])
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(session["endpoint"])
            pages = [page for context in browser.contexts for page in context.pages]
            if not pages:
                raise RuntimeError("浏览器没有打开的页面")
            page = pages[-1]
            page.set_default_timeout(8000)
            return callback(page, evidence)
        finally:
            playwright.stop()


def page_status(page: Any, _: EvidenceStore | None = None) -> dict[str, Any]:
    viewport = page.viewport_size or page.evaluate(
        "() => ({width: innerWidth, height: innerHeight})"
    )
    return {
        "url": redact_url(page.url),
        "title": page.title(),
        "viewport": viewport,
        "closed": page.is_closed(),
    }
