from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from .config import AIConfig


COMMENT_LIMIT = 120
REPLY_LIMIT = 80


class GenerationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GenerationContext:
    note_text: str
    comments: Sequence[str]
    target_comment: str | None = None


class AIProvider:
    def __init__(self, config: AIConfig, client: Any | None = None) -> None:
        self.config = config
        if client is not None:
            self.client = client
            return
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise GenerationError("缺少 openai 依赖，请重新安装 requirements.txt") from exc
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
        )

    def list_model_ids(self) -> list[str]:
        try:
            response = self.client.models.list()
            ids = {
                item.id.strip()
                for item in response.data
                if isinstance(getattr(item, "id", None), str) and item.id.strip()
            }
        except Exception as exc:
            raise GenerationError(f"Provider 模型目录请求失败: {self._safe_error(exc)}") from exc
        if not ids:
            raise GenerationError("Provider 模型目录为空")
        return sorted(ids, key=str.casefold)

    def health(self, *, generate: bool = False) -> dict[str, object]:
        model_ids = self.list_model_ids()
        if self.config.model not in model_ids:
            raise GenerationError(
                f"配置模型 {self.config.model!r} 不在当前 API Key 可见的模型目录中"
            )
        result: dict[str, object] = {
            "ok": True,
            "config": self.config.public_dict(),
            "model_catalog": {
                "ok": True,
                "count": len(model_ids),
                "selected_model_available": True,
            },
        }
        if generate:
            output = self._request(
                "你正在执行连接健康检查。只回复四个汉字：连接正常。",
                "请执行健康检查。",
                max_tokens=min(32, self.config.max_output_tokens),
            )
            result["generation"] = {"ok": bool(output.strip()), "output": output.strip()}
        return result

    def generate(self, system: str, user: str) -> str:
        return self._request(system, user, max_tokens=self.config.max_output_tokens)

    def _request(self, system: str, user: str, *, max_tokens: int) -> str:
        try:
            if self.config.api_style == "chat_completions":
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=max_tokens,
                )
                content = response.choices[0].message.content
            else:
                response = self.client.responses.create(
                    model=self.config.model,
                    instructions=system,
                    input=user,
                    max_output_tokens=max_tokens,
                )
                content = response.output_text
        except Exception as exc:
            raise GenerationError(f"AI 请求失败: {self._safe_error(exc)}") from exc
        if not isinstance(content, str) or not content.strip():
            raise GenerationError("AI 返回了空内容")
        return content

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc).replace(self.config.api_key, "[REDACTED]")
        return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)


class AIContentGenerator:
    def __init__(self, config: AIConfig, provider: AIProvider | None = None) -> None:
        self.provider = provider or AIProvider(config)

    def generate_comment(self, context: GenerationContext) -> str:
        return self._generate("comment", context, COMMENT_LIMIT)

    def generate_reply(self, context: GenerationContext) -> str:
        if not context.target_comment:
            raise ValueError("生成回复时必须提供目标评论")
        return self._generate("reply", context, REPLY_LIMIT)

    def _generate(self, kind: str, context: GenerationContext, limit: int) -> str:
        purpose = "笔记评论" if kind == "comment" else "针对已有评论的回复"
        instructions = (
            "你为账号 owner 起草小红书互动文字。只输出可直接发送的正文，不加引号、标签或解释。"
            "内容必须基于提供的可见上下文，具体相关，不虚构亲历，不包含个人敏感信息、营销引流或重复套话。"
            f"本次输出是{purpose}，最多 {limit} 个字符。"
        )
        payload = {
            "note": context.note_text[:5000],
            "visible_comments": list(context.comments)[:20],
            "target_comment": context.target_comment,
        }
        try:
            value = self.provider.generate(
                instructions, json.dumps(payload, ensure_ascii=False)
            )
            return validate_draft(value, limit)
        except GenerationError:
            raise
        except Exception as exc:
            raise GenerationError(f"AI 草稿生成失败: {exc}") from exc


def validate_draft(value: str, limit: int) -> str:
    draft = " ".join(value.strip().split())
    if not draft:
        raise ValueError("AI 返回了空草稿")
    if len(draft) > limit:
        raise ValueError(f"AI 草稿长度 {len(draft)} 超过限制 {limit}")
    if "\n" in draft or "\r" in draft:
        raise ValueError("AI 草稿必须是单段文本")
    return draft
