from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .evidence import EvidenceStore


NOTE_PATH_PATTERN = re.compile(r"/(?:search_result|explore)/([0-9a-fA-F]{24})(?:/|$)")


class PageGate(StrEnum):
    READY = "ready"
    LOGIN = "login"
    HUMAN = "human"


class HumanInterventionRequired(RuntimeError):
    def __init__(self, message: str, gate: PageGate = PageGate.HUMAN) -> None:
        super().__init__(message)
        self.gate = gate


class InteractionUncertain(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NoteLink:
    note_id: str
    path: str
    text: str
    media: str


@dataclass(frozen=True, slots=True)
class VisibleComment:
    comment_id: str
    text: str
    author: str


@dataclass(frozen=True, slots=True)
class NoteContext:
    note_id: str
    text: str
    comments: tuple[VisibleComment, ...]
    media: str


def classify_page(url: str, body_text: str) -> tuple[PageGate, str | None]:
    lowered_url = url.lower()
    compact = " ".join(body_text.split())
    if any(part in lowered_url for part in ("/login", "/captcha", "/verify")):
        return PageGate.LOGIN, "登录或验证页面"
    login_markers = ("扫码登录", "登录后浏览更多", "手机号登录", "输入手机号", "小红书如何扫码")
    if any(marker in compact for marker in login_markers):
        return PageGate.LOGIN, "页面要求登录"
    human_markers = (
        "安全验证",
        "请完成验证",
        "验证码",
        "访问频繁",
        "操作频繁",
        "账号异常",
        "网络环境存在风险",
    )
    for marker in human_markers:
        if marker in compact:
            return PageGate.HUMAN, marker
    return PageGate.READY, None


class XhsPage:
    def __init__(self, page: Any, evidence: EvidenceStore) -> None:
        self.page = page
        self.evidence = evidence

    def check_gate(self) -> tuple[PageGate, str | None]:
        text = self.page.locator("body").inner_text(timeout=5000)[:20000]
        gate, reason = classify_page(self.page.url, text)
        self.evidence.event("page_gate", gate=gate.value, reason=reason, url=self.page.url)
        return gate, reason

    def require_ready(self) -> None:
        gate, reason = self.check_gate()
        if gate == PageGate.LOGIN:
            raise HumanInterventionRequired(reason or "请先人工登录", gate)
        if gate == PageGate.HUMAN:
            raise HumanInterventionRequired(reason or "页面要求人工接管", gate)

    def search(self, keyword: str) -> list[NoteLink]:
        self.require_ready()
        before = self.page.evaluate(
            """
            () => ({
              noteIds: [...document.querySelectorAll('a[href]')].map((node) => {
                try {
                  return new URL(node.href, document.baseURI).pathname.match(/\\/search_result\\/([0-9a-fA-F]{24})(?:\\/|$)/)?.[1] || null;
                } catch (_) { return null; }
              }).filter(Boolean)
            })
            """
        )
        candidate = self.page.evaluate(
            """
            () => {
              window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 15;
              };
              const hit = (node) => {
                const rect = node.getBoundingClientRect();
                const x = Math.min(innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
                const y = Math.min(innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
                const target = document.elementFromPoint(x, y);
                return target && (target === node || node.contains(target) || target.contains(node));
              };
              const candidates = [...document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]')]
                .filter((node) => visible(node) && hit(node) && !node.disabled)
                .map((node) => {
                  const rect = node.getBoundingClientRect();
                  const semantic = [node.placeholder, node.getAttribute('aria-label'), node.title].filter(Boolean).join(' ');
                  let score = /搜索|search/i.test(semantic) ? 100 : 0;
                  if (rect.top < innerHeight * 0.35) score += 20;
                  if (node.tagName === 'TEXTAREA' || node.tagName === 'INPUT') score += 10;
                  return {node, score, rect};
                })
                .sort((a, b) => b.score - a.score);
              if (!candidates.length || candidates[0].score < 20) return null;
              const node = candidates[0].node;
              let id = node.getAttribute('data-xhs-live-probe-id');
              if (!id) {
                id = `node-${++window.__xhsProbeSerial}`;
                node.setAttribute('data-xhs-live-probe-id', id);
              }
              return {id, score: candidates[0].score};
            }
            """
        )
        if not candidate:
            raise InteractionUncertain("没有找到当前可见且命中的搜索输入框")
        editor = self._probe_locator(candidate["id"])
        editor.click()
        editor.fill(keyword)
        actual = editor.input_value() if editor.evaluate("node => 'value' in node") else editor.inner_text()
        if actual.strip() != keyword:
            raise InteractionUncertain("搜索关键词填入后读取值不一致")
        editor.press("Enter")
        self.evidence.event("search_submitted", keyword=keyword, url=self.page.url)
        try:
            self.page.wait_for_function(
                """
                (before) => {
                  const currentIds = [...document.querySelectorAll('a[href]')].map((node) => {
                    try {
                      return new URL(node.href, document.baseURI).pathname.match(/\\/search_result\\/([0-9a-fA-F]{24})(?:\\/|$)/)?.[1] || null;
                    } catch (_) { return null; }
                  }).filter(Boolean);
                  return location.pathname.startsWith('/search_result')
                    && currentIds.some((id) => !before.noteIds.includes(id));
                }
                """,
                arg=before,
                timeout=30000,
            )
        except Exception as exc:
            self.require_ready()
            raise InteractionUncertain(
                "搜索页已打开，但等待 30 秒仍未加载出笔记结果"
            ) from exc
        notes = self.collect_note_links()
        if not notes:
            raise InteractionUncertain("搜索页已打开，但没有识别到可处理的笔记")
        self.evidence.event(
            "search_complete", keyword=keyword, result_count=len(notes), url=self.page.url
        )
        self._capture("search-complete")
        return notes

    def collect_note_links(self) -> list[NoteLink]:
        raw_links = self.page.evaluate(
            """
            () => [...document.querySelectorAll('a[href]')].map((node) => {
              let path = '';
              try { path = new URL(node.href, document.baseURI).pathname; } catch (_) {}
              return {
                path,
                text: (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 300),
                images: node.querySelectorAll('img').length,
                videos: node.querySelectorAll('video').length
              };
            })
            """
        )
        notes: list[NoteLink] = []
        seen: set[str] = set()
        search_results_only = self.page.evaluate(
            "() => location.pathname.startsWith('/search_result')"
        )
        for link in raw_links:
            if search_results_only and not link["path"].startswith("/search_result/"):
                continue
            match = NOTE_PATH_PATTERN.search(link["path"])
            if not match or match.group(1) in seen:
                continue
            note_id = match.group(1).lower()
            seen.add(note_id)
            media = "video" if link["videos"] else "image" if link["images"] else "unknown"
            notes.append(NoteLink(note_id, link["path"], link["text"], media))
        self.evidence.event("notes_observed", count=len(notes), note_ids=[item.note_id for item in notes])
        return notes

    def scroll_results(self, delta: int = 720) -> dict[str, int | bool]:
        before = self.page.evaluate(
            "() => ({top: Math.round(document.scrollingElement.scrollTop), height: Math.round(document.scrollingElement.scrollHeight), client: Math.round(document.scrollingElement.clientHeight)})"
        )
        size = self.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        self.page.mouse.move(size["width"] * 0.7, size["height"] * 0.6)
        self.page.mouse.wheel(0, delta)
        self.page.wait_for_timeout(1000)
        after = self.page.evaluate(
            "() => ({top: Math.round(document.scrollingElement.scrollTop), height: Math.round(document.scrollingElement.scrollHeight), client: Math.round(document.scrollingElement.clientHeight)})"
        )
        result = {
            "before_top": before["top"],
            "after_top": after["top"],
            "height": after["height"],
            "moved": after["top"] != before["top"],
            "at_bottom": after["top"] + after["client"] >= after["height"] - 4,
        }
        self.evidence.event("results_scrolled", **result)
        self.require_ready()
        return result

    def open_note(self, note_id: str) -> None:
        self.require_ready()
        candidates = self.page.locator(
            f'a[href*="/search_result/{note_id}"], a[href*="/explore/{note_id}"]'
        )
        target = None
        for index in range(candidates.count()):
            item = candidates.nth(index)
            if item.is_visible():
                target = item
                break
        if target is None:
            raise InteractionUncertain(f"笔记 {note_id} 已离开当前虚拟列表")
        before_path = self.page.evaluate("() => location.pathname")
        target.click()
        try:
            self.page.wait_for_function(
                """
                (id) => {
                  const mask = document.querySelector('.note-detail-mask');
                  if (location.pathname.includes(id)) return true;
                  if (!mask) return false;
                  const style = getComputedStyle(mask);
                  const rect = mask.getBoundingClientRect();
                  return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
                }
                """,
                arg=note_id,
                timeout=12000,
            )
        except Exception as exc:
            self.require_ready()
            raise InteractionUncertain(f"点击笔记 {note_id} 后未确认详情打开") from exc
        self.evidence.event("note_opened", note_id=note_id, before_path=before_path, url=self.page.url)
        self._capture("note-opened")

    def read_note_context(self, note_id: str) -> NoteContext:
        self.require_ready()
        raw = self.page.evaluate(
            """
            () => {
              window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
              };
              const root = [...document.querySelectorAll('.note-detail-mask')].find(visible) || document.body;
              const commentSelectors = '.comment-item, [class*="comment-item"], [class*="commentItem"]';
              const comments = [...root.querySelectorAll(commentSelectors)].filter(visible).slice(0, 100).map((node) => {
                let id = node.getAttribute('data-xhs-comment-id');
                if (!id) {
                  id = `comment-${++window.__xhsProbeSerial}`;
                  node.setAttribute('data-xhs-comment-id', id);
                }
                const authorNode = node.querySelector('[class*="author"], [class*="name"], a[href*="/user/profile"]');
                const contentNode = node.querySelector('[class*="content"], [class*="text"]');
                return {
                  id,
                  author: (authorNode?.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
                  text: (contentNode?.innerText || node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 500)
                };
              }).filter((item) => item.text);
              return {
                text: (root.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 6000),
                comments,
                videos: [...root.querySelectorAll('video')].filter(visible).length,
                images: [...root.querySelectorAll('img')].filter(visible).length
              };
            }
            """
        )
        if not raw["text"]:
            raise InteractionUncertain("详情打开后没有读取到可见正文")
        comments: list[VisibleComment] = []
        seen: set[str] = set()
        for item in raw["comments"]:
            fingerprint = f"{item['author']}\0{item['text']}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            comments.append(VisibleComment(item["id"], item["text"], item["author"]))
        media = "video" if raw["videos"] else "image" if raw["images"] else "unknown"
        context = NoteContext(note_id, raw["text"], tuple(comments), media)
        self.evidence.event(
            "note_context", note_id=note_id, media=media, comment_count=len(comments)
        )
        return context

    def submit_comment(self, text: str) -> None:
        self._submit_text(text, kind="comment")

    def activate_reply(self, comment: VisibleComment) -> None:
        container = self.page.locator(f'[data-xhs-comment-id="{comment.comment_id}"]')
        if container.count() != 1 or not container.is_visible():
            raise InteractionUncertain("目标评论已离开当前可见 DOM")
        container.scroll_into_view_if_needed()
        self.page.wait_for_timeout(200)
        target_id = container.evaluate(
            """
            (node) => {
              window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
              const visible = (item) => {
                const style = getComputedStyle(item);
                const rect = item.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
              };
              const hit = (item) => {
                const rect = item.getBoundingClientRect();
                const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return target && (target === item || item.contains(target) || target.contains(item));
              };
              const candidates = [...node.querySelectorAll('button, [role="button"], .reply, [class*="reply"]')].filter((item) => visible(item) && hit(item));
              const target = candidates.find((item) => /回复/.test(item.innerText || item.getAttribute('aria-label') || ''))
                || candidates.find((item) => item.matches('.reply, [class*="reply"]'));
              if (!target) return null;
              let id = target.getAttribute('data-xhs-live-probe-id');
              if (!id) {
                id = `node-${++window.__xhsProbeSerial}`;
                target.setAttribute('data-xhs-live-probe-id', id);
              }
              return id;
            }
            """
        )
        if not target_id:
            raise InteractionUncertain("目标评论中没有找到回复入口")
        self._probe_locator(target_id).click()
        self.page.wait_for_timeout(500)
        self.evidence.event("reply_activated", comment_id=comment.comment_id)

    def submit_reply(self, text: str) -> None:
        self._submit_text(text, kind="reply")

    def _submit_text(self, text: str, kind: str) -> None:
        self.require_ready()
        try:
            editor_id = self._discover_editor()
            editor_id = self._activate_editor(editor_id)
            editor = self._probe_locator(editor_id)
            editor.fill(text)
            actual = editor.evaluate(
                "node => 'value' in node ? node.value : node.innerText"
            )
            if actual.strip() != text:
                raise InteractionUncertain("写入输入区后读取内容不一致")
            submit_id = self._discover_submit(editor_id)
            self._probe_locator(submit_id).click()
        except InteractionUncertain:
            raise
        except Exception as exc:
            raise InteractionUncertain(
                "评论输入或提交控件操作失败"
            ) from exc
        self.evidence.event("write_dispatched", kind=kind, text=text)

    def _discover_editor(self) -> str:
        result = self.page.evaluate(
            """
            () => {
              window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 20 && rect.height > 15;
              };
              const hit = (node) => {
                const rect = node.getBoundingClientRect();
                const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                if (!target) return false;
                if (target === node || node.contains(target) || target.contains(node)) return true;
                const inputBox = node.closest('.input-box');
                return inputBox ? inputBox.contains(target) : false;
              };
              const root = [...document.querySelectorAll('.note-detail-mask, [role="dialog"]')].find(visible) || document.body;
              const candidates = [...root.querySelectorAll('p.content-input, textarea, [contenteditable="true"], [role="textbox"]')]
                .filter((node) => visible(node) && hit(node) && !node.disabled)
                .map((node) => {
                  const semantic = [node.placeholder, node.getAttribute('aria-label'), node.innerText].filter(Boolean).join(' ');
                  let score = /评论|回复|说点什么|输入/.test(semantic) ? 100 : 0;
                  if (node.matches('p.content-input')) score += 50;
                  if (node.closest('.note-detail-mask, [role="dialog"]')) score += 30;
                  return {node, score};
                }).sort((a, b) => b.score - a.score);
              if (!candidates.length || candidates[0].score < 30) return null;
              const node = candidates[0].node;
              let id = node.getAttribute('data-xhs-live-probe-id');
              if (!id) {
                id = `node-${++window.__xhsProbeSerial}`;
                node.setAttribute('data-xhs-live-probe-id', id);
              }
              return id;
            }
            """
        )
        if not result:
            raise InteractionUncertain("没有找到详情中的可见评论输入区")
        return result

    def _activate_editor(self, editor_id: str) -> str:
        point = self.page.evaluate(
            """
            (editorId) => {
              const editor = document.querySelector(`[data-xhs-live-probe-id="${editorId}"]`);
              if (!editor) return null;
              const rect = editor.getBoundingClientRect();
              const points = [
                [rect.left + rect.width / 2, rect.top + rect.height / 2],
                [rect.left + Math.min(24, rect.width / 3), rect.top + rect.height / 2]
              ];
              const inputBox = editor.closest('.input-box');
              for (const [x, y] of points) {
                const target = document.elementFromPoint(x, y);
                const accepted = target && (
                  target === editor || editor.contains(target) || target.contains(editor)
                  || (inputBox && inputBox.contains(target))
                );
                if (accepted) return {x, y};
              }
              return null;
            }
            """,
            editor_id,
        )
        if point is None:
            raise InteractionUncertain("评论输入区当前被无关页面元素遮挡")
        self.page.mouse.click(point["x"], point["y"])
        self.page.wait_for_timeout(300)
        active_editor_id = self._discover_editor()
        if not self._probe_locator(active_editor_id).is_editable():
            raise InteractionUncertain("点击后评论输入区未进入可编辑状态")
        return active_editor_id

    def _discover_submit(self, editor_id: str) -> str:
        result = self.page.evaluate(
            """
            (editorId) => {
              window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
              const editor = document.querySelector(`[data-xhs-live-probe-id="${editorId}"]`);
              if (!editor) return null;
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2 && !node.disabled;
              };
              const hit = (node) => {
                const rect = node.getBoundingClientRect();
                const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return target && (target === node || node.contains(target) || target.contains(node));
              };
              const root = editor.closest('.note-detail-mask, [role="dialog"]') || document.body;
              const candidates = [...root.querySelectorAll('button, [role="button"], .btn.submit, [class*="submit"]')]
                .filter((node) => visible(node) && hit(node))
                .map((node) => {
                  const semantic = [node.innerText, node.getAttribute('aria-label'), node.title].filter(Boolean).join(' ');
                  let score = /发布|发送|提交/.test(semantic) ? 100 : 0;
                  if (node.matches('.btn.submit, [class*="submit"]')) score += 40;
                  const editorRect = editor.getBoundingClientRect();
                  const rect = node.getBoundingClientRect();
                  if (Math.abs(rect.top - editorRect.top) < 160) score += 20;
                  return {node, score};
                }).sort((a, b) => b.score - a.score);
              if (!candidates.length || candidates[0].score < 40) return null;
              const node = candidates[0].node;
              let id = node.getAttribute('data-xhs-live-probe-id');
              if (!id) {
                id = `node-${++window.__xhsProbeSerial}`;
                node.setAttribute('data-xhs-live-probe-id', id);
              }
              return id;
            }
            """,
            editor_id,
        )
        if not result:
            raise InteractionUncertain("输入完成后没有找到可见提交控件")
        return result

    def close_detail(self, note_id: str) -> None:
        before_path = self.page.evaluate("() => location.pathname")
        action = self.page.evaluate(
            """
            () => {
              window.__xhsProbeSerial = window.__xhsProbeSerial || 0;
              const visible = (node) => {
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 2 && rect.height > 2;
              };
              const hit = (node) => {
                const rect = node.getBoundingClientRect();
                const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return target && (target === node || node.contains(target) || target.contains(node));
              };
              const detail = [...document.querySelectorAll('.note-detail-mask, [role="dialog"]')].find(visible);
              if (!detail) return null;
              const close = [...detail.querySelectorAll('button, [role="button"], [class*="close"]')]
                .filter((node) => visible(node) && hit(node))
                .find((node) => /关闭|close/i.test([node.innerText, node.getAttribute('aria-label'), node.title, node.className].join(' ')));
              if (close) {
                let id = close.getAttribute('data-xhs-live-probe-id');
                if (!id) {
                  id = `node-${++window.__xhsProbeSerial}`;
                  close.setAttribute('data-xhs-live-probe-id', id);
                }
                return {kind: 'control', id};
              }
              if (!detail.matches('.note-detail-mask')) return null;
              const rect = detail.getBoundingClientRect();
              const points = [
                [rect.left + 12, rect.top + 12],
                [rect.right - 12, rect.top + 12],
                [rect.left + 12, rect.bottom - 12],
                [rect.right - 12, rect.bottom - 12]
              ];
              for (const [x, y] of points) {
                const hit = document.elementFromPoint(x, y);
                if (hit === detail) {
                  return {kind: 'mask', x, y};
                }
              }
              return null;
            }
            """
        )
        if action and action["kind"] == "control":
            self._probe_locator(action["id"]).click()
        elif action and action["kind"] == "mask":
            self.page.mouse.click(action["x"], action["y"])
        elif action is None:
            self.page.go_back(wait_until="domcontentloaded", timeout=15000)
        try:
            self.page.wait_for_function(
                """({id, path}) => !location.pathname.includes(id) && location.pathname !== path || !document.querySelector('.note-detail-mask, [role="dialog"]')""",
                arg={"id": note_id, "path": before_path},
                timeout=10000,
            )
        except Exception as exc:
            raise InteractionUncertain("关闭详情后未确认搜索列表恢复") from exc
        self.evidence.event("note_closed", note_id=note_id, method=action and action["kind"] or "back")
        self._capture("note-closed")

    def _probe_locator(self, probe_id: str) -> Any:
        locator = self.page.locator(f'[data-xhs-live-probe-id="{probe_id}"]')
        if locator.count() != 1 or not locator.is_visible():
            raise InteractionUncertain(f"临时元素 {probe_id} 已失效，请重新观察页面")
        return locator

    def _capture(self, prefix: str) -> None:
        name = f"{prefix}-{int(time.time() * 1000)}"
        try:
            self.evidence.save_viewport(name, self.page)
        except Exception as exc:
            self.evidence.event(
                "capture_failed", stage=name, error=type(exc).__name__
            )
