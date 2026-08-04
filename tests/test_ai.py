import json
from types import SimpleNamespace

import pytest

from puppy.ai import AIProvider
from puppy.config import AIConfig, ConfigurationError


def config(api_key: str = "sk-test-secret-value") -> AIConfig:
    return AIConfig(
        provider="siliconflow",
        api_key=api_key,
        base_url="https://api.siliconflow.cn/v1",
        model="Pro/zai-org/GLM-5.1",
        api_style="chat_completions",
        request_timeout_seconds=60,
        max_output_tokens=300,
    )


class FakeModels:
    def list(self):
        return SimpleNamespace(
            data=[SimpleNamespace(id="Pro/zai-org/GLM-5.1")]
        )


def test_env_file_is_the_only_ai_configuration_source(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "PUPPY_AI_PROVIDER=siliconflow",
                "PUPPY_AI_API_KEY=sk-file-secret-value",
                "PUPPY_AI_BASE_URL=https://api.siliconflow.cn/v1",
                "PUPPY_AI_MODEL=Pro/zai-org/GLM-5.1",
                "PUPPY_AI_API_STYLE=chat_completions",
                "PUPPY_AI_REQUEST_TIMEOUT_SECONDS=60",
                "PUPPY_AI_MAX_OUTPUT_TOKENS=300",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPY_AI_MODEL", "process-environment-model")

    loaded = AIConfig.from_env_file(env_file)

    assert loaded.model == "Pro/zai-org/GLM-5.1"
    assert loaded.api_key == "sk-file-secret-value"


def test_missing_env_file_fails_with_actionable_error(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match=r"\.env\.example"):
        AIConfig.from_env_file(tmp_path / ".env")


def test_health_report_never_contains_api_key() -> None:
    secret = "sk-health-secret-value"
    client = SimpleNamespace(models=FakeModels())

    report = AIProvider(config(secret), client=client).health()

    assert report["config"]["api_key_present"] is True
    assert secret not in json.dumps(report, ensure_ascii=False)


def test_provider_uses_configured_chat_completions_wire_api() -> None:
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="这篇把关键点讲清楚了。")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(
            create=lambda **_: pytest.fail("不应调用 Responses API")
        ),
    )
    provider = AIProvider(config(), client=client)
    value = provider.generate("只返回结果", "公开观察")

    assert value == "这篇把关键点讲清楚了。"
    assert captured["model"] == "Pro/zai-org/GLM-5.1"
    assert captured["messages"][0] == {"role": "system", "content": "只返回结果"}
