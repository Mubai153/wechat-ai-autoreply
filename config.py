from __future__ import annotations

import os
import re
import shutil
import sys
import json
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from urllib.parse import urlparse

from dotenv import load_dotenv, set_key


# 开发模式使用源码目录；打包成 exe 后使用 exe 所在目录，确保旁边的
# .env、data 和 Codex 配置仍然可以被找到。
PROJECT_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
ENV_PATH = PROJECT_DIR / ".env"
REPLY_PROFILES_PATH = PROJECT_DIR / "data" / "reply_profiles.json"
_ENV_WRITE_LOCK = Lock()
_PROFILE_WRITE_LOCK = Lock()


@dataclass(frozen=True)
class ReplyProfile:
    """一套可独立分配给联系人的回复规则。"""

    name: str
    system_prompt: str
    reply_cooldown_seconds: int
    max_history_messages: int
    max_reply_chars: int
    max_input_chars: int
    persona_path: Path | None = None


def _profile_path(value: str) -> Path | None:
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else PROJECT_DIR / path


def load_reply_profiles(default_profile: ReplyProfile) -> tuple[tuple[ReplyProfile, ...], tuple[tuple[str, str], ...]]:
    """读取方案库；文件不存在时无感兼容旧版 .env 配置。"""
    if not REPLY_PROFILES_PATH.exists():
        return (default_profile,), ()
    try:
        payload = json.loads(REPLY_PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 不能因为一个可选的界面配置文件阻止既有自动回复继续工作。
        return (default_profile,), ()
    entries = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return (default_profile,), ()
    profiles: list[ReplyProfile] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        try:
            profile = ReplyProfile(
                name=name,
                system_prompt=str(item.get("system_prompt", default_profile.system_prompt)).strip(),
                reply_cooldown_seconds=max(0, int(item.get("reply_cooldown_seconds", default_profile.reply_cooldown_seconds))),
                max_history_messages=max(0, int(item.get("max_history_messages", default_profile.max_history_messages))),
                max_reply_chars=max(1, int(item.get("max_reply_chars", default_profile.max_reply_chars))),
                max_input_chars=max(1, int(item.get("max_input_chars", default_profile.max_input_chars))),
                persona_path=_profile_path(str(item.get("persona_path", ""))),
            )
        except (TypeError, ValueError):
            continue
        profiles.append(profile)
        seen.add(key)
    if not profiles:
        profiles.append(default_profile)
        seen.add(default_profile.name.casefold())
    raw_assignments = payload.get("assignments", {}) if isinstance(payload, dict) else {}
    assignments: list[tuple[str, str]] = []
    if isinstance(raw_assignments, dict):
        for contact, profile_name in raw_assignments.items():
            contact = str(contact).strip()
            profile_name = str(profile_name).strip()
            if contact and profile_name.casefold() in seen:
                assignments.append((contact, profile_name))
    return tuple(profiles), tuple(assignments)


def save_reply_profiles(profiles: tuple[ReplyProfile, ...], assignments: dict[str, str]) -> None:
    """原子保存方案和联系人分配，避免中断时损坏已有方案库。"""
    if not profiles:
        raise ValueError("至少保留一个回复方案")
    valid_names = {profile.name.casefold() for profile in profiles}
    payload = {
        "version": 1,
        "profiles": [
            {
                "name": profile.name,
                "system_prompt": profile.system_prompt,
                "reply_cooldown_seconds": profile.reply_cooldown_seconds,
                "max_history_messages": profile.max_history_messages,
                "max_reply_chars": profile.max_reply_chars,
                "max_input_chars": profile.max_input_chars,
                "persona_path": str(profile.persona_path or ""),
            }
            for profile in profiles
        ],
        "assignments": {
            contact: name for contact, name in assignments.items()
            if contact.strip() and name.casefold() in valid_names
        },
    }
    REPLY_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PROFILE_WRITE_LOCK:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".reply_profiles.", suffix=".tmp",
            dir=REPLY_PROFILES_PATH.parent, delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            json.dump(payload, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
        try:
            os.replace(temp_path, REPLY_PROFILES_PATH)
        finally:
            temp_path.unlink(missing_ok=True)


def load_project_env() -> None:
    """Load the persisted project settings as the source of truth.

    ``load_dotenv`` normally keeps values already present in ``os.environ``.
    That is useful for development, but it can make an old inherited
    ``SYSTEM_PROMPT`` win over the value saved in ``.env`` on the next launch.
    Settings written by the app must take precedence, so startup always loads
    the project file with ``override=True``.
    """
    load_dotenv(ENV_PATH, override=True)


def save_project_env(updates: dict[str, str]) -> None:
    """Persist GUI settings safely and refresh the current process.

    The file is first copied to a temporary file and all keys are updated
    there.  Replacing the original only after every update succeeds prevents a
    partially written configuration if the process is interrupted while
    saving (especially important for the multiline ``SYSTEM_PROMPT``).
    """
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _ENV_WRITE_LOCK:
        temp_name: str | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{ENV_PATH.name}.",
                suffix=".tmp",
                dir=ENV_PATH.parent,
                delete=False,
            ) as temp:
                temp_name = temp.name

            temp_path = Path(temp_name)
            if ENV_PATH.exists():
                shutil.copyfile(ENV_PATH, temp_path)
            else:
                example = PROJECT_DIR / ".env.example"
                if example.exists():
                    shutil.copyfile(example, temp_path)

            for env_key, value in updates.items():
                result = set_key(str(temp_path), env_key, value, quote_mode="auto")
                if result is None:
                    raise OSError(f"无法写入配置项 {env_key}")

            os.replace(temp_path, ENV_PATH)
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    load_project_env()


load_project_env()


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
    # 放在末尾并提供默认值，保持外部脚本直接构造 Settings 的兼容性。
    wechat_background_mode: bool = True
    wechat_allow_mouse_fallback: bool = False
    # 可选的离线语气画像；为空时保持原有行为。
    persona_path: Path | None = None
    image_recognition_enabled: bool = False
    media_retention_days: int = 7
    media_cache_max_mb: int = 512
    media_cleanup_interval_seconds: int = 3600
    # 仅 LM Studio 可使用的离线聊天记忆；其他提供方绝不加载或发送它。
    local_memory_enabled: bool = False
    local_memory_path: Path | None = None
    local_memory_max_results: int = 6
    local_memory_max_chars: int = 800
    # 新配置优先使用 WECHAT_TARGETS；保留 wechat_target 兼容旧版调用方。
    wechat_targets: tuple[str, ...] = ()
    # 独立方案保存在 data/reply_profiles.json，不与包含凭据的 .env 混放。
    reply_profiles: tuple[ReplyProfile, ...] = ()
    contact_profile_assignments: tuple[tuple[str, str], ...] = ()

    @property
    def target_contacts(self) -> tuple[str, ...]:
        """返回去重后的目标联系人，最多由 validate() 校验为三个。"""
        if self.wechat_targets:
            return self.wechat_targets
        return (self.wechat_target,) if self.wechat_target.strip() else ()

    @property
    def default_reply_profile(self) -> ReplyProfile:
        if self.reply_profiles:
            return self.reply_profiles[0]
        return ReplyProfile(
            name="默认方案",
            system_prompt=self.system_prompt,
            reply_cooldown_seconds=self.reply_cooldown_seconds,
            max_history_messages=self.max_history_messages,
            max_reply_chars=self.max_reply_chars,
            max_input_chars=self.max_input_chars,
            persona_path=self.persona_path,
        )

    def reply_profile_named(self, name: str | None) -> ReplyProfile:
        wanted = (name or "").strip().casefold()
        for profile in self.reply_profiles:
            if profile.name.casefold() == wanted:
                return profile
        return self.default_reply_profile

    def reply_profile_for(self, *identifiers: str) -> ReplyProfile:
        """按会话的任一标识匹配方案，未分配时回退默认方案。"""
        assignments = {
            contact.strip().casefold(): profile_name
            for contact, profile_name in self.contact_profile_assignments
            if contact.strip()
        }
        for identifier in identifiers:
            profile_name = assignments.get((identifier or "").strip().casefold())
            if profile_name:
                return self.reply_profile_named(profile_name)
        return self.default_reply_profile

    def with_reply_profile(self, profile: ReplyProfile) -> "Settings":
        """返回仅替换回复规则的运行时设置，其他连接与安全设置保持不变。"""
        return replace(
            self,
            system_prompt=profile.system_prompt,
            reply_cooldown_seconds=profile.reply_cooldown_seconds,
            max_history_messages=profile.max_history_messages,
            max_reply_chars=profile.max_reply_chars,
            max_input_chars=profile.max_input_chars,
            persona_path=profile.persona_path,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        llm_provider = os.getenv("LLM_PROVIDER", "ccswitch").strip().lower()
        if llm_provider == "ccswitch":
            llm_model = os.getenv("CCSWITCH_MODEL", "").strip()
            llm_base_url = (
                os.getenv("CCSWITCH_BASE_URL", "http://127.0.0.1:15721/v1").strip()
                or "http://127.0.0.1:15721/v1"
            ).rstrip("/")
            # CC Switch owns upstream credentials. A per-request local placeholder
            # (or the existing Codex OAuth token for OpenAI Official) is selected
            # by ReplyGenerator instead of being persisted in this project.
            llm_api_key = ""
        elif llm_provider == "lmstudio":
            llm_model = os.getenv("LMSTUDIO_MODEL", "").strip()
            llm_base_url = (
                os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").strip()
                or "http://127.0.0.1:1234/v1"
            ).rstrip("/")
            # LM Studio does not require a real API key, but the OpenAI SDK
            # requires a non-empty value when constructing its client.
            llm_api_key = os.getenv("LMSTUDIO_API_KEY", "lm-studio").strip() or "lm-studio"
        elif llm_provider == "openai_compatible":
            llm_model = os.getenv("LLM_MODEL", "").strip()
            llm_base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
            llm_api_key = os.getenv("LLM_API_KEY", "").strip()
        else:
            llm_model = os.getenv("CODEX_MODEL", "").strip()
            llm_base_url = ""
            llm_api_key = ""

        database_path = Path(os.getenv("DATABASE_PATH", "data/wechat_autoreply.sqlite3"))
        if not database_path.is_absolute():
            database_path = PROJECT_DIR / database_path

        persona_raw = os.getenv("PERSONA_PATH", "").strip()
        persona_path = None
        if persona_raw:
            persona_path = Path(persona_raw)
            if not persona_path.is_absolute():
                persona_path = PROJECT_DIR / persona_path

        memory_raw = os.getenv("LOCAL_MEMORY_PATH", "").strip()
        memory_path = None
        if memory_raw:
            memory_path = Path(memory_raw)
            if not memory_path.is_absolute():
                memory_path = PROJECT_DIR / memory_path

        legacy_target = os.getenv("WECHAT_TARGET", "").strip()
        targets_raw = os.getenv("WECHAT_TARGETS", "")
        targets: list[str] = []
        seen: set[str] = set()
        for target in re.split(r"[,，;；\r\n]+", targets_raw):
            target = target.strip()
            key = target.casefold()
            if target and key not in seen:
                targets.append(target)
                seen.add(key)
        if not targets and legacy_target:
            targets.append(legacy_target)

        legacy_system_prompt = os.getenv(
            "SYSTEM_PROMPT",
            "你是我的微信聊天助手。用自然、简洁、友好的中文回复。",
        ).strip()
        legacy_persona_path = persona_path
        legacy_profile = ReplyProfile(
            name="默认方案",
            system_prompt=legacy_system_prompt,
            reply_cooldown_seconds=max(0, _int("REPLY_COOLDOWN_SECONDS", 0)),
            max_history_messages=max(0, _int("MAX_HISTORY_MESSAGES", 100)),
            max_reply_chars=max(1, _int("MAX_REPLY_CHARS", 500)),
            max_input_chars=max(1, _int("MAX_INPUT_CHARS", 4000)),
            persona_path=legacy_persona_path,
        )
        profiles, assignments = load_reply_profiles(legacy_profile)
        default_profile = profiles[0]

        return cls(
            llm_provider=llm_provider,
            llm_model=llm_model,
            codex_command=os.getenv("CODEX_COMMAND", "codex").strip(),
            codex_timeout_seconds=max(10, _int("CODEX_TIMEOUT_SECONDS", 120)),
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            wechat_target=targets[0] if targets else legacy_target,
            auto_send=_bool("AUTO_SEND", False),
            reply_cooldown_seconds=default_profile.reply_cooldown_seconds,
            max_history_messages=default_profile.max_history_messages,
            max_reply_chars=default_profile.max_reply_chars,
            max_input_chars=default_profile.max_input_chars,
            database_path=database_path,
            system_prompt=default_profile.system_prompt,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            wechat_background_mode=_bool("WECHAT_BACKGROUND_MODE", True),
            wechat_allow_mouse_fallback=_bool("WECHAT_ALLOW_MOUSE_FALLBACK", False),
            persona_path=default_profile.persona_path,
            image_recognition_enabled=_bool("IMAGE_RECOGNITION_ENABLED", False),
            media_retention_days=max(0, _int("MEDIA_RETENTION_DAYS", 7)),
            media_cache_max_mb=max(0, _int("MEDIA_CACHE_MAX_MB", 512)),
            media_cleanup_interval_seconds=max(60, _int("MEDIA_CLEANUP_INTERVAL_SECONDS", 3600)),
            local_memory_enabled=_bool("LOCAL_MEMORY_ENABLED", False),
            local_memory_path=memory_path,
            local_memory_max_results=max(1, _int("LOCAL_MEMORY_MAX_RESULTS", 6)),
            local_memory_max_chars=max(100, _int("LOCAL_MEMORY_MAX_CHARS", 800)),
            wechat_targets=tuple(targets),
            reply_profiles=profiles,
            contact_profile_assignments=assignments,
        )

    def validate(self) -> None:
        missing = []
        if self.llm_provider not in {"ccswitch", "codex_cli", "lmstudio", "openai_compatible"}:
            raise ValueError(
                "LLM_PROVIDER 只能是 ccswitch、codex_cli、lmstudio 或 openai_compatible"
            )
        if self.llm_provider == "ccswitch" and not self.llm_base_url:
            missing.append("CCSWITCH_BASE_URL")
        if self.llm_provider == "ccswitch" and self.llm_base_url:
            route = urlparse(self.llm_base_url)
            if route.scheme not in {"http", "https"} or route.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("CCSWITCH_BASE_URL 必须是本机回环地址")
        if self.llm_provider == "openai_compatible" and not self.llm_base_url:
            missing.append("LLM_BASE_URL")
        if self.llm_provider == "openai_compatible" and not self.llm_model:
            missing.append("LLM_MODEL")
        if self.llm_provider == "openai_compatible" and not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if self.llm_provider == "lmstudio" and not self.llm_base_url:
            missing.append("LMSTUDIO_BASE_URL")
        if self.llm_provider == "lmstudio" and self.llm_base_url:
            route = urlparse(self.llm_base_url)
            if route.scheme not in {"http", "https"} or route.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError("LMSTUDIO_BASE_URL 必须是本机回环地址")
        if self.llm_provider == "lmstudio" and self.local_memory_enabled:
            if self.local_memory_path is None:
                missing.append("LOCAL_MEMORY_PATH")
            elif not self.local_memory_path.is_file():
                raise ValueError(f"找不到本地聊天记忆文件：{self.local_memory_path}")
        targets = self.target_contacts
        if not targets:
            missing.append("WECHAT_TARGETS（或 WECHAT_TARGET）")
        elif len(targets) > 3:
            raise ValueError("WECHAT_TARGETS 最多支持 3 个联系人")
        if missing:
            raise ValueError("缺少配置：" + ", ".join(missing))
