from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_TOKEN_PATTERN = re.compile(
    r"(?i)(xsec_token|token|authorization|web_session)=([^&\s\"']+)"
)


def redact_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return _TOKEN_PATTERN.sub(r"\1=[REDACTED]", value)
    if not parts.scheme or not parts.netloc:
        return parts.path
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact_text(value: str) -> str:
    return _TOKEN_PATTERN.sub(r"\1=[REDACTED]", value)


class EvidenceStore:
    def __init__(self, root: Path, run_dir: Path | None = None) -> None:
        if run_dir is None:
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.run_dir = root / run_id
            self.run_dir.mkdir(parents=True, exist_ok=False)
        else:
            self.run_dir = run_dir
            self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.summary_path = self.run_dir / "summary.json"

    def event(self, event_type: str, **data: Any) -> None:
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": event_type,
            **self._sanitize(data),
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    def write_summary(self, summary: dict[str, Any]) -> None:
        self._write_json(self.summary_path, self._sanitize(summary))

    def save_json(self, name: str, value: Any) -> Path:
        path = self.run_dir / f"{name}.json"
        self._write_json(path, self._sanitize(value))
        return path

    def save_page(self, page: Any, stage: str, full_page: bool = False) -> dict[str, str]:
        screenshot_path = self.run_dir / f"{stage}.png"
        html_path = self.run_dir / f"{stage}.html"
        text_path = self.run_dir / f"{stage}.txt"

        page.screenshot(path=str(screenshot_path), full_page=False)
        files = {"screenshot": screenshot_path.name}
        if full_page:
            full_page_path = self.run_dir / f"{stage}-full.png"
            try:
                page.screenshot(path=str(full_page_path), full_page=True)
                files["full_page_screenshot"] = full_page_path.name
            except Exception:
                pass
        html = page.evaluate(
            """
            () => {
              const clone = document.documentElement.cloneNode(true);
              clone.querySelectorAll('script, style, link[rel="preload"]').forEach((node) => node.remove());
              clone.querySelectorAll('input, textarea').forEach((node) => {
                node.removeAttribute('value');
                if (node.tagName === 'TEXTAREA') node.textContent = '';
              });
              for (const attribute of ['href', 'src', 'action']) {
                clone.querySelectorAll(`[${attribute}]`).forEach((node) => {
                  const raw = node.getAttribute(attribute);
                  if (!raw || raw.startsWith('data:') || raw.startsWith('blob:')) {
                    node.removeAttribute(attribute);
                    return;
                  }
                  try {
                    const parsed = new URL(raw, document.baseURI);
                    node.setAttribute(attribute, parsed.origin + parsed.pathname);
                  } catch (_) {
                    node.removeAttribute(attribute);
                  }
                });
              }
              return '<!doctype html>\\n' + clone.outerHTML;
            }
            """
        )
        html_path.write_text(redact_text(html), encoding="utf-8")

        visible_text = page.locator("body").inner_text(timeout=5000)
        lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
        summary_text = "\n".join(lines[:100])[:10000]
        text_path.write_text(redact_text(summary_text), encoding="utf-8")

        return {**files, "html": html_path.name, "text": text_path.name}

    def _write_json(self, path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(
                    secret in lowered
                    for secret in ("cookie", "authorization", "localstorage", "session_value")
                ):
                    continue
                if lowered in {"url", "href", "source_url", "before_url", "after_url"}:
                    sanitized[str(key)] = redact_url(str(item))
                else:
                    sanitized[str(key)] = self._sanitize(item)
            return sanitized
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return redact_text(value)
        return value
