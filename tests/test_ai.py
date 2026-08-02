import json
from types import SimpleNamespace

import pytest

from xhs_robot.ai import AIContentGenerator, AIProvider, GenerationContext, validate_draft
from xhs_robot.config import AIConfig, ConfigurationError


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
                "XHS_PROVIDER=siliconflow",
                "XHS_API_KEY=sk-file-secret-value",
                "XHS_BASE_URL=https://api.siliconflow.cn/v1",
                "XHS_MODEL=Pro/zai-org/GLM-5.1",
                "XHS_API_STYLE=chat_completions",
                "XHS_REQUEST_TIMEOUT_SECONDS=60",
                "XHS_MAX_OUTPUT_TOKENS=300",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XHS_MODEL", "process-environment-model")

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


def test_siliconflow_uses_chat_completions_wire_api() -> None:
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
    generator = AIContentGenerator(config(), provider=provider)

    value = generator.generate_comment(
        GenerationContext(note_text="笔记正文", comments=["可见评论"])
    )

    assert value == "这篇把关键点讲清楚了。"
    assert captured["model"] == "Pro/zai-org/GLM-5.1"
    assert captured["messages"][0]["role"] == "system"


def test_validate_draft_returns_sendable_single_paragraph() -> None:
    assert validate_draft("  这篇把关键点讲清楚了。  ", 30) == "这篇把关键点讲清楚了。"


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_validate_draft_rejects_empty_content(value: str) -> None:
    with pytest.raises(ValueError, match="空草稿"):
        validate_draft(value, 30)


def test_validate_draft_rejects_content_over_limit() -> None:
    with pytest.raises(ValueError, match="超过限制"):
        validate_draft("一" * 31, 30)
