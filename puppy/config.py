from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
SUPPORTED_API_STYLES = {"chat_completions", "responses"}
CONFIG_KEYS = (
    "PUPPY_AI_PROVIDER",
    "PUPPY_AI_API_KEY",
    "PUPPY_AI_BASE_URL",
    "PUPPY_AI_MODEL",
    "PUPPY_AI_API_STYLE",
    "PUPPY_AI_REQUEST_TIMEOUT_SECONDS",
    "PUPPY_AI_MAX_OUTPUT_TOKENS",
)


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AIConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    api_style: str
    request_timeout_seconds: float
    max_output_tokens: int

    @classmethod
    def from_env_file(cls, path: Path = DEFAULT_ENV_FILE) -> "AIConfig":
        resolved = path.resolve()
        if not resolved.is_file():
            raise ConfigurationError(
                f"配置文件不存在: {resolved}；请从 .env.example 创建 .env"
            )
        values = dotenv_values(resolved, encoding="utf-8", interpolate=False)
        return cls.from_mapping(values, source=resolved.name)

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str | None], *, source: str = ".env"
    ) -> "AIConfig":

        def required(name: str) -> str:
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"{source} 缺少必填配置 {name}")
            return value.strip()

        provider = required("PUPPY_AI_PROVIDER").lower()
        api_key = required("PUPPY_AI_API_KEY")
        base_url = required("PUPPY_AI_BASE_URL").rstrip("/")
        model = required("PUPPY_AI_MODEL")
        api_style = required("PUPPY_AI_API_STYLE").lower()

        if api_key.lower().startswith(("replace-", "your-", "example-")):
            raise ConfigurationError("PUPPY_AI_API_KEY 仍是模板占位值")
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigurationError("PUPPY_AI_BASE_URL 必须是有效的 http(s) URL")
        if parsed_url.username or parsed_url.password:
            raise ConfigurationError("PUPPY_AI_BASE_URL 不能包含用户名或密码")
        if parsed_url.query or parsed_url.fragment:
            raise ConfigurationError("PUPPY_AI_BASE_URL 不能包含查询参数或片段")
        if api_style not in SUPPORTED_API_STYLES:
            choices = ", ".join(sorted(SUPPORTED_API_STYLES))
            raise ConfigurationError(f"PUPPY_AI_API_STYLE 只能是 {choices}")

        timeout = _number(
            values, "PUPPY_AI_REQUEST_TIMEOUT_SECONDS", minimum=1, maximum=300
        )
        max_tokens = _integer(
            values, "PUPPY_AI_MAX_OUTPUT_TOKENS", minimum=1, maximum=4096
        )
        return cls(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            api_style=api_style,
            request_timeout_seconds=timeout,
            max_output_tokens=max_tokens,
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "api_key_present": bool(self.api_key),
            "base_url": self.base_url,
            "model": self.model,
            "api_style": self.api_style,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_output_tokens": self.max_output_tokens,
        }


class AIConfigStore:
    def __init__(
        self,
        path: Path = DEFAULT_ENV_FILE,
        example_path: Path | None = None,
    ) -> None:
        self.path = path.resolve()
        self.example_path = (example_path or self.path.with_name(".env.example")).resolve()

    def ensure(self) -> None:
        if self.path.is_file():
            return
        if not self.example_path.is_file():
            raise ConfigurationError(f"配置模板不存在: {self.example_path}")
        self._atomic_write(self.example_path.read_text(encoding="utf-8"))

    def read_public(self) -> dict[str, Any]:
        self.ensure()
        values = self._read_values()
        public_values = {
            key: "" if key == "PUPPY_AI_API_KEY" else str(values.get(key) or "")
            for key in CONFIG_KEYS
        }
        error: str | None = None
        try:
            AIConfig.from_mapping(values)
        except ConfigurationError as exc:
            error = str(exc)
        return {
            "file": self.path.name,
            "values": public_values,
            "api_key_present": bool(str(values.get("PUPPY_AI_API_KEY") or "").strip()),
            "ready": error is None,
            "error": error,
        }

    def save(
        self,
        updates: Mapping[str, object],
        *,
        clear_api_key: bool = False,
    ) -> dict[str, Any]:
        self.ensure()
        unknown = sorted(set(updates) - set(CONFIG_KEYS))
        if unknown:
            raise ConfigurationError(f"未知配置项: {', '.join(unknown)}")

        normalized: dict[str, str] = {}
        for key, raw in updates.items():
            value = str(raw if raw is not None else "").strip()
            if "\r" in value or "\n" in value:
                raise ConfigurationError(f"{key} 必须是单行配置")
            if key == "PUPPY_AI_API_KEY" and not value and not clear_api_key:
                continue
            normalized[key] = value
        if clear_api_key:
            normalized["PUPPY_AI_API_KEY"] = ""

        current = self.path.read_text(encoding="utf-8")
        candidate = _update_env_text(current, normalized)
        parsed = dotenv_values(stream=StringIO(candidate), interpolate=False)
        validation_values = dict(parsed)
        if clear_api_key and not str(validation_values.get("PUPPY_AI_API_KEY") or ""):
            validation_values["PUPPY_AI_API_KEY"] = "sk-explicitly-cleared"
        AIConfig.from_mapping(validation_values)
        self._atomic_write(candidate)
        return self.read_public()

    def _read_values(self) -> Mapping[str, str | None]:
        return dotenv_values(self.path, encoding="utf-8", interpolate=False)

    def _atomic_write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _number(
    values: Mapping[str, str | None], name: str, *, minimum: float, maximum: float
) -> float:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(f".env 缺少必填配置 {name}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return value


def _integer(
    values: Mapping[str, str | None], name: str, *, minimum: int, maximum: int
) -> int:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(f".env 缺少必填配置 {name}")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _update_env_text(content: str, updates: Mapping[str, str]) -> str:
    consumed: set[str] = set()
    lines: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        key = match.group(1) if match else None
        if key is None or key not in updates:
            lines.append(line)
            continue
        lines.append(f"{key}={_encode_env_value(updates[key])}")
        consumed.add(key)
    for key, value in updates.items():
        if key not in consumed:
            lines.append(f"{key}={_encode_env_value(value)}")
    return "\n".join(lines).rstrip() + "\n"


def _encode_env_value(value: str) -> str:
    if not value or re.fullmatch(r"[^\s#='\"\\]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)
