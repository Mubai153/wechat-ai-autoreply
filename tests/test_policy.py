from pathlib import Path
from datetime import datetime, timezone

from config import Settings
from main import add_ai_prefix
from models import IncomingMessage
from policy import is_target, should_reply
from storage import Storage
from wechat_autoreply.wechat_adapter import WeChatAdapter, _is_outgoing


def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider="codex_cli",
        llm_model="test-model",
        codex_command="codex",
        codex_timeout_seconds=120,
        llm_base_url="",
        llm_api_key="",
        wechat_target="小明",
        auto_send=False,
        reply_cooldown_seconds=30,
        max_history_messages=12,
        max_reply_chars=500,
        max_input_chars=100,
        database_path=tmp_path / "test.sqlite3",
        system_prompt="test",
        log_level="INFO",
    )


def message(content: str = "你好") -> IncomingMessage:
    return IncomingMessage(
        message_id="m1",
        chat_id="wxid_xiaoming",
        chat_name="小明",
        sender_id="wxid_xiaoming",
        sender_name="小明",
        content=content,
        created_at=datetime.now(timezone.utc),
    )


def test_target_match(tmp_path: Path):
    assert is_target(message(), "小明")
    assert not is_target(message(), "小红")


def test_processed_message_is_skipped(tmp_path: Path):
    store = Storage(tmp_path / "test.sqlite3")
    msg = message()
    store.mark_processed(msg.message_id, msg.chat_id, msg.content)
    allowed, reason = should_reply(msg, settings(tmp_path), store)
    assert not allowed
    assert reason == "消息已处理"


def test_ai_prefix_is_added_once():
    assert add_ai_prefix("你好") == "AI：你好"
    assert add_ai_prefix("AI：你好") == "AI：你好"
    assert add_ai_prefix("AI: 你好") == "AI：你好"


def test_outgoing_messages_are_detected():
    assert _is_outgoing({"is_self": True})
    assert _is_outgoing({"sender_id": 2, "origin_source": 2})
    # origin_source 是来源渠道，不代表消息方向。
    assert not _is_outgoing({"sender_id": 4, "origin_source": 1})
    # 没有 sender_id 的旧格式仍兼容 origin_source。
    assert _is_outgoing({"origin_source": 1})
    assert not _is_outgoing({"origin_source": 2})


def test_listener_drops_self_sender_messages():
    callbacks = {}

    class FakeListener:
        def add_listener(self, user, callback):
            callbacks[user] = callback

        def start(self):
            return None

    adapter = WeChatAdapter.__new__(WeChatAdapter)
    adapter.listener = FakeListener()
    adapter.target_usernames = {"小明": "wxid_xiaoming"}
    adapter.target_username = "wxid_xiaoming"
    adapter.settings = settings(Path("."))
    adapter._self_ids = set()
    received = []

    adapter.listen(received.append)
    callback = callbacks["wxid_xiaoming"]
    callback({"sender_id": 2, "origin_source": 2, "content": "我发的"}, None)
    callback({"sender_id": 4, "origin_source": 1, "content": "对方发的"}, None)

    assert [item.content for item in received] == ["对方发的"]


def test_non_text_message_is_skipped(tmp_path: Path):
    store = Storage(tmp_path / "test.sqlite3")
    msg = IncomingMessage(
        message_id="image-1",
        chat_id="wxid_xiaoming",
        chat_name="小明",
        sender_id="wxid_xiaoming",
        sender_name="小明",
        content="<msg><img /></msg>",
        created_at=datetime.now(timezone.utc),
        message_type="图片",
    )
    allowed, reason = should_reply(msg, settings(tmp_path), store)
    assert not allowed
    assert reason == "非文本消息"


def test_animated_emoji_is_reported_separately(tmp_path: Path):
    store = Storage(tmp_path / "test.sqlite3")
    msg = IncomingMessage(
        message_id="emoji-1",
        chat_id="wxid_xiaoming",
        chat_name="小明",
        sender_id="wxid_xiaoming",
        sender_name="小明",
        content="[动画表情]",
        created_at=datetime.now(timezone.utc),
        message_type="动画表情",
    )
    allowed, reason = should_reply(msg, settings(tmp_path), store)
    assert not allowed
    assert reason == "动画表情暂不支持图片识别"


def test_send_failure_is_not_reported_as_success():
    adapter = WeChatAdapter.__new__(WeChatAdapter)
    adapter.target_username = "wxid_test"
    adapter._quick_send = lambda *_args, **_kwargs: {
        "status": "失败",
        "message": "输入框不可用",
    }

    try:
        adapter.send_text("AI：测试")
    except RuntimeError as exc:
        assert "输入框不可用" in str(exc)
    else:
        raise AssertionError("发送失败必须抛出异常")


def test_send_success_is_verified_with_outgoing_origin():
    adapter = WeChatAdapter.__new__(WeChatAdapter)
    adapter.target_username = "wxid_test"
    adapter._quick_send = lambda *_args, **kwargs: {
        "status": "成功",
        "message": "已发送",
        "verify_arg": kwargs.get("verify"),
    }

    class FakeDB:
        @staticmethod
        def get_messages(_user, limit=5):
            return [{"origin_source": 1, "content": "AI：测试"}]

    adapter.db = FakeDB()
    adapter.send_text("AI：测试")
