import threading
from types import SimpleNamespace

from gui import directory_size, format_bytes
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

    assert calls[0] == ("sending", {"reply": "AI：测试", "manual": True})
    assert calls[1] == ("send", "AI：测试")
    assert calls[2] == ("store", "wxid_test", "assistant", "AI：测试")
    assert calls[3][0] == "generated"
    assert calls[3][1]["sent"] is True
    assert calls[3][1]["manual"] is True
