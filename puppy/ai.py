from __future__ import annotations

import re
from typing import Any

from .config import AIConfig


class GenerationError(RuntimeError):
    pass


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
