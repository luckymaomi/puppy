from __future__ import annotations

import re
from typing import Any

from ..evidence import EvidenceStore, redact_url
from ..observations import CommentObservation, Observation
from .base import AdvanceResult, HumanInterventionRequired, PageInteractionError, ResourceLink


VIDEO_PATTERN = re.compile(r"/video/(BV[0-9A-Za-z]+)(?:/|$)")
ARTICLE_PATTERN = re.compile(r"/(?:read/)?(cv[0-9]+)(?:/|$)")
SECURITY_MARKERS = ("安全验证", "请完成验证", "访问频繁", "操作频繁", "网络环境存在风险")


class BilibiliAdapter:
    platform = "bilibili"

    def __init__(self, page: Any, evidence: EvidenceStore, *, comments_limit: int) -> None:
        self.page = page
        self.list_page = page
        self.detail_page = None
        self.evidence = evidence
        self.comments_limit = comments_limit
        self.resource_type = "video"
        self._capture_serial = 0

    def prepare(self, keyword: str, resource_type: str) -> None:
        self.resource_type = resource_type
        if "bilibili.com" not in self.page.url:
            self.page.goto("https://www.bilibili.com/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)
        self.guard(self.page)
        self._search(keyword)
        if resource_type == "article":
            self._select_article_tab()
        self.evidence.event("source_ready", platform=self.platform, keyword=keyword, resource_type=resource_type, url=self.page.url)

    def guard(self, page: Any | None = None) -> None:
        current = page or self.page
        body = current.locator("body").inner_text(timeout=5000)[:30000]
        marker = next((item for item in SECURITY_MARKERS if item in body), None)
        if marker:
            raise HumanInterventionRequired(marker)
        close = current.locator(".login-tip .close:visible").first
        if close.count():
            close.click()
            current.wait_for_timeout(250)
            if current.locator(".login-tip:visible").count():
                raise HumanInterventionRequired("哔哩哔哩右下角登录提示关闭后仍阻断页面")
            self.evidence.event("login_prompt_closed", platform=self.platform, url=current.url)

    def discover(self) -> list[ResourceLink]:
        self.guard(self.page)
        raw = self.page.evaluate(
            r"""
            () => [...document.querySelectorAll('a[href]')].map((node) => {
              let path = '';
              try { path = new URL(node.href, document.baseURI).pathname; } catch (_) {}
              return {path, title: (node.innerText || node.title || node.ariaLabel || '').trim().replace(/\s+/g, ' ').slice(0, 300)};
            })
            """
        )
        seen: set[str] = set()
        links: list[ResourceLink] = []
        for item in raw:
            pattern = VIDEO_PATTERN if self.resource_type == "video" else ARTICLE_PATTERN
            match = pattern.search(item["path"])
            if not match:
                continue
            resource_id = match.group(1)
            if resource_id in seen:
                continue
            seen.add(resource_id)
            links.append(ResourceLink(resource_id, self.resource_type, item["path"], item["title"]))
        self.evidence.event(
            "resources_observed",
            platform=self.platform,
            resource_type=self.resource_type,
            count=len(links),
            resource_ids=[item.resource_id for item in links],
        )
        return links

    def advance(self) -> AdvanceResult:
        self.guard(self.page)
        before = self.page.evaluate(
            "() => ({top: document.scrollingElement.scrollTop, height: document.scrollingElement.scrollHeight, client: document.scrollingElement.clientHeight})"
        )
        size = self.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        self.page.mouse.move(size["width"] * 0.72, size["height"] * 0.72)
        self.page.mouse.wheel(0, 760)
        self.page.wait_for_timeout(700)
        after = self.page.evaluate(
            "() => ({top: document.scrollingElement.scrollTop, height: document.scrollingElement.scrollHeight, client: document.scrollingElement.clientHeight})"
        )
        moved = after["top"] != before["top"] or after["height"] != before["height"]
        at_bottom = after["top"] + after["client"] >= after["height"] - 8
        if at_bottom:
            next_button = self._visible_next_button()
            if next_button is not None:
                before_ids = {item.resource_id for item in self.discover()}
                next_button.click()
                try:
                    self.page.wait_for_function(
                        "(ids) => [...document.querySelectorAll('a[href]')].some((node) => !ids.some((id) => node.href.includes(id)) && /\\/(video\\/BV|read\\/cv)/.test(node.href))",
                        arg=list(before_ids),
                        timeout=15000,
                    )
                except Exception as exc:
                    self.guard(self.page)
                    raise PageInteractionError("哔哩哔哩点击下一页后结果集合没有变化") from exc
                self.page.evaluate("() => window.scrollTo(0, 0)")
                self.evidence.event("source_page_changed", platform=self.platform, url=self.page.url)
                return AdvanceResult(moved=True, page_changed=True)
            self.evidence.event("source_exhausted", platform=self.platform, url=self.page.url)
            return AdvanceResult(moved=moved, source_exhausted=True)
        self.evidence.event(
            "source_advanced",
            platform=self.platform,
            before_top=round(before["top"]),
            after_top=round(after["top"]),
            moved=moved,
        )
        return AdvanceResult(moved=moved)

    def open(self, link: ResourceLink) -> None:
        self.guard(self.page)
        candidates = self.page.locator(f'a[href*="{link.resource_id}"]')
        target = next(
            (candidates.nth(index) for index in range(candidates.count()) if candidates.nth(index).is_visible()),
            None,
        )
        if target is None:
            raise PageInteractionError(f"资源 {link.resource_id} 已离开当前结果页")
        before_pages = set(self.page.context.pages)
        target.click()
        self.page.wait_for_timeout(900)
        new_pages = [item for item in self.page.context.pages if item not in before_pages]
        self.detail_page = new_pages[-1] if new_pages else self.page
        self.detail_page.set_default_timeout(8000)
        try:
            if link.resource_type == "article":
                self.detail_page.wait_for_function(
                    r"(id) => location.pathname.includes(id) || /^\/opus\/[0-9]+/.test(location.pathname)",
                    arg=link.resource_id,
                    timeout=12000,
                )
            else:
                self.detail_page.wait_for_function(
                    "(id) => location.href.includes(id)", arg=link.resource_id, timeout=12000
                )
        except Exception as exc:
            raise PageInteractionError(f"点击资源 {link.resource_id} 后未确认详情页") from exc
        self.detail_page.wait_for_timeout(700)
        self.guard(self.detail_page)
        self.evidence.event(
            "resource_opened",
            platform=self.platform,
            resource_type=link.resource_type,
            resource_id=link.resource_id,
            opened_new_page=self.detail_page is not self.page,
            url=self.detail_page.url,
        )

    def observe(self, link: ResourceLink) -> Observation:
        if self.detail_page is None:
            raise PageInteractionError("哔哩哔哩详情页不存在")
        current = self.detail_page
        self.guard(current)
        limitations: list[str] = []
        comments: tuple[CommentObservation, ...] = ()
        metadata: dict[str, Any]
        content: str
        if link.resource_type == "video":
            metadata, content, comments, comment_limitations = self._observe_video(current)
            limitations.extend(comment_limitations)
            limitations.append("video_media_not_interpreted")
        else:
            metadata, content, article_limitations = self._observe_article(current, link)
            limitations.extend(article_limitations)
        evidence_refs = self._capture_observation(
            current, f"{link.resource_type}-{link.resource_id}"
        )
        observation = Observation(
            platform=self.platform,
            resource_type=link.resource_type,
            resource_id=link.resource_id,
            source_url=redact_url(current.url),
            metadata=metadata,
            content=content,
            comments=comments,
            evidence_refs=evidence_refs,
            limitations=tuple(dict.fromkeys(limitations)),
        )
        self.evidence.event(
            "observation_ready",
            platform=self.platform,
            resource_type=link.resource_type,
            resource_id=link.resource_id,
            comment_count=len(comments),
            limitations=limitations,
        )
        return observation

    def close(self, link: ResourceLink) -> None:
        if self.detail_page is None:
            return
        detail = self.detail_page
        if detail is not self.page:
            detail.close()
        else:
            detail.go_back(wait_until="domcontentloaded")
        self.detail_page = None
        self.page.bring_to_front()
        self.guard(self.page)
        self.evidence.event("resource_closed", platform=self.platform, resource_id=link.resource_id, url=self.page.url)

    def _search(self, keyword: str) -> None:
        selectors = (
            "input.nav-search-input:visible",
            "#nav-searchform input:visible",
            "input[placeholder*='搜索']:visible",
        )
        editor = next(
            (self.page.locator(selector).first for selector in selectors if self.page.locator(selector).count()),
            None,
        )
        if editor is None:
            raise PageInteractionError("没有找到哔哩哔哩当前可见搜索框")
        before_pages = set(self.page.context.pages)
        editor.click()
        editor.fill(keyword)
        editor.press("Enter")
        self.page.wait_for_timeout(900)
        new_pages = [item for item in self.page.context.pages if item not in before_pages]
        if new_pages:
            self.page = new_pages[-1]
            self.list_page = self.page
            self.page.set_default_timeout(8000)
        try:
            self.page.wait_for_function(
                "() => location.hostname.includes('search.bilibili.com') && [...document.querySelectorAll('a[href]')].some((node) => /\\/(video\\/BV|read\\/cv)/.test(node.href))",
                timeout=20000,
            )
        except Exception as exc:
            self.guard(self.page)
            raise PageInteractionError("哔哩哔哩搜索提交后没有加载出公开结果") from exc
        self.guard(self.page)
        self.evidence.event("search_complete", platform=self.platform, keyword=keyword, url=self.page.url)

    def _select_article_tab(self) -> None:
        candidates = self.page.get_by_text("专栏", exact=True)
        target = next(
            (candidates.nth(index) for index in range(candidates.count()) if candidates.nth(index).is_visible()),
            None,
        )
        if target is None:
            raise PageInteractionError("哔哩哔哩搜索页没有可见专栏标签")
        target.click()
        try:
            self.page.wait_for_function(
                "() => [...document.querySelectorAll('a[href]')].some((node) => /\\/read\\/cv[0-9]+/.test(node.href))",
                timeout=15000,
            )
        except Exception as exc:
            raise PageInteractionError("切换专栏标签后没有识别到公开摘要") from exc

    def _visible_next_button(self):
        candidates = self.page.get_by_text("下一页", exact=True)
        for index in range(candidates.count()):
            item = candidates.nth(index)
            if item.is_visible() and item.is_enabled():
                return item
        return None

    def _observe_video(self, page: Any) -> tuple[dict[str, Any], str, tuple[CommentObservation, ...], list[str]]:
        metadata = page.evaluate(
            r"""
            () => {
              const text = (selector, limit = 1000) => (document.querySelector(selector)?.innerText || '').trim().replace(/\s+/g, ' ').slice(0, limit);
              return {
                title: text('.video-title, h1'),
                uploader: text('.up-name, .up-info-container .name, [class*="up-name"]', 200),
                description: text('.desc-info-text, .basic-desc-info, [class*="desc-info"]', 4000),
                stats: text('.video-info-detail, .video-info-meta, .view-text', 1000),
                published: text('.pubdate-text, [class*="pubdate"]', 200),
                tags: [...document.querySelectorAll('.tag-link, [class*="tag"] a')].map((node) => (node.innerText || '').trim()).filter(Boolean).filter((value, index, all) => all.indexOf(value) === index).slice(0, 30)
              };
            }
            """
        )
        comments: dict[str, CommentObservation] = {}
        login_truncated = False
        displayed_count = None
        stagnant = 0
        previous_count = -1
        for _ in range(12):
            self.guard(page)
            snapshot = self._read_shadow_comments(page)
            login_truncated = login_truncated or snapshot["login_truncated"]
            displayed_count = snapshot["displayed_count"] or displayed_count
            for item in snapshot["comments"]:
                key = f"{item['author']}\0{item['text']}"
                comments.setdefault(key, CommentObservation(item["author"], item["text"], item.get("id")))
            if len(comments) >= self.comments_limit or login_truncated:
                break
            stagnant = stagnant + 1 if len(comments) == previous_count else 0
            previous_count = len(comments)
            if stagnant >= 2 and page.locator("bili-comments").count():
                break
            page.mouse.wheel(0, 760)
            page.wait_for_timeout(650)
        limitations: list[str] = []
        if login_truncated:
            limitations.append("comments_truncated_by_login")
        if self.comments_limit and len(comments) >= self.comments_limit:
            limitations.append("comments_limit_reached")
        selected = tuple(list(comments.values())[: self.comments_limit])
        metadata["displayed_comment_count"] = displayed_count
        return metadata, metadata.get("description", ""), selected, limitations

    def _read_shadow_comments(self, page: Any) -> dict[str, Any]:
        return page.evaluate(
            r"""
            () => {
              const deepAll = (root, selector) => {
                const results = [...root.querySelectorAll(selector)];
                for (const node of root.querySelectorAll('*')) {
                  if (node.shadowRoot) results.push(...deepAll(node.shadowRoot, selector));
                }
                return results;
              };
              const deepText = (root, selector) => {
                const node = deepAll(root, selector)[0];
                const target = node?.shadowRoot || node;
                return (target?.textContent || '').trim().replace(/\s+/g, ' ');
              };
              const renderers = deepAll(document, 'bili-comment-renderer');
              const comments = renderers.map((renderer, index) => {
                const root = renderer.shadowRoot || renderer;
                return {
                  id: renderer.getAttribute('data-rpid') || `visible-${index}`,
                  author: deepText(root, 'bili-comment-user-info').slice(0, 120),
                  text: deepText(root, 'bili-rich-text').slice(0, 1200)
                };
              }).filter((item) => item.text);
              const commentsHost = deepAll(document, 'bili-comments')[0];
              const hostText = (commentsHost?.shadowRoot?.textContent || commentsHost?.textContent || '').replace(/\s+/g, ' ');
              return {
                comments,
                login_truncated: /登录后查看\s*[0-9]+\s*条评论/.test(hostText),
                displayed_count: hostText.match(/评论\s*([0-9万+.]+)/)?.[1] || null
              };
            }
            """
        )

    def _observe_article(
        self, page: Any, link: ResourceLink
    ) -> tuple[dict[str, Any], str, list[str]]:
        raw = page.evaluate(
            r"""
            () => {
              const text = (selector, limit = 6000) => (document.querySelector(selector)?.innerText || '').trim().replace(/\s+/g, ' ').slice(0, limit);
              return {
                title: text('h1, .title', 500),
                author: text('.up-name, .author-name, [class*="author"]', 200),
                published: text('.publish-text, [class*="time"]', 200),
                content: text('.opus-module-content, .article-holder, .article-content, #read-article-holder', 12000),
                body: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 3000)
              };
            }
            """
        )
        content = raw["content"]
        limitations: list[str] = []
        if not content:
            limitations.append(
                "article_body_login_blocked" if "登录" in raw["body"] else "article_body_unavailable"
            )
        return {
            "title": raw["title"] or link.title,
            "author": raw["author"],
            "published": raw["published"],
        }, content, limitations

    def _capture(self, page: Any, prefix: str) -> str:
        self._capture_serial += 1
        safe = re.sub(r"[^a-z0-9_-]", "-", prefix.lower())[:58].strip("-") or "page"
        name = f"{self._capture_serial:03d}-{safe}"
        try:
            return self.evidence.save_viewport(name, page).name
        except Exception as exc:
            self.evidence.event("capture_failed", platform=self.platform, error=str(exc))
            return ""

    def _capture_observation(self, page: Any, prefix: str) -> tuple[str, ...]:
        self._capture_serial += 1
        safe = re.sub(r"[^a-z0-9_-]", "-", prefix.lower())[:58].strip("-") or "page"
        name = f"{self._capture_serial:03d}-{safe}"
        try:
            files = self.evidence.save_page(page, name)
            return tuple(files.values())
        except Exception as exc:
            self.evidence.event("capture_failed", platform=self.platform, error=str(exc))
            return ()
