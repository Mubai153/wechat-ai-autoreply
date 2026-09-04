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

    @property
    def is_text(self) -> bool:
        return self.message_type.strip().casefold() in {"文本", "text", "1"} and bool(
            self.content.strip()
        )

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
