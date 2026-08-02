from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
SUPPORTED_API_STYLES = {"chat_completions", "responses"}


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

        def required(name: str) -> str:
            value = values.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"{resolved.name} 缺少必填配置 {name}")
            return value.strip()

        provider = required("XHS_PROVIDER").lower()
        api_key = required("XHS_API_KEY")
        base_url = required("XHS_BASE_URL").rstrip("/")
        model = required("XHS_MODEL")
        api_style = required("XHS_API_STYLE").lower()

        if api_key.lower().startswith(("replace-", "your-", "example-")):
            raise ConfigurationError("XHS_API_KEY 仍是模板占位值")
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigurationError("XHS_BASE_URL 必须是有效的 http(s) URL")
        if parsed_url.query or parsed_url.fragment:
            raise ConfigurationError("XHS_BASE_URL 不能包含查询参数或片段")
        if api_style not in SUPPORTED_API_STYLES:
            choices = ", ".join(sorted(SUPPORTED_API_STYLES))
            raise ConfigurationError(f"XHS_API_STYLE 只能是 {choices}")

        timeout = _number(values, "XHS_REQUEST_TIMEOUT_SECONDS", minimum=1, maximum=300)
        max_tokens = _integer(values, "XHS_MAX_OUTPUT_TOKENS", minimum=1, maximum=4096)
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


def _number(
    values: dict[str, str | None], name: str, *, minimum: float, maximum: float
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
    values: dict[str, str | None], name: str, *, minimum: int, maximum: int
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
