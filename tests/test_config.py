import json

import pytest

from xhs_robot.config import AIConfigStore, ConfigurationError


VALID_ENV = """# keep this comment
XHS_PROVIDER=siliconflow
XHS_API_KEY=sk-private-test-value
XHS_BASE_URL=https://api.siliconflow.cn/v1
XHS_MODEL=Pro/zai-org/GLM-5.1
XHS_API_STYLE=chat_completions
XHS_REQUEST_TIMEOUT_SECONDS=60
XHS_MAX_OUTPUT_TOKENS=300
"""


def config_store(tmp_path) -> AIConfigStore:
    env_file = tmp_path / ".env"
    example_file = tmp_path / ".env.example"
    env_file.write_text(VALID_ENV, encoding="utf-8")
    example_file.write_text(VALID_ENV, encoding="utf-8")
    return AIConfigStore(env_file, example_file)


def test_public_configuration_never_returns_api_key(tmp_path) -> None:
    secret = "sk-private-test-value"
    store = config_store(tmp_path)

    public = store.read_public()

    assert public["api_key_present"] is True
    assert public["values"]["XHS_API_KEY"] == ""
    assert secret not in json.dumps(public, ensure_ascii=False)


def test_blank_key_preserves_existing_key_and_explicit_clear_removes_it(tmp_path) -> None:
    store = config_store(tmp_path)

    store.save({"XHS_API_KEY": ""})
    assert "XHS_API_KEY=sk-private-test-value" in store.path.read_text(encoding="utf-8")

    result = store.save({}, clear_api_key=True)
    assert "XHS_API_KEY=\n" in store.path.read_text(encoding="utf-8")
    assert result["api_key_present"] is False
    assert result["ready"] is False


def test_invalid_candidate_does_not_overwrite_configuration(tmp_path) -> None:
    store = config_store(tmp_path)
    before = store.path.read_text(encoding="utf-8")

    with pytest.raises(ConfigurationError, match="有效的 http"):
        store.save({"XHS_BASE_URL": "not-a-url"})

    assert store.path.read_text(encoding="utf-8") == before
