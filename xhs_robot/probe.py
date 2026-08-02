from __future__ import annotations

import re
import time
from typing import Any, Callable

from .browser import page_status
from .evidence import EvidenceStore, redact_url


def save_snapshot(
    page: Any, evidence: EvidenceStore, stage: str, full_page: bool = False
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", stage):
        raise ValueError("stage 只能使用小写字母、数字、下划线和短横线")
    files = evidence.save_page(page, stage, full_page=full_page)
    result = {**page_status(page), "stage": stage, "files": files}
    evidence.event("snapshot", **result)
    return result


def inspect_page(page: Any, evidence: EvidenceStore, kind: str) -> Any:
    inspectors: dict[str, Callable[[Any], Any]] = {
        "interactive": inspect_interactive,
        "scroll": inspect_scroll,
        "links": inspect_links,
        "detail": inspect_detail,
        "frames": inspect_frames,
    }
    result = inspectors[kind](page)
    evidence.save_json(f"inspect_{kind}_{int(time.time() * 1000)}", result)
    evidence.event(
        "inspect", kind=kind, count=len(result) if isinstance(result, list) else None
    )
    return result


def inspect_interactive(page: Any) -> list[dict[str, Any]]:
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
            if (node.matches('a[href]')) {
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
              editable: !node.disabled && node.matches('input, textarea, [contenteditable="true"]'),
              classes: [...node.classList].slice(0, 6),
              parent_classes: node.parentElement ? [...node.parentElement.classList].slice(0, 6) : [],
              rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
            };
          });
        }
        """
    )


def inspect_scroll(page: Any) -> list[dict[str, Any]]:
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


def inspect_links(page: Any) -> list[dict[str, Any]]:
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


def inspect_detail(page: Any) -> dict[str, Any]:
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


def inspect_frames(page: Any) -> list[dict[str, Any]]:
    return [{"name": frame.name, "url": redact_url(frame.url)} for frame in page.frames]


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
    before = page_status(page)
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
        if x is None or y is None or not 0 <= x < viewport["width"] or not 0 <= y < viewport["height"]:
            raise ValueError("点击坐标必须位于当前视口内")
        page.mouse.click(x, y)
    elif action == "scroll":
        if probe_id == "window":
            size = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            page.mouse.move(size["width"] * 0.7, size["height"] * 0.6)
            page.mouse.wheel(0, delta)
        else:
            locator = locator_by_probe_id(page, probe_id)
            box = locator.bounding_box()
            if box is None:
                raise RuntimeError(f"临时元素 {probe_id} 当前没有可见边界")
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.wheel(0, delta)
    else:
        locator = locator_by_probe_id(page, probe_id)
        if action == "click":
            locator.click()
        elif action == "fill":
            locator.fill(value)
        elif action == "press":
            locator.press(value)
        elif action == "hover":
            locator.hover()

    page.wait_for_timeout(500)
    after = page_status(page)
    result = {"action": action, "probe_id": probe_id, "before": before, "after": after}
    evidence.event("action", **result)
    return result


def locator_by_probe_id(page: Any, probe_id: str | None) -> Any:
    locator = page.locator(f'[data-xhs-live-probe-id="{probe_id}"]')
    count = locator.count()
    if count != 1:
        raise RuntimeError(f"临时元素 {probe_id} 当前匹配 {count} 个节点，请重新 inspect")
    return locator
