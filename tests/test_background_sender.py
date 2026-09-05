from types import SimpleNamespace

import wechat_autoreply.background_sender as background_sender_module
from wechat_autoreply.wechat_adapter import WeChatAdapter


def test_adapter_uses_configured_remark_for_background_search(monkeypatch):
    created_with = []
    state = {"sent": False}

    class FakeSender:
        def __init__(self, target):
            created_with.append(target)

        @staticmethod
        def send_text(_text):
            state["sent"] = True
            return {"status": "成功", "message": "已发送"}

    monkeypatch.setattr(background_sender_module, "BackgroundWeChatSender", FakeSender)

    adapter = WeChatAdapter.__new__(WeChatAdapter)
    adapter.settings = SimpleNamespace(
        wechat_target="小明的唯一备注",
        wechat_background_mode=True,
        wechat_allow_mouse_fallback=False,
    )
    adapter.target_username = "wxid_xiaoming"
    adapter._background_sender = None

    class FakeDB:
        @staticmethod
        def get_messages(_user, limit=20):
            if not state["sent"]:
                return []
            return [
                {"sort_seq": 2, "origin_source": 1, "content": "AI：测试"}
            ]

    adapter.db = FakeDB()
    adapter.send_text("AI：测试")

    assert created_with == ["小明的唯一备注"]


def test_background_search_does_not_resolve_remark_again(monkeypatch):
    sender = BackgroundSenderWithoutInit("小明的唯一备注")
    monkeypatch.setattr(background_sender_module.time, "sleep", lambda _seconds: None)

    assert sender._open_chat_background("小明的唯一备注")
    assert sender._uia.search_values == ["", "小明的唯一备注"]
    assert sender._uia.resolve_called is False


def test_search_result_prefers_legacy_default_action():
    calls = []

    class LegacyPattern:
        @staticmethod
        def Select():
            calls.append("wrong-select")
            return True

        @staticmethod
        def DoDefaultAction():
            calls.append("legacy")
            return True

    class InvokePattern:
        @staticmethod
        def Invoke():
            calls.append("invoke")
            return True

    class SearchResult:
        @staticmethod
        def GetLegacyIAccessiblePattern():
            return LegacyPattern()

        @staticmethod
        def GetInvokePattern():
            return InvokePattern()

    background_sender_module.BackgroundWeChatSender._invoke(SearchResult())

    assert calls == ["legacy"]


class BackgroundSenderWithoutInit(background_sender_module.BackgroundWeChatSender):
    def __init__(self, target):
        self.target = target
        self._timeout = 1
        self._ready = True
        self._current_chat = None
        self._opened_chat_name = None
        self._window_handle = 1
        self._uia = FakeUIA()

    def _ensure_background_window(self):
        return self._uia.window


class FakeValuePattern:
    def __init__(self, values):
        self.values = values

    def SetValue(self, value, waitTime=0):
        self.values.append(value)
        return True


class FakeControl:
    def __init__(self, values):
        self.pattern = FakeValuePattern(values)

    def GetValuePattern(self):
        return self.pattern


class FakeInvokePattern:
    @staticmethod
    def Invoke():
        return True


class FakeCell:
    @staticmethod
    def GetInvokePattern():
        return FakeInvokePattern()


class FakeUIA:
    def __init__(self):
        self.window = object()
        self.search_values = []
        self.resolve_called = False
        self.box = FakeControl(self.search_values)

    def _search_box(self, _window):
        return self.box

    def _resolve_search_keyword(self, _keyword):
        self.resolve_called = True
        raise AssertionError("备注名不应再次映射")

    @staticmethod
    def _collect_results(keyword):
        return [{"name": keyword, "cell": FakeCell()}]

    @staticmethod
    def current_chat():
        return "小明的唯一备注"
