import json
import sqlite3
from types import SimpleNamespace

import pytest

from config import Settings
from llm import ReplyGenerator


def _settings() -> Settings:
    return Settings(
        llm_provider="ccswitch",
        llm_model="",
        codex_command="codex",
        codex_timeout_seconds=120,
        llm_base_url="http://127.0.0.1:15721/v1",
        llm_api_key="",
        wechat_target="小明",
        auto_send=False,
        reply_cooldown_seconds=0,
        max_history_messages=10,
        max_reply_chars=500,
        max_input_chars=4000,
        database_path=SimpleNamespace(),
        system_prompt="简短回复",
        log_level="INFO",
    )


def _set_current_provider(
    ccswitch_home,
    *,
    name: str,
    category: str | None,
    model: str,
) -> None:
    ccswitch_home.mkdir(exist_ok=True)
    database_path = ccswitch_home / "cc-switch.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS providers ("
            "name TEXT, category TEXT, settings_config TEXT, "
            "app_type TEXT, is_current INTEGER)"
        )
        connection.execute("DELETE FROM providers")
        config = f'model = "{model}"\n' if model else ""
        connection.execute(
            "INSERT INTO providers VALUES (?, ?, ?, 'codex', 1)",
            (name, category, json.dumps({"config": config})),
        )


def test_ccswitch_follows_model_changes_and_uses_responses_stream(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "custom"\nmodel = "first-model"\n'
        '[model_providers.custom]\nname = "Third Party"\n'
        'base_url = "http://127.0.0.1:15721/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ccswitch_home = tmp_path / "cc-switch"
    monkeypatch.setenv("CCSWITCH_HOME", str(ccswitch_home))
    _set_current_provider(
        ccswitch_home,
        name="First Provider",
        category=None,
        model="first-model",
    )

    constructors = []
    requests = []

    class FakeResponses:
        @staticmethod
        def create(**kwargs):
            requests.append(kwargs)
            return [
                SimpleNamespace(type="response.output_text.delta", delta="热"),
                SimpleNamespace(type="response.output_text.delta", delta="切换"),
                SimpleNamespace(type="response.completed", response=SimpleNamespace(output_text="")),
            ]

    class FakeClient:
        def __init__(self, **kwargs):
            constructors.append(kwargs)
            self.responses = FakeResponses()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr("llm.OpenAI", FakeClient)
    generator = ReplyGenerator(_settings())

    assert generator.generate([{"role": "assistant", "content": "你好"}], "在吗") == "热切换"
    _set_current_provider(
        ccswitch_home,
        name="Second Provider",
        category="cn_official",
        model="second-model",
    )
    assert generator.generate([], "在吗") == "热切换"

    assert [request["model"] for request in requests] == ["first-model", "second-model"]
    assert all(request["stream"] is True for request in requests)
    assert all(request["store"] is False for request in requests)
    assert all("max_output_tokens" not in request for request in requests)
    assert requests[0]["input"][-1]["content"][0] == {
        "type": "input_text",
        "text": "在吗",
    }
    assert all(item["api_key"] == "PROXY_MANAGED" for item in constructors)


def test_ccswitch_uses_existing_codex_oauth_only_for_official_provider(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text(
        'model_provider = "cc-switch-official"\nmodel = "official-model"\n'
        '[model_providers.cc-switch-official]\nname = "OpenAI"\n'
        'requires_openai_auth = true\n',
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(
        '{"auth_mode":"chatgpt","tokens":{"access_token":"oauth-secret"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ccswitch_home = tmp_path / "cc-switch"
    monkeypatch.setenv("CCSWITCH_HOME", str(ccswitch_home))
    _set_current_provider(
        ccswitch_home,
        name="OpenAI Official",
        category="official",
        model="",
    )

    assert ReplyGenerator(_settings())._ccswitch_runtime() == (
        "official-model",
        "oauth-secret",
    )


def test_ccswitch_model_override_does_not_require_codex_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    ccswitch_home = tmp_path / "cc-switch"
    monkeypatch.setenv("CCSWITCH_HOME", str(ccswitch_home))
    _set_current_provider(
        ccswitch_home,
        name="Third Party",
        category=None,
        model="",
    )
    settings = _settings()
    object.__setattr__(settings, "llm_model", "manual-model")

    assert ReplyGenerator(settings)._ccswitch_runtime() == (
        "manual-model",
        "PROXY_MANAGED",
    )


def test_lmstudio_uses_configured_model_with_chat_completions(monkeypatch):
    settings = _settings()
    object.__setattr__(settings, "llm_provider", "lmstudio")
    object.__setattr__(settings, "llm_model", "local-model")
    object.__setattr__(settings, "llm_base_url", "http://127.0.0.1:1234/v1")
    object.__setattr__(settings, "llm_api_key", "lm-studio")
    requests = []

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="本地回复"))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.constructor_kwargs = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

        @staticmethod
        def close():
            return None

    monkeypatch.setattr("llm.OpenAI", FakeClient)
    generator = ReplyGenerator(settings)

    assert generator.generate([], "你好") == "本地回复"
    assert requests[0]["model"] == "local-model"
    assert requests[0]["reasoning_effort"] == "none"
    assert requests[0]["messages"][-1] == {"role": "user", "content": "你好"}


def test_lmstudio_includes_retrieved_local_memory_but_other_providers_do_not(monkeypatch, tmp_path):
    memory_path = tmp_path / "memory.jsonl"
    memory_path.write_text('{"text":"今晚一起吃火锅吗"}\n', encoding="utf-8")
    settings = _settings()
    object.__setattr__(settings, "llm_provider", "lmstudio")
    object.__setattr__(settings, "llm_model", "local-model")
    object.__setattr__(settings, "llm_base_url", "http://127.0.0.1:1234/v1")
    object.__setattr__(settings, "llm_api_key", "lm-studio")
    object.__setattr__(settings, "local_memory_enabled", True)
    object.__setattr__(settings, "local_memory_path", memory_path)
    requests = []

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="本地回复"))]
            )

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        @staticmethod
        def close():
            return None

    monkeypatch.setattr("llm.OpenAI", FakeClient)
    assert ReplyGenerator(settings).generate([], "晚上吃火锅吗") == "本地回复"
    assert "今晚一起吃火锅吗" in requests[0]["messages"][0]["content"]

    object.__setattr__(settings, "llm_provider", "ccswitch")
    generator = ReplyGenerator.__new__(ReplyGenerator)
    generator.settings = settings
    generator.persona_prompt = ""
    generator.local_memory = None
    assert generator._memory_context("晚上吃火锅吗") == ""


