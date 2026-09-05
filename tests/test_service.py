import queue
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

from main import ReplyService, _WORKER_STOP
from models import IncomingMessage


class FakeStorage:
    def __init__(self):
        self.messages = []
        self.processed = []

    @staticmethod
    def was_processed(_message_id):
        return False

    @staticmethod
    def last_assistant_at(_chat_id):
        return None

    def mark_processed(self, message_id, chat_id, content):
        self.processed.append((message_id, chat_id, content))

    def add_message(self, chat_id, role, content):
        self.messages.append((chat_id, role, content))


class FakeAdapter:
    target_username = "wxid_test"

    @staticmethod
    def recent_history(*_args, **_kwargs):
        return []

    @staticmethod
    def download_image(*_args, **_kwargs):
        raise AssertionError("缺少 local_id 时不应下载图片")


def make_service(*, dry_run=True, image_enabled=False):
    service = ReplyService.__new__(ReplyService)
    service.settings = SimpleNamespace(
        image_recognition_enabled=image_enabled,
        max_input_chars=4000,
        reply_cooldown_seconds=30,
        max_history_messages=100,
    )
    service.dry_run = dry_run
    service.storage = FakeStorage()
    service.adapter = FakeAdapter()
    service.generator = SimpleNamespace(generate=lambda *_args, **_kwargs: "测试回复")
    service.stop_event = threading.Event()
    service._state_lock = threading.RLock()
    service._history_cache = {}
    service._requests = {}
    service._last_request = None
    service.event_callback = None
    return service


def incoming(*, message_type="文本", local_id=None):
    return IncomingMessage(
        message_id="m1",
        chat_id="wxid_test",
        chat_name="小明",
        sender_id="wxid_test",
        sender_name="小明",
        content="[图片]" if message_type == "图片" else "你好",
        created_at=datetime.now(timezone.utc),
        message_type=message_type,
        local_id=local_id,
    )


def run_one(service, message):
    work_queue = queue.Queue()
    work_queue.put(message)
    work_queue.put(_WORKER_STOP)
    service._worker(message.chat_id, work_queue)


def test_preview_does_not_start_reply_cooldown():
    service = make_service(dry_run=True)

    run_one(service, incoming())

    assert service.storage.messages == [("wxid_test", "user", "你好")]


def test_successful_auto_send_records_reply_for_cooldown():
    service = make_service(dry_run=False)
    service._send_message = lambda _message, _reply: None

    run_one(service, incoming())

    assert service.storage.messages == [
        ("wxid_test", "user", "你好"),
        ("wxid_test", "assistant", "AI：测试回复"),
    ]


def test_image_without_local_id_emits_skipped_event():
    service = make_service(dry_run=True, image_enabled=True)
    events = []
    service.event_callback = lambda event, payload: events.append((event, payload))

    run_one(service, incoming(message_type="图片"))

    assert any(
        event == "skipped" and payload["reason"] == "图片消息缺少本地消息 ID"
        for event, payload in events
    )
    assert not any(event == "generated" for event, _payload in events)
