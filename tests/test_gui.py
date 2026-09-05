import threading
import json
import queue
from types import SimpleNamespace

import pytest

from gui import (
    AutoReplyApp,
    directory_size,
    extract_lmstudio_model_ids,
    format_bytes,
    normalize_numeric_setting,
)
from main import ReplyService


def test_format_bytes_uses_readable_units():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1 KB"
    assert format_bytes(2 * 1024 * 1024) == "2.0 MB"


def test_directory_size_counts_nested_files(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.bin").write_bytes(b"123")
    (tmp_path / "nested" / "b.bin").write_bytes(b"4567")
    assert directory_size(tmp_path) == 7


def test_directory_size_keeps_partial_result_when_scan_fails(tmp_path, monkeypatch):
    (tmp_path / "a.bin").write_bytes(b"123")

    def interrupted_walk(*_args, **_kwargs):
        yield str(tmp_path), [], ["a.bin"]
        raise OSError("目录在扫描中消失")

    monkeypatch.setattr("gui.os.walk", interrupted_walk)

    assert directory_size(tmp_path) == 3


def test_empty_optional_numeric_setting_uses_default():
    assert normalize_numeric_setting("图片保留天数", "MEDIA_RETENTION_DAYS", "") == "7"


def test_numeric_setting_error_identifies_the_field():
    with pytest.raises(ValueError, match="调用超时（秒）.*CODEX_TIMEOUT_SECONDS"):
        normalize_numeric_setting("调用超时（秒）", "CODEX_TIMEOUT_SECONDS", "不是数字")


def test_extract_lmstudio_model_ids_keeps_unique_ids_in_server_order():
    payload = {
        "data": [
            {"id": "qwen/qwen3.5-9b"},
            {"id": "qwen/qwen3.6-35b-a3b"},
            {"id": "qwen/qwen3.5-9b"},
            {"object": "model"},
        ]
    }

    assert extract_lmstudio_model_ids(payload) == [
        "qwen/qwen3.5-9b",
        "qwen/qwen3.6-35b-a3b",
    ]


def test_extract_lmstudio_native_models_excludes_embeddings():
    payload = {
        "models": [
            {"type": "llm", "key": "qwen/qwen3.5-9b"},
            {"type": "embedding", "key": "text-embedding-nomic-embed-text-v1.5"},
        ]
    }

    assert extract_lmstudio_model_ids(payload) == ["qwen/qwen3.5-9b"]


def test_lmstudio_refresh_falls_back_when_native_endpoint_has_no_chat_models(monkeypatch):
    responses = [
        {"models": [{"type": "embedding", "key": "embed-model"}]},
        {"data": [{"id": "chat-model"}]},
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("gui.urllib.request.urlopen", lambda *_args, **_kwargs: Response(responses.pop(0)))
    monkeypatch.setattr("gui.threading.Thread", ImmediateThread)
    app = AutoReplyApp.__new__(AutoReplyApp)
    app._lmstudio_model_combo = object()
    app._lmstudio_model_status = SimpleNamespace(configure=lambda **_kwargs: None)
    app._lmstudio_model_request_id = 0
    app.form_vars = {"LMSTUDIO_BASE_URL": SimpleNamespace(get=lambda: "http://127.0.0.1:1234/v1")}
    app.ui_queue = queue.Queue()

    app._refresh_lmstudio_models()

    event = app.ui_queue.get_nowait()
    assert event["event"] == "lmstudio_models"
    assert event["payload"]["models"] == ["chat-model"]


def test_send_button_allows_unsent_preview_even_in_auto_mode():
    configured = {}

    class Button:
        def configure(self, **kwargs):
            configured.update(kwargs)

    app = AutoReplyApp.__new__(AutoReplyApp)
    app.send_reply_button = Button()
    app.last_reply = "AI：测试"
    app.last_reply_sent = False
    app.service = object()

    app._update_send_reply_button()

    assert configured["state"] == "normal"
    assert configured["cursor"] == "hand2"


def test_send_button_stays_disabled_after_reply_was_sent():
    configured = {}

    class Button:
        def configure(self, **kwargs):
            configured.update(kwargs)

    app = AutoReplyApp.__new__(AutoReplyApp)
    app.send_reply_button = Button()
    app.last_reply = "AI：测试"
    app.last_reply_sent = True
    app.service = object()

    app._update_send_reply_button()

    assert configured["state"] == "disabled"
    assert configured["cursor"] == "arrow"


def test_service_mode_change_emits_structured_event():
    events = []
    service = ReplyService.__new__(ReplyService)
    service.event_callback = lambda event, payload: events.append((event, payload))
    service.dry_run = True

    service.set_dry_run(False)

    assert service.dry_run is False
    assert events == [("mode_changed", {"dry_run": False})]


def test_stop_cleans_up_even_when_stop_event_was_already_set():
    calls = []

    class Adapter:
        @staticmethod
        def stop():
            calls.append("adapter")

    class Worker:
        @staticmethod
        def is_alive():
            return False

    service = ReplyService.__new__(ReplyService)
    service._stop_lock = threading.Lock()
    service._stopped = False
    service.stop_event = threading.Event()
    service.stop_event.set()
    service.adapter = Adapter()
    service.worker = Worker()
    service.event_callback = lambda event, payload: calls.append(event)

    service.stop()

    assert calls == ["adapter", "service_stopped"]


def test_send_reply_sends_preview_and_records_manual_send():
    calls = []

    class Adapter:
        @staticmethod
        def send_text(text):
            calls.append(("send", text))

    class Storage:
        @staticmethod
        def add_message(chat_id, role, content):
            calls.append(("store", chat_id, role, content))

    service = ReplyService.__new__(ReplyService)
    service.adapter = Adapter()
    service.storage = Storage()
    service._last_request = (SimpleNamespace(chat_id="wxid_test", chat_name="小明", content="你好"), "你好", None)
    service.event_callback = lambda event, payload: calls.append((event, payload))

    service.send_reply("AI：测试")

    assert calls[0] == (
        "sending",
        {
            "reply": "AI：测试",
            "manual": True,
            "chat_name": "小明",
            "chat_id": "wxid_test",
            "message_id": "",
        },
    )
    assert calls[1] == ("send", "AI：测试")
    assert calls[2] == ("store", "wxid_test", "assistant", "AI：测试")
    assert calls[3][0] == "generated"
    assert calls[3][1]["sent"] is True
    assert calls[3][1]["manual"] is True


def test_send_reply_ui_freezes_reply_and_message_before_starting_thread(monkeypatch):
    calls = []
    pending = []

    class Button:
        @staticmethod
        def configure(**_kwargs):
            return None

    class Service:
        @staticmethod
        def send_reply(reply, message_id):
            calls.append((reply, message_id))

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            pending.append(target)

        @staticmethod
        def start():
            return None

    monkeypatch.setattr("gui.threading.Thread", DeferredThread)
    app = AutoReplyApp.__new__(AutoReplyApp)
    app.service = Service()
    app.send_reply_button = Button()
    app.last_reply = "AI：第一条"
    app.last_message_id = "m1"
    app.ui_queue = __import__("queue").Queue()

    app._send_reply()
    app.last_reply = "AI：第二条"
    app.last_message_id = "m2"
    pending[0]()

    assert calls == [("AI：第一条", "m1")]
