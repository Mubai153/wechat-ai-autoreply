from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# 开发模式使用源码目录；打包成 exe 后使用 exe 所在目录，确保旁边的
# .env、data 和 Codex 配置仍然可以被找到。
PROJECT_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
load_dotenv(PROJECT_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_model: str
    codex_command: str
    codex_timeout_seconds: int
    llm_base_url: str
    llm_api_key: str
    wechat_target: str
    auto_send: bool
    reply_cooldown_seconds: int
    max_history_messages: int
    max_reply_chars: int
    max_input_chars: int
    database_path: Path
    system_prompt: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(os.getenv("DATABASE_PATH", "data/wechat_autoreply.sqlite3"))
        if not database_path.is_absolute():
            database_path = PROJECT_DIR / database_path

        return cls(
            llm_provider=os.getenv("LLM_PROVIDER", "codex_cli").strip().lower(),
            llm_model=os.getenv("CODEX_MODEL", os.getenv("LLM_MODEL", "")).strip(),
            codex_command=os.getenv("CODEX_COMMAND", "codex").strip(),
            codex_timeout_seconds=max(10, _int("CODEX_TIMEOUT_SECONDS", 120)),
            llm_base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            wechat_target=os.getenv("WECHAT_TARGET", "").strip(),
            auto_send=_bool("AUTO_SEND", False),
            reply_cooldown_seconds=max(0, _int("REPLY_COOLDOWN_SECONDS", 0)),
            max_history_messages=max(0, _int("MAX_HISTORY_MESSAGES", 12)),
            max_reply_chars=max(1, _int("MAX_REPLY_CHARS", 500)),
            max_input_chars=max(1, _int("MAX_INPUT_CHARS", 4000)),
            database_path=database_path,
            system_prompt=os.getenv(
                "SYSTEM_PROMPT",
                "你是我的微信聊天助手。用自然、简洁、友好的中文回复。",
            ).strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def validate(self) -> None:
        missing = []
        if self.llm_provider not in {"codex_cli", "openai_compatible"}:
            raise ValueError("LLM_PROVIDER 只能是 codex_cli 或 openai_compatible")
        if self.llm_provider == "openai_compatible" and not self.llm_base_url:
            missing.append("LLM_BASE_URL")
        if self.llm_provider == "openai_compatible" and not self.llm_model:
            missing.append("LLM_MODEL")
        if self.llm_provider == "openai_compatible" and not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if not self.wechat_target:
            missing.append("WECHAT_TARGET")
        if missing:
            raise ValueError("缺少配置：" + ", ".join(missing))
