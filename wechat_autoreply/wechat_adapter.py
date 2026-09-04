from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from config import Settings
from models import IncomingMessage


logger = logging.getLogger(__name__)


def _first(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return default


def _is_outgoing(raw: dict[str, Any]) -> bool:
    """识别当前账号自己发出的消息，防止自动回复形成自我循环。"""
    for key in ("is_self", "is_send", "is_sender", "IsSender"):
        if key not in raw:
            continue
        value = str(raw[key]).strip().casefold()
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False

    # 微信 4.x 数据库中 origin_source=1 为本机发出，=2 为对方发来。
    try:
        return int(raw.get("origin_source", raw.get("originSource", 0))) == 1
    except (TypeError, ValueError):
        return False


class ShardedWeChatDB:
    """让 wechatauto 的监听器合并同一会话分布在多个消息库的记录。

    微信 4.x 会把消息放进多个 message_N.db。部分会话在分片切换后会
    同时出现在两个库中，而适配器原版只取第一个命中的表，导致新消息
    被完全漏读。这里仅覆盖监听器实际使用的两个只读查询接口。
    """

    def __init__(self, db_cls: type) -> None:
        self._db = db_cls()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def _table(self, user: str) -> str:
        return "Msg_" + hashlib.md5(user.encode()).hexdigest()

    def _rows(self, user: str, *, since_seq: int | None = None, limit: int = 200) -> list[Any]:
        table = self._table(user)
        rows: list[Any] = []
        for rel in self._db._message_dbs():
            conn = self._db._open(rel)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                if since_seq is None:
                    query = (
                        f"SELECT local_id, local_type, real_sender_id, create_time, "
                        f"message_content, source, packed_info_data, compress_content, sort_seq, "
                        f"origin_source, status "
                        f"FROM \"{table}\" ORDER BY sort_seq DESC LIMIT ?"
                    )
                    rows.extend(conn.execute(query, (limit,)).fetchall())
                else:
                    query = (
                        f"SELECT local_id, local_type, real_sender_id, create_time, "
                        f"message_content, source, packed_info_data, compress_content, sort_seq, "
                        f"origin_source, status "
                        f"FROM \"{table}\" WHERE sort_seq > ? "
                        f"ORDER BY sort_seq ASC LIMIT ?"
                    )
                    rows.extend(conn.execute(query, (since_seq, limit)).fetchall())
            finally:
                conn.close()

        if since_seq is None:
            rows.sort(key=lambda row: row["sort_seq"], reverse=True)
        else:
            rows.sort(key=lambda row: row["sort_seq"])
        return rows[:limit]

    def get_messages(self, user: str, limit: int = 20, offset: int = 0) -> list[dict]:
        rows = self._rows(user, limit=max(limit + offset, limit))
        return [self._to_dict(row) for row in rows[offset:offset + limit]]

    def get_new_messages(self, user: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        rows = self._rows(user, since_seq=since_seq, limit=limit)
        return [self._to_dict(row) for row in rows]

    def _to_dict(self, row: Any) -> dict[str, Any]:
        message = self._db._msg_row_to_dict(row)
        message["origin_source"] = row["origin_source"]
        message["status"] = row["status"]
        return message


class WeChatAdapter:
    """Thin wrapper around wechatauto-replica's documented DB/listener/send APIs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            from wechatauto import WeChatDB
            from wechatauto.db import Listener
            from wechatauto.guia import quick_send
        except ImportError as exc:
            raise RuntimeError(
                "未安装微信 4.x 适配器。请先运行 install_wechat_adapter.ps1，"
                "并确认已审查第三方源码。"
            ) from exc

        self._WeChatDB = WeChatDB
        self._Listener = Listener
        self._quick_send = quick_send
        self.db = ShardedWeChatDB(WeChatDB)
        self.target_username = self._resolve_target()
        self.listener = Listener(self.db, interval=1.0)

    def _resolve_target(self) -> str:
        matches = self.db.search_contact(self.settings.wechat_target)
        if isinstance(matches, dict):
            matches = [matches]
        matches = list(matches or [])
        if not matches:
            raise RuntimeError(f"找不到联系人：{self.settings.wechat_target}")

        wanted = self.settings.wechat_target.casefold()
        exact = []
        for item in matches:
            if not isinstance(item, dict):
                continue
            username = _first(item, "username", "UserName", "wxid", "user_name")
            names = {
                _first(item, "nickname", "nick_name", "NickName", "name").casefold(),
                _first(item, "remark", "RemarkName", "remark_name").casefold(),
                _first(item, "display_name", "displayName").casefold(),
            }
            if wanted in names:
                exact.append(username)

        candidates = [item for item in exact if item]
        if len(candidates) == 1:
            return candidates[0]
        if len(matches) == 1:
            item = matches[0]
            username = _first(item, "username", "UserName", "wxid", "user_name")
            if username:
                return username
        raise RuntimeError(
            f"联系人匹配不唯一：{self.settings.wechat_target}。"
            "请改用唯一备注名，或在适配器中指定 wxid。"
        )

    def _normalize(self, raw: dict[str, Any]) -> IncomingMessage | None:
        content = _first(raw, "content", "Content", "text", "msg", "message")
        if not content:
            return None
        chat_id = _first(raw, "chat_id", "talker_id", "username", "UserName", default=self.target_username)
        chat_name = _first(raw, "chat_name", "talker_name", "nickname", "nick_name", "NickName", default=self.settings.wechat_target)
        sender_id = _first(raw, "sender_id", "sender_username", "from_user", "FromUserName", default=chat_id)
        sender_name = _first(raw, "sender_name", "sender_username", "from_name", "nickname", "nick_name", "NickName", default=chat_name)
        local_id = _first(raw, "local_id", "msg_id", "MsgId")
        created = _first(raw, "create_time", "timestamp", "CreateTime")
        sort_seq = _first(raw, "sort_seq", "SortSeq")
        # local_id 在不同 message_N.db 分片中可能重复，优先使用会话+排序序号。
        message_id = (
            f"{chat_id}:{sort_seq}"
            if sort_seq
            else (f"{chat_id}:{local_id}" if local_id else f"{chat_id}:{created}:{content}")
        )
        try:
            created_at = datetime.fromtimestamp(float(created), tz=timezone.utc) if created else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            created_at = datetime.now(timezone.utc)
        return IncomingMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            created_at=created_at,
            message_type=_first(raw, "type", "msg_type", default="文本"),
        )

    def listen(self, callback: Callable[[IncomingMessage], None]) -> None:
        def on_raw(raw: dict[str, Any], _listener: Any) -> None:
            if not isinstance(raw, dict):
                logger.debug("忽略非字典消息：%r", raw)
                return
            if _is_outgoing(raw):
                logger.debug(
                    "忽略本机发出的消息：local_id=%s, sort_seq=%s",
                    _first(raw, "local_id", "msg_id", "MsgId", default="unknown"),
                    _first(raw, "sort_seq", "SortSeq", default="unknown"),
                )
                return
            message = self._normalize(raw)
            if message is not None:
                logger.info(
                    "收到消息：type=%s, local_id=%s, sender=%s, content_length=%s",
                    _first(raw, "type", "msg_type", default="unknown"),
                    _first(raw, "local_id", "msg_id", "MsgId", default="unknown"),
                    message.sender_name,
                    len(message.content),
                )
                callback(message)

        self.listener.add_listener(self.target_username, on_raw)
        logger.info("开始监听联系人：%s (%s)", self.settings.wechat_target, self.target_username)
        self.listener.start()

    def stop(self) -> None:
        self.listener.stop()

    def send_text(self, text: str) -> None:
        # 上游 verify=True 只按 real_sender_id==2 判断自己发送，但该数字会随
        # message_N.db 分片变化。本项目改用稳定的 origin_source=1 自行回读。
        result = self._quick_send(text, self.target_username, verify=False)
        failed = (
            result.get("status") != "成功"
            if isinstance(result, dict) and "status" in result
            else not result
        )
        if failed:
            detail = result.get("message", "未知错误") if isinstance(result, dict) else str(result)
            raise RuntimeError(f"微信发送失败：{detail}")

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            messages = self.db.get_messages(self.target_username, limit=5)
            if any(
                _is_outgoing(message) and (message.get("content") or "").strip() == text.strip()
                for message in messages
            ):
                return
            time.sleep(0.5)
        raise RuntimeError("微信已执行发送操作，但数据库未确认该消息")
