from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

from evidence import EvidenceStore, redact_url


EXPLORE_URL = "https://www.xiaohongshu.com/explore"
LOCAL_ROOT = Path(".xhs-probe")
CURRENT_SESSION = LOCAL_ROOT / "current.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="小红书实时页面探针：保留浏览器现场，由 agent 逐步观察和操作"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="启动独立可见 Chromium；命令退出后浏览器继续运行")
    start.add_argument("--profile-dir", type=Path, default=LOCAL_ROOT / "live-profile")
    start.add_argument("--cdp-port", type=int, default=9229)

    subparsers.add_parser("status", help="读取当前页面状态，不关闭浏览器")

    snapshot = subparsers.add_parser("snapshot", help="保存当前截图、脱敏 HTML 和文本摘要")
    snapshot.add_argument("--stage", required=True)
    snapshot.add_argument("--full-page", action="store_true", help="基线专用；可能触发页面滚动")

    inspect = subparsers.add_parser("inspect", help="读取当前页面结构，并为可操作元素分配临时 ID")
    inspect.add_argument(
        "kind",
        choices=("interactive", "scroll", "links", "detail", "frames"),
    )

    act = subparsers.add_parser("act", help="只操作刚刚观察到的临时元素 ID")
    act.add_argument(
        "action",
        choices=("click", "click-point", "fill", "press", "scroll", "hover", "back", "wait"),
    )
    act.add_argument("--id", dest="probe_id")
    act.add_argument("--value")
    act.add_argument("--delta", type=int, default=700)
    act.add_argument("--x", type=float)
    act.add_argument("--y", type=float)

    return parser


