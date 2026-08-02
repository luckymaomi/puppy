from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

from .evidence import EvidenceStore, redact_url
from .paths import AppPaths


EXPLORE_URL = "https://www.xiaohongshu.com/explore"
T = TypeVar("T")


def endpoint_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/json/version", timeout=0.5) as response:
            return response.status == 200
    except (URLError, TimeoutError, OSError):
        return False


class BrowserSession:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def start(self, profile_dir: Path | None = None, cdp_port: int = 9229) -> dict[str, Any]:
        if not 1024 <= cdp_port <= 65535:
            raise ValueError("CDP 端口必须在 1024 到 65535 之间")
        endpoint = f"http://127.0.0.1:{cdp_port}"
        if endpoint_ready(endpoint):
            if self.paths.session_file.is_file():
                session = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
                if session.get("endpoint") == endpoint:
                    return {**session, "already_running": True}
            raise RuntimeError(f"端口 {cdp_port} 已有浏览器在运行")

        self.paths.ensure()
        resolved_profile = (profile_dir or self.paths.profile_dir).resolve()
        resolved_profile.mkdir(parents=True, exist_ok=True)
        evidence = EvidenceStore(self.paths.evidence_dir)

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
                f"--user-data-dir={resolved_profile}",
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
            "profile_dir": str(resolved_profile),
            "evidence_dir": str(evidence.run_dir.resolve()),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "already_running": False,
        }
        self.paths.session_file.write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        evidence.event("browser_started", pid=process.pid, url=EXPLORE_URL)
        return session

    def load(self) -> dict[str, Any]:
        if not self.paths.session_file.exists():
            raise RuntimeError("没有当前浏览器会话，请先运行 `python app.py start`")
        session = json.loads(self.paths.session_file.read_text(encoding="utf-8"))
        endpoint = session.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint_ready(endpoint):
            raise RuntimeError("当前浏览器调试端口不可用；浏览器可能已关闭")
        return session

    def with_page(self, callback: Callable[[Any, EvidenceStore], T]) -> T:
        session = self.load()
        evidence = EvidenceStore(
            self.paths.evidence_dir, run_dir=Path(session["evidence_dir"])
        )
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
