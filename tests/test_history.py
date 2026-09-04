from types import SimpleNamespace

from wechat_autoreply.wechat_adapter import WeChatAdapter


def _adapter(messages):
    class FakeDB:
        @staticmethod
        def get_messages(_user, limit=20, offset=0):
            return messages[offset:offset + limit]

    adapter = WeChatAdapter.__new__(WeChatAdapter)
    adapter.db = FakeDB()
    adapter.target_username = "wxid_test"
    adapter.settings = SimpleNamespace(wechat_target="小明")
    return adapter


def test_history_keeps_real_messages_but_filters_ai_replies():
    # 微信数据库接口按最新在前返回。
    adapter = _adapter(
        [
            {"sort_seq": 4, "local_type": 1, "origin_source": 2, "content": "最新"},
            {"sort_seq": 3, "local_type": 1, "origin_source": 1, "content": "AI：不应读取"},
            {"sort_seq": 2, "local_type": 1, "origin_source": 1, "content": "我手动发的"},
            {"sort_seq": 1, "local_type": 1, "origin_source": 2, "content": "最早"},
        ]
    )

    assert adapter.recent_history(100) == [
        {"role": "user", "content": "最早"},
        {"role": "assistant", "content": "我手动发的"},
        {"role": "user", "content": "最新"},
    ]


def test_history_excludes_current_message_and_normalizes_media():
    adapter = _adapter(
        [
            {"sort_seq": 3, "local_type": 1, "origin_source": 2, "content": "当前"},
            {"sort_seq": 2, "local_type": 3, "origin_source": 2, "content": "xml"},
            {"sort_seq": 1, "local_type": 47, "origin_source": 2, "content": "xml"},
        ]
    )

    assert adapter.recent_history(
        2,
        exclude_message_id="wxid_test:3",
    ) == [
        {"role": "user", "content": "[动画表情]"},
        {"role": "user", "content": "[图片]"},
    ]