def start_browser(profile_dir: Path, cdp_port: int) -> dict[str, Any]:
    if not 1024 <= cdp_port <= 65535:
        raise ValueError("CDP 端口必须在 1024 到 65535 之间")
    endpoint = f"http://127.0.0.1:{cdp_port}"
    if _endpoint_ready(endpoint):
        raise RuntimeError(f"端口 {cdp_port} 已有浏览器在运行")

    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    evidence = EvidenceStore(LOCAL_ROOT / "evidence")

    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
    if not executable.exists():
        raise RuntimeError("Playwright Chromium 可执行文件不存在")

    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,900",
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
        if _endpoint_ready(endpoint):
            break
        time.sleep(0.25)
    else:
        raise TimeoutError("等待 Chromium 调试端口超时")

    session = {
        "endpoint": endpoint,
        "cdp_port": cdp_port,
        "browser_pid": process.pid,
        "profile_dir": str(profile_dir),
        "evidence_dir": str(evidence.run_dir.resolve()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    CURRENT_SESSION.write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    evidence.event("browser_started", pid=process.pid, url=EXPLORE_URL)
    return session


def _endpoint_ready(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/json/version", timeout=0.5) as response:
            return response.status == 200
    except (URLError, TimeoutError, OSError):
        return False


def load_session() -> dict[str, Any]:
    if not CURRENT_SESSION.exists():
        raise RuntimeError("没有当前探针会话，请先运行 `python app.py start`")
    session = json.loads(CURRENT_SESSION.read_text(encoding="utf-8"))
    if not _endpoint_ready(session["endpoint"]):
        raise RuntimeError("当前浏览器调试端口不可用；浏览器可能已关闭")
    return session


def with_page(callback: Callable[[Any, EvidenceStore], Any]) -> Any:
    session = load_session()
    evidence = EvidenceStore(
        LOCAL_ROOT / "evidence", run_dir=Path(session["evidence_dir"])
    )
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(session["endpoint"])
        if not browser.contexts:
            raise RuntimeError("浏览器没有可访问的上下文")
        pages = [page for context in browser.contexts for page in context.pages]
        if not pages:
            raise RuntimeError("浏览器没有打开的页面")
        page = pages[-1]
        page.set_default_timeout(8000)
        return callback(page, evidence)
    finally:
        # This drops only the CDP client connection. The independently launched browser remains open.
        playwright.stop()


def page_status(page: Any, _: EvidenceStore) -> dict[str, Any]:
    return {
        "url": redact_url(page.url),
        "title": page.title(),
        "viewport": page.viewport_size,
        "closed": page.is_closed(),
    }


def save_snapshot(
    page: Any, evidence: EvidenceStore, stage: str, full_page: bool = False
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", stage):
        raise ValueError("stage 只能使用小写字母、数字、下划线和短横线")
    files = evidence.save_page(page, stage, full_page=full_page)
    result = {**page_status(page, evidence), "stage": stage, "files": files}
    evidence.event("snapshot", **result)
    return result


def inspect_page(page: Any, evidence: EvidenceStore, kind: str) -> Any:
    inspectors = {
        "interactive": _inspect_interactive,
        "scroll": _inspect_scroll,
        "links": _inspect_links,
        "detail": _inspect_detail,
        "frames": _inspect_frames,
    }
    result = inspectors[kind](page)
    evidence.save_json(f"inspect_{kind}_{int(time.time() * 1000)}", result)
    evidence.event("inspect", kind=kind, count=len(result) if isinstance(result, list) else None)
    return result


def _inspect_interactive(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
          window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
          const visible = (node) => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
          };
          const semantic = 'a, button, input, textarea, [contenteditable="true"], [role="button"], [role="tab"], [role="textbox"], [tabindex]';
          const nodes = [...document.querySelectorAll('body *')]
            .filter(visible)
            .filter((node) => node.matches(semantic) || getComputedStyle(node).cursor === 'pointer')
            .filter((node) => {
              const rect = node.getBoundingClientRect();
              const x = Math.min(innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
              const y = Math.min(innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
              const hit = document.elementFromPoint(x, y);
              return hit && (hit === node || node.contains(hit) || hit.contains(node));
            });
          return nodes.slice(0, 800).map((node) => {
            let id = node.getAttribute('data-xhs-live-probe-id');
            if (!id) {
              id = `node-${++window.__xhsProbeSerial}`;
              node.setAttribute('data-xhs-live-probe-id', id);
            }
            const rect = node.getBoundingClientRect();
            let href = null;
            if (node.href) {
              try { href = new URL(node.href, document.baseURI).pathname; } catch (_) {}
            }
            const label = node.labels && node.labels.length
              ? [...node.labels].map((item) => item.innerText || '').join(' ').trim()
              : null;
            return {
              probe_id: id,
              tag: node.tagName.toLowerCase(),
              type: node.getAttribute('type'),
              role: node.getAttribute('role'),
              text: (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
              placeholder: node.getAttribute('placeholder'),
              aria_label: node.getAttribute('aria-label'),
              title: node.getAttribute('title'),
              label,
              href,
              editable: !node.disabled && (node.matches('input, textarea, [contenteditable="true"]')),
              classes: [...node.classList].slice(0, 6),
              parent_classes: node.parentElement ? [...node.parentElement.classList].slice(0, 6) : [],
              rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
            };
          });
        }
        """
    )


def _inspect_scroll(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
          window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
          const visible = (node) => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 20;
          };
          const nodes = [document.scrollingElement, ...document.querySelectorAll('body *')];
          return nodes.filter((node, index) => {
            if (!node || (index && !visible(node))) return false;
            const style = getComputedStyle(node);
            return index === 0 || (node.scrollHeight > node.clientHeight + 4 && /(auto|scroll)/.test(style.overflowY));
          }).slice(0, 100).map((node, index) => {
            let id = index === 0 ? 'window' : node.getAttribute('data-xhs-live-probe-id');
            if (!id) {
              id = `node-${++window.__xhsProbeSerial}`;
              node.setAttribute('data-xhs-live-probe-id', id);
            }
            const rect = index === 0 ? {x: 0, y: 0, width: innerWidth, height: innerHeight} : node.getBoundingClientRect();
            return {
              probe_id: id,
              tag: node.tagName.toLowerCase(),
              role: node.getAttribute('role'),
              id: node.id || null,
              classes: [...node.classList].slice(0, 6),
              scroll_top: Math.round(node.scrollTop),
              scroll_height: Math.round(node.scrollHeight),
              client_height: Math.round(node.clientHeight),
              at_bottom: node.scrollTop + node.clientHeight >= node.scrollHeight - 4,
              descendant_links: node.querySelectorAll('a[href]').length,
              rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
            };
          });
        }
        """
    )


def _inspect_links(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
          window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
          const visible = (node) => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
          };
          return [...document.querySelectorAll('a[href]')].filter(visible).slice(0, 500).map((node) => {
            let id = node.getAttribute('data-xhs-live-probe-id');
            if (!id) {
              id = `node-${++window.__xhsProbeSerial}`;
              node.setAttribute('data-xhs-live-probe-id', id);
            }
            let path = null;
            try { path = new URL(node.href, document.baseURI).pathname; } catch (_) {}
            const rect = node.getBoundingClientRect();
            return {
              probe_id: id,
              path,
              text: (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 160),
              images: node.querySelectorAll('img').length,
              videos: node.querySelectorAll('video').length,
              rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
            };
          });
        }
        """
    )


def _inspect_detail(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const visible = (node) => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
          };
          const largeImages = [...document.images].filter((node) => {
            const rect = node.getBoundingClientRect();
            return visible(node) && rect.width >= 200 && rect.height >= 150;
          });
          return {
            url_path: location.pathname,
            title: document.title,
            dialogs: [...document.querySelectorAll('[role="dialog"]')].filter(visible).length,
            visible_videos: [...document.querySelectorAll('video')].filter(visible).length,
            visible_large_images: largeImages.length,
            textareas: [...document.querySelectorAll('textarea')].filter(visible).length,
            editable_nodes: [...document.querySelectorAll('input, textarea, [contenteditable="true"]')].filter(visible).length,
            visible_buttons: [...document.querySelectorAll('button, [role="button"]')].filter(visible).length
          };
        }
        """
    )


def _inspect_frames(page: Any) -> list[dict[str, Any]]:
    return [
        {"name": frame.name, "url": redact_url(frame.url)}
        for frame in page.frames
    ]


def perform_action(
    page: Any,
    evidence: EvidenceStore,
    action: str,
    probe_id: str | None,
    value: str | None,
    delta: int,
    x: float | None,
    y: float | None,
) -> dict[str, Any]:
    before = page_status(page, evidence)
    if action in {"click", "fill", "press", "scroll", "hover"} and not probe_id:
        raise ValueError(f"{action} 需要 --id")
    if action in {"fill", "press"} and value is None:
        raise ValueError(f"{action} 需要 --value")
    if action == "click-point" and (x is None or y is None):
        raise ValueError("click-point 需要 --x 和 --y")

    if action == "back":
        page.go_back(wait_until="domcontentloaded", timeout=15000)
    elif action == "wait":
        seconds = float(value or "1")
        if not 0 <= seconds <= 15:
            raise ValueError("单次等待必须在 0 到 15 秒之间")
        page.wait_for_timeout(int(seconds * 1000))
    elif action == "click-point":
        viewport = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        if not 0 <= x < viewport["width"] or not 0 <= y < viewport["height"]:
            raise ValueError("点击坐标必须位于当前视口内")
        page.mouse.click(x, y)
    elif action == "scroll":
        if probe_id == "window":
            size = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            page.mouse.move(size["width"] * 0.7, size["height"] * 0.6)
            page.mouse.wheel(0, delta)
        else:
            locator = _locator_by_probe_id(page, probe_id)
            box = locator.bounding_box()
            if box is None:
                raise RuntimeError(f"临时元素 {probe_id} 当前没有可见边界")
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.wheel(0, delta)
    else:
        locator = _locator_by_probe_id(page, probe_id)
        if action == "click":
            locator.click()
        elif action == "fill":
            locator.fill(value)
        elif action == "press":
            locator.press(value)
        elif action == "hover":
            locator.hover()

    page.wait_for_timeout(500)
    after = page_status(page, evidence)
    result = {"action": action, "probe_id": probe_id, "before": before, "after": after}
    evidence.event("action", **result)
    return result


def _locator_by_probe_id(page: Any, probe_id: str | None) -> Any:
    locator = page.locator(f'[data-xhs-live-probe-id="{probe_id}"]')
    count = locator.count()
    if count != 1:
        raise RuntimeError(f"临时元素 {probe_id} 当前匹配 {count} 个节点，请重新 inspect")
    return locator


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args()
    try:
        if args.command == "start":
            print_json(start_browser(args.profile_dir, args.cdp_port))
        elif args.command == "status":
            print_json(with_page(page_status))
        elif args.command == "snapshot":
            print_json(
                with_page(
                    lambda page, evidence: save_snapshot(
                        page, evidence, args.stage, full_page=args.full_page
                    )
                )
            )
        elif args.command == "inspect":
            print_json(with_page(lambda page, evidence: inspect_page(page, evidence, args.kind)))
        elif args.command == "act":
            print_json(
                with_page(
                    lambda page, evidence: perform_action(
                        page,
                        evidence,
                        args.action,
                        args.probe_id,
                        args.value,
                        args.delta,
                        args.x,
                        args.y,
                    )
                )
            )
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
