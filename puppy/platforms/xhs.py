from __future__ import annotations

import re
import time
from typing import Any

from ..evidence import EvidenceStore, redact_url
from ..observations import CommentObservation, Observation
from .base import AdvanceResult, HumanInterventionRequired, PageInteractionError, ResourceLink


NOTE_PATTERN = re.compile(r"/(?:explore|search_result)/([0-9a-fA-F]{24})(?:/|$)")
SECURITY_MARKERS = (
    "安全验证",
    "请完成验证",
    "访问频繁",
    "操作频繁",
    "网络环境存在风险",
)


class XiaohongshuAdapter:
    platform = "xiaohongshu"

    def __init__(self, page: Any, evidence: EvidenceStore, *, comments_limit: int) -> None:
        self.page = page
        self.list_page = page
        self.evidence = evidence
        self.comments_limit = comments_limit
        self._capture_serial = 0

    def prepare(self, keyword: str, resource_type: str) -> None:
        del resource_type
        if "xiaohongshu.com" not in self.page.url:
            self.page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)
        self.guard()
        if keyword:
            self._search(keyword)
        self.evidence.event("source_ready", platform=self.platform, keyword=keyword, url=self.page.url)

    def guard(self) -> None:
        body = self.page.locator("body").inner_text(timeout=5000)[:20000]
        marker = next((item for item in SECURITY_MARKERS if item in body), None)
        if marker:
            raise HumanInterventionRequired(marker)
        modal = self.page.locator(".reds-modal.login-modal:visible")
        if modal.count() == 0:
            return
        close = modal.first.locator(".icon-btn-wrapper.close-button:visible, .close-button:visible").first
        if close.count() == 0:
            self._capture("login-blocked")
            raise HumanInterventionRequired("小红书登录窗口没有可验证的关闭控件")
        self._capture("login-before-close")
        close.click()
        try:
            modal.first.wait_for(state="hidden", timeout=3000)
        except Exception as exc:
            raise HumanInterventionRequired("小红书登录窗口关闭后仍阻断页面") from exc
        self.evidence.event("login_prompt_closed", platform=self.platform, url=self.page.url)
        self._capture("login-after-close")

    def discover(self) -> list[ResourceLink]:
        self.guard()
        raw = self.page.evaluate(
            r"""
            () => [...document.querySelectorAll('a[href]')].map((node) => {
              let path = '';
              try { path = new URL(node.href, document.baseURI).pathname; } catch (_) {}
              return {
                path,
                title: (node.innerText || node.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 240)
              };
            })
            """
        )
        seen: set[str] = set()
        links: list[ResourceLink] = []
        for item in raw:
            match = NOTE_PATTERN.search(item["path"])
            if not match:
                continue
            resource_id = match.group(1).lower()
            if resource_id in seen:
                continue
            seen.add(resource_id)
            links.append(ResourceLink(resource_id, "note", item["path"], item["title"]))
        self.evidence.event(
            "resources_observed",
            platform=self.platform,
            count=len(links),
            resource_ids=[item.resource_id for item in links],
        )
        return links

    def advance(self) -> AdvanceResult:
        self.guard()
        before = self.page.evaluate(
            "() => ({top: document.scrollingElement.scrollTop, height: document.scrollingElement.scrollHeight, client: document.scrollingElement.clientHeight})"
        )
        size = self.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        self.page.mouse.move(size["width"] * 0.72, size["height"] * 0.72)
        self.page.mouse.wheel(0, 702)
        self.page.wait_for_timeout(900)
        self.guard()
        after = self.page.evaluate(
            "() => ({top: document.scrollingElement.scrollTop, height: document.scrollingElement.scrollHeight, client: document.scrollingElement.clientHeight})"
        )
        moved = after["top"] != before["top"] or after["height"] != before["height"]
        self.evidence.event(
            "source_advanced",
            platform=self.platform,
            before_top=round(before["top"]),
            after_top=round(after["top"]),
            moved=moved,
        )
        return AdvanceResult(moved=moved)

    def open(self, link: ResourceLink) -> None:
        self.guard()
        candidates = self.page.locator(
            f'a[href*="/explore/{link.resource_id}"], a[href*="/search_result/{link.resource_id}"]'
        )
        target = next(
            (candidates.nth(index) for index in range(candidates.count()) if candidates.nth(index).is_visible()),
            None,
        )
        if target is None:
            raise PageInteractionError(f"笔记 {link.resource_id} 已离开当前虚拟列表")
        target.click()
        try:
            self.page.wait_for_function(
                "(id) => location.pathname.includes(id) || !!document.querySelector('.note-detail-mask')",
                arg=link.resource_id,
                timeout=12000,
            )
        except Exception as exc:
            self.guard()
            raise PageInteractionError(f"点击笔记 {link.resource_id} 后未确认详情打开") from exc
        self.page.wait_for_timeout(700)
        self.guard()
        self.evidence.event("resource_opened", platform=self.platform, resource_id=link.resource_id, url=self.page.url)

    def observe(self, link: ResourceLink) -> Observation:
        comments: dict[str, CommentObservation] = {}
        limitations: list[str] = []
        stagnant = 0
        previous_count = -1
        rounds = 0
        while self.comments_limit and len(comments) < self.comments_limit and stagnant < 2 and rounds < 12:
            self.guard()
            snapshot = self._read_detail()
            for item in snapshot["comments"]:
                key = f"{item['author']}\0{item['text']}"
                comments.setdefault(
                    key,
                    CommentObservation(item["author"], item["text"], item.get("id")),
                )
            if snapshot["login_truncated"]:
                limitations.append("comments_truncated_by_login")
                break
            stagnant = stagnant + 1 if len(comments) == previous_count else 0
            previous_count = len(comments)
            if len(comments) >= self.comments_limit:
                limitations.append("comments_limit_reached")
                break
            moved = self.page.evaluate(
                """
                () => {
                  const node = document.querySelector('.note-scroller');
                  if (!node) return false;
                  const before = node.scrollTop;
                  node.scrollBy({top: Math.max(420, node.clientHeight * .75), behavior: 'auto'});
                  return node.scrollTop !== before;
                }
                """
            )
            if not moved:
                stagnant += 1
            self.page.wait_for_timeout(600)
            rounds += 1
        self.guard()
        raw = self._read_detail()
        for item in raw["comments"]:
            key = f"{item['author']}\0{item['text']}"
            comments.setdefault(key, CommentObservation(item["author"], item["text"], item.get("id")))
        if raw["login_truncated"] and "comments_truncated_by_login" not in limitations:
            limitations.append("comments_truncated_by_login")
        if raw["video_count"]:
            limitations.append("video_media_not_interpreted")
        evidence_refs = self._capture_observation(f"note-{link.resource_id}")
        selected_comments = tuple(list(comments.values())[: self.comments_limit])
        observation = Observation(
            platform=self.platform,
            resource_type="note",
            resource_id=link.resource_id,
            source_url=redact_url(self.page.url),
            metadata={
                "title": raw["title"] or link.title,
                "author": raw["author"],
                "tags": raw["tags"],
                "published": raw["published"],
                "displayed_comment_count": raw["displayed_comment_count"],
                "media": "video" if raw["video_count"] else "image" if raw["image_count"] else "unknown",
            },
            content=raw["content"],
            comments=selected_comments,
            evidence_refs=evidence_refs,
            limitations=tuple(limitations),
        )
        self.evidence.event(
            "observation_ready",
            platform=self.platform,
            resource_id=link.resource_id,
            comment_count=len(selected_comments),
            limitations=limitations,
        )
        return observation

    def close(self, link: ResourceLink) -> None:
        self.guard()
        close = self.page.locator(".close-circle:visible").first
        if close.count():
            close.click()
        else:
            self.page.go_back(wait_until="domcontentloaded")
        try:
            self.page.wait_for_function(
                "(id) => !location.pathname.includes(id) && !document.querySelector('.note-detail-mask')",
                arg=link.resource_id,
                timeout=8000,
            )
        except Exception as exc:
            raise PageInteractionError(f"关闭笔记 {link.resource_id} 后没有恢复列表") from exc
        self.guard()
        self.evidence.event("resource_closed", platform=self.platform, resource_id=link.resource_id, url=self.page.url)

    def _search(self, keyword: str) -> None:
        candidate = self.page.evaluate(
            """
            () => {
              const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 30 && rect.height > 15 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const hit = (node) => {
                const rect = node.getBoundingClientRect();
                const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return target && (target === node || node.contains(target) || target.contains(node));
              };
              const nodes = [...document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]')]
                .filter((node) => visible(node) && hit(node) && !node.disabled)
                .map((node, index) => ({
                  index,
                  node,
                  score: (/搜索|search/i.test([node.placeholder, node.ariaLabel, node.title].filter(Boolean).join(' ')) ? 100 : 0)
                    + (node.getBoundingClientRect().top < innerHeight * .35 ? 20 : 0)
                })).sort((a, b) => b.score - a.score);
              if (!nodes.length || nodes[0].score < 20) return null;
              const id = `puppy-search-${Date.now()}`;
              nodes[0].node.setAttribute('data-puppy-search', id);
              return id;
            }
            """
        )
        if not candidate:
            raise PageInteractionError("没有找到小红书当前可见搜索框")
        editor = self.page.locator(f'[data-puppy-search="{candidate}"]')
        editor.click()
        editor.fill(keyword)
        editor.press("Enter")
        try:
            self.page.wait_for_function(
                "() => location.pathname.startsWith('/search_result') && [...document.querySelectorAll('a[href]')].some((node) => /\\/(search_result|explore)\\/[0-9a-fA-F]{24}/.test(node.href))",
                timeout=30000,
            )
        except Exception as exc:
            self.guard()
            raise PageInteractionError("小红书搜索提交后没有加载出可见笔记") from exc
        self.guard()
        self.evidence.event("search_complete", platform=self.platform, keyword=keyword, url=self.page.url)

    def _read_detail(self) -> dict[str, Any]:
        return self.page.evaluate(
            r"""
            () => {
              const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 1 && rect.height > 1 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const root = [...document.querySelectorAll('.note-detail-mask')].find(visible) || document.body;
              const text = (node, limit = 4000) => (node?.innerText || node?.textContent || '').trim().replace(/\s+/g, ' ').slice(0, limit);
              const first = (selectors) => selectors.map((selector) => [...root.querySelectorAll(selector)].find(visible)).find(Boolean);
              const title = text(first(['.title', '[class*="title"]']), 300);
              const author = text(first(['.author-container .name', '.author .name', '[class*="author"] [class*="name"]', 'a[href*="/user/profile"]']), 120);
              const description = text(first(['.desc', '.note-text', '[class*="desc"]']), 6000);
              const published = text(first(['.date', '[class*="date"]', '[class*="time"]']), 120);
              const tags = [...root.querySelectorAll('a, span')].filter((node) => visible(node) && text(node, 100).startsWith('#')).map((node) => text(node, 100)).filter((value, index, all) => all.indexOf(value) === index).slice(0, 30);
              const comments = [...root.querySelectorAll('.comment-item, [class*="comment-item"], [class*="commentItem"]')].filter(visible).map((node, index) => {
                const authorNode = node.querySelector('[class*="author"], [class*="name"], a[href*="/user/profile"]');
                const contentNode = node.querySelector('.note-text, [class*="content"], [class*="text"]');
                return {id: node.getAttribute('data-id') || `visible-${index}`, author: text(authorNode, 100), text: text(contentNode || node, 800)};
              }).filter((item) => item.text);
              const bodyText = text(root, 10000);
              const total = bodyText.match(/共\s*([0-9万+.]+)\s*条评论/)?.[1] || null;
              return {
                title,
                author,
                content: description || bodyText.slice(0, 6000),
                published,
                tags,
                comments,
                displayed_comment_count: total,
                login_truncated: !!root.querySelector('.comments-login') || bodyText.includes('登录查看全部评论内容'),
                video_count: [...root.querySelectorAll('video')].filter(visible).length,
                image_count: [...root.querySelectorAll('img')].filter(visible).length
              };
            }
            """
        )

    def _capture(self, prefix: str) -> str:
        self._capture_serial += 1
        safe = re.sub(r"[^a-z0-9_-]", "-", prefix.lower())[:58].strip("-") or "page"
        name = f"{self._capture_serial:03d}-{safe}"
        try:
            return self.evidence.save_viewport(name, self.page).name
        except Exception as exc:
            self.evidence.event("capture_failed", platform=self.platform, error=str(exc))
            return ""

    def _capture_observation(self, prefix: str) -> tuple[str, ...]:
        self._capture_serial += 1
        safe = re.sub(r"[^a-z0-9_-]", "-", prefix.lower())[:58].strip("-") or "page"
        name = f"{self._capture_serial:03d}-{safe}"
        try:
            files = self.evidence.save_page(self.page, name)
            return tuple(files.values())
        except Exception as exc:
            self.evidence.event("capture_failed", platform=self.platform, error=str(exc))
            return ()
