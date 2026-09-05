from types import SimpleNamespace

from wechat_autoreply.wechat_adapter import ShardedWeChatDB, WeChatAdapter, _is_ai_reply, _is_outgoing


def test_ai_reply_prefix_is_case_insensitive_and_allows_leading_whitespace():
    assert _is_ai_reply(" AI：不应读取")
    assert _is_ai_reply("ai: 不应读取")
    assert not _is_ai_reply("A.I. 不是自动回复前缀")


def test_outgoing_uses_the_v4_sent_status():
    assert _is_outgoing({"real_sender_id": 6, "status": 2}, self_ids={"wxid_me"})
    assert not _is_outgoing({"real_sender_id": 6, "status": 4}, self_ids={"wxid_me"})


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


def test_sharded_database_deduplicates_copied_message_rows():
    row = {
        "local_id": 7,
        "local_type": 1,
        "real_sender_id": 4,
        "create_time": 123,
        "message_content": "同一条消息",
        "source": "",
        "packed_info_data": "",
        "compress_content": "",
        "sort_seq": 99,
        "origin_source": 2,
        "status": 0,
    }

    class Connection:
        def execute(self, query, _params):
            if "sqlite_master" in query:
                return self
            return self

        def fetchone(self):
            return (1,)

        def fetchall(self):
            return [row]

        @staticmethod
        def close():
            return None

    class Database:
        @staticmethod
        def _message_dbs():
            return ["message_0.db", "message_1.db"]

        @staticmethod
        def _open(_path):
            return Connection()

    sharded = ShardedWeChatDB.__new__(ShardedWeChatDB)
    sharded._db = Database()

    assert sharded._rows("wxid_test", limit=20) == [row]
