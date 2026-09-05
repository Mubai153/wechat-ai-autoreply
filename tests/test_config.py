from dataclasses import replace

import pytest

from config import Settings, load_project_env, save_project_env


def test_ccswitch_is_default_and_has_local_route(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CCSWITCH_BASE_URL", raising=False)
    monkeypatch.delenv("CCSWITCH_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.llm_provider == "ccswitch"
    assert settings.llm_base_url == "http://127.0.0.1:15721/v1"
    assert settings.llm_model == ""


def test_openai_compatible_uses_its_own_model_setting(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("CODEX_MODEL", "codex-only")
    monkeypatch.setenv("LLM_MODEL", "chat-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("LLM_API_KEY", "secret")

    settings = Settings.from_env()

    assert settings.llm_model == "chat-model"
    assert settings.llm_base_url == "https://example.test/v1"
    assert settings.llm_api_key == "secret"


def test_lmstudio_uses_local_defaults_and_does_not_require_model_or_real_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.llm_base_url == "http://127.0.0.1:1234/v1"
    assert settings.llm_model == ""
    assert settings.llm_api_key == "lm-studio"


def test_ccswitch_rejects_remote_route_to_protect_local_credentials(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ccswitch")
    settings = replace(
        Settings.from_env(),
        llm_base_url="https://not-local.example/v1",
    )

    with pytest.raises(ValueError, match="本机回环地址"):
        settings.validate()


def test_saved_prompt_is_written_and_loaded_from_env_file(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SYSTEM_PROMPT=旧指令\nOTHER_SETTING=保留\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("config.ENV_PATH", env_path)
    for key in ("SYSTEM_PROMPT", "NEW_SETTING", "OTHER_SETTING"):
        monkeypatch.delenv(key, raising=False)
    save_project_env({"SYSTEM_PROMPT": "第一行\n第二行；不要丢失", "NEW_SETTING": "新值"})

    assert "SYSTEM_PROMPT='第一行\n第二行；不要丢失'" in env_path.read_text(encoding="utf-8")
    assert "OTHER_SETTING=保留" in env_path.read_text(encoding="utf-8")
    assert Settings.from_env().system_prompt == "第一行\n第二行；不要丢失"


def test_persisted_env_overrides_inherited_environment(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SYSTEM_PROMPT=已保存的指令\n", encoding="utf-8")
    monkeypatch.setattr("config.ENV_PATH", env_path)
    monkeypatch.setenv("SYSTEM_PROMPT", "进程里的旧指令")

    load_project_env()

    assert Settings.from_env().system_prompt == "已保存的指令"
