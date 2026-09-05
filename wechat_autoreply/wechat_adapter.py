from __future__ import annotations

import hashlib
import logging
import threading
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


def _is_outgoing(
    raw: dict[str, Any],
    *,
    self_ids: set[str] | None = None,
) -> bool:
    """识别当前账号自己发出的消息，防止自动回复形成自我循环。

    ``origin_source`` 表示消息来源渠道，并不是收发方向；不同版本的
    微信会给它写入 1、2、3、5 等多个值。微信 4.x 的消息表使用
    ``real_sender_id=2`` 表示当前账号；因此只使用发送者字段判断，
    不根据 ``origin_source`` 猜测方向。
    """
    explicit_false = False
    for key in ("is_self", "is_send", "is_sender", "IsSender"):
        if key not in raw:
            continue
        value = str(raw[key]).strip().casefold()
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            explicit_false = True

    # 适配器原始消息通常来自 real_sender_id，转换后为 sender_id。
    # 2 是 wechatauto 对当前账号的稳定标识，与消息库分片无关。
    sender_keys = ("sender_id", "real_sender_id", "senderId", "realSenderId")
    has_sender = False
    for key in sender_keys:
        if key not in raw or raw[key] is None or not str(raw[key]).strip():
            continue
        has_sender = True
        value = str(raw[key]).strip().casefold()
        if value in {"2", "self", "me", "我"}:
            return True
        if self_ids and value in self_ids:
            return True

    # 某些上游对象只保留 sender_username/from_user；若能提供当前账号
    # 的微信号，也可以在没有数字 sender_id 时准确识别自己。
    if self_ids:
        for key in ("sender_username", "from_user", "FromUserName", "username"):
            value = str(raw.get(key, "")).strip().casefold()
            if value and value in self_ids:
                return True

    if explicit_false:
        return False

    # 旧版调用方可能只传 origin_source；仅在缺少发送者字段时保留该
    # 兼容路径。当前适配器返回 sender_id，因此不会走到这里。
    if has_sender:
        return False
    try:
        return int(raw.get("origin_source", raw.get("originSource", 0))) == 1
    except (TypeError, ValueError):
        return False


