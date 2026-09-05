from dataclasses import replace

import pytest

from config import ReplyProfile, Settings, load_project_env, save_project_env, save_reply_profiles


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


def test_reply_profiles_are_persisted_and_matched_by_contact(monkeypatch, tmp_path):
    profiles_path = tmp_path / "reply_profiles.json"
    monkeypatch.setattr("config.REPLY_PROFILES_PATH", profiles_path)
    default = ReplyProfile("默认方案", "默认语气", 0, 100, 500, 4000)
    customer = ReplyProfile("客户", "专业、简洁", 15, 20, 80, 1000)

    save_reply_profiles((default, customer), {"客户A": "客户"})
    monkeypatch.setenv("WECHAT_TARGETS", "客户A,朋友B")
    settings = Settings.from_env()

    assert settings.reply_profile_for("客户A").name == "客户"
    assert settings.reply_profile_for("朋友B").name == "默认方案"
    assert settings.with_reply_profile(settings.reply_profile_for("客户A")).max_reply_chars == 80