def test_lmstudio_can_auto_select_first_model(monkeypatch):
    settings = _settings()
    object.__setattr__(settings, "llm_provider", "lmstudio")
    object.__setattr__(settings, "llm_model", "")
    object.__setattr__(settings, "llm_base_url", "http://127.0.0.1:1234/v1")
    object.__setattr__(settings, "llm_api_key", "lm-studio")
    requests = []

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="自动选择"))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())
            self.models = SimpleNamespace(
                list=lambda: SimpleNamespace(data=[SimpleNamespace(id="loaded-model")])
            )

        @staticmethod
        def close():
            return None

    monkeypatch.setattr("llm.OpenAI", FakeClient)
    generator = ReplyGenerator(settings)

    assert generator.generate([], "测试") == "自动选择"
    assert requests[0]["model"] == "loaded-model"


def test_codex_network_check_fails_fast_when_backend_is_unreachable(monkeypatch):
    settings = _settings()
    object.__setattr__(settings, "llm_provider", "codex_cli")
    generator = ReplyGenerator.__new__(ReplyGenerator)
    generator.settings = settings
    generator._codex_network_checked_at = 0.0
    generator._codex_network_error = ""

    def unreachable(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("llm.urllib.request.urlopen", unreachable)

    with pytest.raises(RuntimeError, match="Codex CLI 网络不可用"):
        generator._check_codex_network()


def test_codex_network_check_caches_success(monkeypatch):
    settings = _settings()
    object.__setattr__(settings, "llm_provider", "codex_cli")
    generator = ReplyGenerator.__new__(ReplyGenerator)
    generator.settings = settings
    generator._codex_network_checked_at = 0.0
    generator._codex_network_error = ""
    calls = []

    class Response:
        def __enter__(self):
            calls.append("checked")
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("llm.urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    generator._check_codex_network()
    generator._check_codex_network()

    assert calls == ["checked"]