def _is_ai_reply(content: str) -> bool:
    """自动回复都由发送层统一加前缀，以此与用户手动发言区分。"""
    return content.lstrip().startswith(("AI：", "AI:"))


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

    def get_message_row(self, user: str, local_id: int) -> dict[str, Any] | None:
        """跨所有 message_N.db 分片读取媒体下载所需的完整消息行。"""
        table = self._table(user)
        for rel in self._db._message_dbs():
            conn = self._db._open(rel)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                row = conn.execute(
                    f"SELECT local_id, local_type, server_id, real_sender_id, create_time, "
                    f"message_content, source, packed_info_data, compress_content, sort_seq "
                    f"FROM \"{table}\" WHERE local_id=? LIMIT 1",
                    (int(local_id),),
                ).fetchone()
                if row:
                    return {
                        "local_id": row["local_id"],
                        "local_type": row["local_type"],
                        "server_id": row["server_id"],
                        "sender_id": row["real_sender_id"],
                        "create_time": row["create_time"],
                        "content": row["message_content"],
                        "source": row["source"],
                        "packed_info": row["packed_info_data"],
                        "compress_content": row["compress_content"],
                        "sort_seq": row["sort_seq"],
                    }
            finally:
                conn.close()
        return None

    def get_new_messages(self, user: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        rows = self._rows(user, since_seq=since_seq, limit=limit)
        return [self._to_dict(row) for row in rows]

    def _to_dict(self, row: Any) -> dict[str, Any]:
        message = self._db._msg_row_to_dict(row)
        message["local_type"] = row["local_type"]
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
        self._background_sender = None
        self._background_senders: dict[str, Any] = {}
        self._send_lock = threading.RLock()
        self.db = ShardedWeChatDB(WeChatDB)
        self._self_ids: set[str] = set()
        # sender_id=2 是主判定；微信号作为部分上游版本的备用身份字段。
        self_wxid = getattr(self.db, "wxid", "")
        if self_wxid:
            self._self_ids.add(str(self_wxid).strip().casefold())
        try:
            self_info = self.db.get_self_info()
        except Exception:
            self_info = {}
        if isinstance(self_info, dict):
            for key in ("username", "wxid", "user_name"):
                value = self_info.get(key)
                if value:
                    self._self_ids.add(str(value).strip().casefold())
        self.target_usernames: dict[str, str] = {}
        for target in settings.target_contacts:
            username = self._resolve_target(target)
            if username in self.target_usernames.values():
                raise RuntimeError(f"联系人配置重复或指向同一会话：{target}")
            self.target_usernames[target] = username
        self.target_username = next(iter(self.target_usernames.values()), "")
        self.listener = Listener(self.db, interval=1.0)

    def _resolve_target(self, target: str | None = None) -> str:
        target = target or self.settings.wechat_target
        matches = self.db.search_contact(target)
        if isinstance(matches, dict):
            matches = [matches]
        matches = list(matches or [])
        if not matches:
            raise RuntimeError(f"找不到联系人：{target}")

        wanted = target.casefold()
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
            f"联系人匹配不唯一：{target}。"
            "请改用唯一备注名，或在适配器中指定 wxid。"
        )

    def _normalize(
        self,
        raw: dict[str, Any],
        *,
        default_chat_id: str | None = None,
        default_chat_name: str | None = None,
    ) -> IncomingMessage | None:
        local_type = raw.get("local_type", raw.get("localType"))
        try:
            local_type = int(local_type) if local_type is not None else None
        except (TypeError, ValueError):
            local_type = None
        content = _first(raw, "content", "Content", "text", "msg", "message")
        if local_type == 3:
            content = "[图片]"
        elif local_type == 47:
            content = "[动画表情]"
        if not content:
            return None
        chat_id = _first(raw, "chat_id", "talker_id", "username", "UserName", default=default_chat_id or self.target_username)
        chat_name = _first(raw, "chat_name", "talker_name", "nickname", "nick_name", "NickName", default=default_chat_name or self.settings.wechat_target)
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
        if local_type == 3:
            message_type = "图片"
        elif local_type == 47:
            message_type = "动画表情"
        elif local_type == 1:
            message_type = "文本"
        else:
            message_type = _first(raw, "type", "msg_type", default="文本")
        return IncomingMessage(
            message_id=message_id,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            created_at=created_at,
            message_type=message_type,
            local_id=int(local_id) if local_id.isdigit() else None,
        )

    def listen(self, callback: Callable[[IncomingMessage], None]) -> None:
        def on_raw(
            raw: dict[str, Any],
            _listener: Any,
            target_username: str,
            target_name: str,
        ) -> None:
            if not isinstance(raw, dict):
                logger.debug("忽略非字典消息：%r", raw)
                return
            if _is_outgoing(raw, self_ids=getattr(self, "_self_ids", None)):
                logger.debug(
                    "忽略本机发出的消息：local_id=%s, sort_seq=%s",
                    _first(raw, "local_id", "msg_id", "MsgId", default="unknown"),
                    _first(raw, "sort_seq", "SortSeq", default="unknown"),
                )
                return
            message = self._normalize(
                raw,
                default_chat_id=target_username,
                default_chat_name=target_name,
            )
            if message is not None:
                logger.info(
                    "收到消息：type=%s, local_id=%s, sender=%s, content_length=%s",
                    _first(raw, "type", "msg_type", default="unknown"),
                    _first(raw, "local_id", "msg_id", "MsgId", default="unknown"),
                    message.sender_name,
                    len(message.content),
                )
                callback(message)

        for target_name, target_username in self.target_usernames.items():
            self.listener.add_listener(
                target_username,
                lambda raw, listener, username=target_username, name=target_name: on_raw(
                    raw, listener, username, name
                ),
            )
            logger.info("开始监听联系人：%s (%s)", target_name, target_username)
        self.listener.start()

    def target_username_for(
        self,
        chat_id: str | None = None,
        chat_name: str | None = None,
    ) -> str | None:
        """把消息中的会话标识映射到已配置的微信 username。"""
        usernames = getattr(self, "target_usernames", {})
        if not usernames:
            return getattr(self, "target_username", None)
        by_id = {value.casefold(): value for value in usernames.values()}
        if chat_id and chat_id.strip().casefold() in by_id:
            return by_id[chat_id.strip().casefold()]
        by_name = {key.casefold(): value for key, value in usernames.items()}
        if chat_name and chat_name.strip().casefold() in by_name:
            return by_name[chat_name.strip().casefold()]
        return None

    def recent_history(
        self,
        limit: int = 100,
        *,
        exclude_message_id: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, str]]:
        """读取最近的真实双方对话，按时间正序返回给模型。

        对方消息和用户手动发出的消息都保留，仅过滤带
        ``AI：`` / ``AI:`` 前缀的程序自动回复。
        数据库是“最新在前”，因此先向前分页收集足够的
        对方消息，再反转成模型需要的时间正序。
        """
        if limit <= 0:
            return []

        username = chat_id or self.target_username

        newest_first: list[dict[str, str]] = []
        offset = 0
        batch_size = max(100, limit)
        while len(newest_first) < limit:
            raw_messages = self.db.get_messages(
                username,
                limit=batch_size,
                offset=offset,
            )
            if not raw_messages:
                break
            for raw in raw_messages:
                if not isinstance(raw, dict):
                    continue
                message = self._normalize(raw)
                if message is None or message.message_id == exclude_message_id:
                    continue
                content = message.content.strip()
                outgoing = _is_outgoing(raw, self_ids=getattr(self, "_self_ids", None))
                if outgoing and _is_ai_reply(content):
                    continue
                if message.is_image:
                    content = "[图片]"
                elif message.is_emoji:
                    content = "[动画表情]"
                if not content:
                    continue
                newest_first.append(
                    {
                        "role": "assistant" if outgoing else "user",
                        "content": content,
                    }
                )
                if len(newest_first) >= limit:
                    break
            offset += len(raw_messages)
            if len(raw_messages) < batch_size:
                break

        return list(reversed(newest_first))

    def stop(self) -> None:
        self.listener.stop()

    def send_to(self, text: str, *, chat_id: str, chat_name: str = "") -> None:
        """把消息发送到指定会话，避免多联系人模式下串发。"""
        self.send_text(text, chat_id=chat_id, chat_name=chat_name)

    def send_text(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        chat_name: str | None = None,
    ) -> None:
        target_username = self.target_username_for(chat_id, chat_name) or self.target_username
        target_name = next(
            (name for name, username in getattr(self, "target_usernames", {}).items() if username == target_username),
            getattr(getattr(self, "settings", None), "wechat_target", ""),
        )
        lock = getattr(self, "_send_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._send_lock = lock
        with lock:
            self._send_text_locked(text, target_username, target_name)

    def _send_text_locked(self, text: str, target_username: str, target_name: str) -> None:
        # 默认使用项目内的无鼠标 UIA 路径。只有显式允许时才回退到上游
        # 坐标/OCR 方案；这样 UIA 不可用时会快速报错，不会突然抢鼠标。
        settings = getattr(self, "settings", None)
        background_mode = getattr(settings, "wechat_background_mode", False)
        allow_mouse_fallback = getattr(settings, "wechat_allow_mouse_fallback", True)
        if background_mode:
            senders = getattr(self, "_background_senders", None)
            if senders is None:
                senders = {}
                self._background_senders = senders
            sender = senders.get(target_username)
            if sender is None:
                from wechat_autoreply.background_sender import BackgroundWeChatSender

                # UIA 搜索框不识别内部 wxid；直接使用用户配置的唯一备注名。
                sender = BackgroundWeChatSender(target_name)
                senders[target_username] = sender
                if target_username == getattr(self, "target_username", ""):
                    self._background_sender = sender
                logger.info(
                    "已初始化后台 UIA 发送器（使用备注名 %s，不使用鼠标点击）",
                    target_name,
                )
            try:
                result = sender.send_text(text)
            except Exception:
                if not allow_mouse_fallback:
                    raise
                logger.warning("后台 UIA 发送失败，按配置回退坐标/OCR 发送", exc_info=True)
                result = self._quick_send(text, target_username, verify=False)
        else:
            result = self._quick_send(text, target_username, verify=False)

        # 上游 verify=True 只按 real_sender_id==2 判断自己发送；这里复用
        # 同一发送者判定逻辑，避免把对方消息误当成发送确认。
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
            messages = self.db.get_messages(target_username, limit=5)
            if any(
                _is_outgoing(message) and (message.get("content") or "").strip() == text.strip()
                for message in messages
            ):
                return
            time.sleep(0.5)
        raise RuntimeError("微信已执行发送操作，但数据库未确认该消息")

    def download_image(
        self,
        local_id: int,
        save_dir: str,
        *,
        chat_id: str | None = None,
    ) -> str | None:
        """读取并解密一条图片消息，失败时返回 None。"""
        from wechatauto.media import MediaDownloader

        downloader = MediaDownloader(self.db, save_dir=save_dir)
        username = chat_id or self.target_username
        return downloader.download_image(username, int(local_id), save_dir=save_dir)
