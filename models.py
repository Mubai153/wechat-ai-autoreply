from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_name: str
    sender_id: str
    sender_name: str
    content: str
    created_at: datetime
    message_type: str = "文本"
    local_id: int | None = None
    media_path: str = ""

    @property
    def is_text(self) -> bool:
        return self.message_type.strip().casefold() in {"文本", "text", "1"} and bool(
            self.content.strip()
        )

    @property
    def is_image(self) -> bool:
        return self.message_type.strip().casefold() in {"图片", "image", "3"}

    @property
    def is_emoji(self) -> bool:
        return self.message_type.strip().casefold() in {"动画表情", "emoji", "47"}

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
