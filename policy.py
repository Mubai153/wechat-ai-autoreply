from __future__ import annotations

from datetime import datetime, timezone

from config import Settings
from models import IncomingMessage
from storage import Storage


def is_target(message: IncomingMessage, target: str) -> bool:
    wanted = target.strip().casefold()
    return wanted in {
        message.chat_id.strip().casefold(),
        message.chat_name.strip().casefold(),
        message.sender_id.strip().casefold(),
        message.sender_name.strip().casefold(),
    }


def should_reply(
    message: IncomingMessage,
    settings: Settings,
    storage: Storage,
) -> tuple[bool, str]:
    if not message.is_text:
        return False, "非文本消息"
    if len(message.content) > settings.max_input_chars:
        return False, "消息过长"
    if storage.was_processed(message.message_id):
        return False, "消息已处理"

    last_reply = storage.last_assistant_at(message.chat_id)
    if last_reply is not None:
        elapsed = (datetime.now(timezone.utc) - last_reply).total_seconds()
        if elapsed < settings.reply_cooldown_seconds:
            return False, f"冷却中（还需 {settings.reply_cooldown_seconds - int(elapsed)} 秒）"
    return True, "通过"

