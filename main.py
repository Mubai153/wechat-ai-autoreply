from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Callable

from config import PROJECT_DIR, Settings
from llm import ReplyGenerator
from media_cleanup import cleanup_media_cache
from policy import is_target, should_reply
from storage import Storage
from wechat_autoreply.wechat_adapter import WeChatAdapter


logger = logging.getLogger("wechat-autoreply")
_WORKER_STOP = object()


def add_ai_prefix(reply: str) -> str:
    """为最终发送内容统一加上 AI：，避免模型重复添加前缀。"""
    text = reply.strip()
    for prefix in ("AI：", "AI:"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    return f"AI：{text}"


class ReplyService:
    def __init__(
        self,
        settings: Settings,
        dry_run: bool,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.event_callback = event_callback
        self.storage = Storage(settings.database_path)
        self.generator = ReplyGenerator(settings)
        self.adapter = WeChatAdapter(settings)
        self.queue: queue.Queue = queue.Queue()  # 兼容旧版外部调用方
        self.worker_queues: dict[str, queue.Queue] = {
            username: queue.Queue()
            for username in self.adapter.target_usernames.values()
        }
        self.workers: dict[str, threading.Thread] = {
            username: threading.Thread(
                target=self._worker,
                args=(username, work_queue),
                name=f"reply-worker-{username}",
                daemon=True,
            )
            for username, work_queue in self.worker_queues.items()
        }
        # 保留 worker 属性，兼容旧版 GUI/调用方检查服务线程状态的代码。
        self.worker = next(iter(self.workers.values()), None)
        self.stop_event = threading.Event()
        self.media_root = PROJECT_DIR / "data" / "media"
        self._last_media_cleanup = 0.0
        self._last_request: tuple[Any, str, str | None] | None = None
        self._requests: dict[str, tuple[Any, str, str | None]] = {}
        self._history_cache: dict[str, list[dict[str, str]]] = {}
        self._state_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stopped = False

    def _emit(self, event: str, **payload: Any) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(event, payload)
        except Exception:
            logger.debug("界面事件回调失败：%s", event, exc_info=True)

    def set_dry_run(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._emit("mode_changed", dry_run=dry_run)

    def _maybe_cleanup_media(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_media_cleanup < self.settings.media_cleanup_interval_seconds:
            return
        removed, freed = cleanup_media_cache(
            self.media_root,
            retention_days=self.settings.media_retention_days,
            max_bytes=self.settings.media_cache_max_mb * 1024 * 1024,
        )
        self._last_media_cleanup = now
        if removed:
            logger.info("图片缓存清理完成：删除=%s，释放=%s MB", removed, round(freed / 1024 / 1024, 2))

    def _refresh_history(
        self,
        chat_id: str | None = None,
        exclude_message_id: str | None = None,
    ) -> list[dict[str, str]]:
        """从微信刷新真实双方对话，过滤程序自己的 AI 回复。"""
        history = self.adapter.recent_history(
            self.settings.max_history_messages,
            exclude_message_id=exclude_message_id,
            chat_id=chat_id,
        )
        cache_key = chat_id or self.adapter.target_username
        with self._state_lock:
            self._history_cache[cache_key] = history
        logger.info(
            "已从 %s 读取真实双方对话：%s 条（已过滤 AI 回复）",
            chat_id or "目标联系人",
            len(history),
        )
        return history

    def enqueue(self, message) -> None:
        if not is_target(message, self.settings.target_contacts):
            logger.debug("忽略非目标联系人消息：%s", message.chat_name)
            return
        chat_id = self.adapter.target_username_for(message.chat_id, message.chat_name) or message.chat_id
        work_queue = self.worker_queues.get(chat_id)
        if work_queue is None:
            logger.warning(
                "忽略未注册的目标会话：chat_id=%s, chat_name=%s",
                message.chat_id,
                message.chat_name,
            )
            return
        logger.info("已加入回复队列：chat=%s, message_id=%s", message.chat_name, message.message_id)
        self._emit(
            "received",
            chat_name=message.chat_name,
            content=message.content,
            message_type=message.message_type,
            created_at=message.created_at.isoformat(),
            message_id=message.message_id,
            chat_id=message.chat_id,
        )
        work_queue.put(message)

    def _worker(self, chat_id: str, work_queue: queue.Queue) -> None:
        while not self.stop_event.is_set():
            try:
                message = work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if message is _WORKER_STOP:
                work_queue.task_done()
                break
            try:
                allowed, reason = should_reply(message, self.settings, self.storage)
                self.storage.mark_processed(message.message_id, message.chat_id, message.content)
                if not allowed:
                    logger.info("跳过消息：%s", reason)
                    self._emit(
                        "skipped",
                        reason=reason,
                        chat_name=message.chat_name,
                        chat_id=message.chat_id,
                        message_id=message.message_id,
                    )
                    continue

                self._emit(
                    "policy_passed",
                    reason=reason,
                    chat_name=message.chat_name,
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                history = self._refresh_history(
                    chat_id=message.chat_id,
                    exclude_message_id=message.message_id,
                )
                logger.info("开始调用模型服务生成回复：history=%s", len(history))
                self._emit(
                    "generating",
                    history_count=len(history),
                    chat_name=message.chat_name,
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                image_path = None
                prompt_message = message.content
                if message.is_image and self.settings.image_recognition_enabled:
                    media_dir = PROJECT_DIR / "data" / "media" / message.chat_id
                    media_dir.mkdir(parents=True, exist_ok=True)
                    if message.local_id is None:
                        logger.info("图片消息缺少 local_id，跳过图片识别")
                        continue
                    image_path = self.adapter.download_image(
                        message.local_id,
                        str(media_dir),
                        chat_id=message.chat_id,
                    )
                    if not image_path:
                        logger.info("本地未找到或无法解密图片，跳过本条消息")
                        self._emit(
                            "skipped",
                            reason="本地未找到或无法解密图片",
                            chat_name=message.chat_name,
                            chat_id=message.chat_id,
                            message_id=message.message_id,
                        )
                        continue
                    prompt_message = "对方发送了一张图片。请先理解图片内容，再按我的语气回复。"
                request = (message, prompt_message, image_path)
                with self._state_lock:
                    self._last_request = request
                    self._requests[message.message_id] = request
                reply = add_ai_prefix(
                    self.generator.generate(history, prompt_message, image_path=image_path)
                )
                self.storage.add_message(message.chat_id, "user", prompt_message)

                if self.dry_run:
                    logger.warning("[DRY-RUN] %s -> %s", message.content, reply)
                    self._emit(
                        "generated",
                        reply=reply,
                        sent=False,
                        chat_name=message.chat_name,
                        chat_id=message.chat_id,
                        message_id=message.message_id,
                        input_content=message.content,
                    )
                else:
                    self._emit(
                        "sending",
                        reply=reply,
                        chat_name=message.chat_name,
                        chat_id=message.chat_id,
                        message_id=message.message_id,
                    )
                    self._send_message(message, reply)
                    logger.info("已自动回复：%s", reply)
                    self._emit(
                        "generated",
                        reply=reply,
                        sent=True,
                        chat_name=message.chat_name,
                        chat_id=message.chat_id,
                        message_id=message.message_id,
                        input_content=message.content,
                    )
                self.storage.add_message(message.chat_id, "assistant", reply)
            except Exception as exc:
                logger.exception("处理消息失败；本条消息不会自动重试")
                self._emit(
                    "error",
                    message=str(exc),
                    chat_name=message.chat_name,
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                )
            finally:
                work_queue.task_done()

    def _send_message(self, message: Any, text: str) -> None:
        """按消息会话路由发送；兼容旧版只接受一个参数的适配器。"""
        send_to = getattr(self.adapter, "send_to", None)
        if callable(send_to):
            send_to(text, chat_id=message.chat_id, chat_name=message.chat_name)
        else:  # pragma: no cover - 仅兼容外部旧适配器
            self.adapter.send_text(text)

    def regenerate_last(self, message_id: str | None = None) -> None:
        """重新生成最近一条回复，仅供界面预览，绝不自动发送。"""
        request = self._request_for(message_id)
        if request is None:
            raise RuntimeError("还没有可重新生成的消息")
        message, prompt_message, image_path = request
        history = self._refresh_history(
            chat_id=message.chat_id,
            exclude_message_id=message.message_id,
        )
        self._emit(
            "generating",
            history_count=len(history),
            regenerated=True,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )
        reply = add_ai_prefix(
            self.generator.generate(history, prompt_message, image_path=image_path)
        )
        logger.info("已重新生成回复预览：%s", reply)
        self._emit(
            "generated",
            reply=reply,
            sent=False,
            regenerated=True,
            chat_name=message.chat_name,
            chat_id=message.chat_id,
            message_id=message.message_id,
            input_content=message.content,
        )

    def _request_for(self, message_id: str | None = None):
        requests = getattr(self, "_requests", {})
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            if message_id:
                return requests.get(message_id)
            return getattr(self, "_last_request", None)
        with lock:
            if message_id:
                return requests.get(message_id)
            return getattr(self, "_last_request", None)

    def send_reply(self, reply: str, message_id: str | None = None) -> None:
        """手动发送界面中当前的预览回复。

        该操作是用户明确点击后的一次性发送，因此即使服务处于“仅预览”模式也允许。
        """
        request = self._request_for(message_id)
        if request is None:
            raise RuntimeError("还没有可发送的回复")
        text = reply.strip()
        if not text:
            raise RuntimeError("回复内容为空")
        message = request[0]
        self._emit("sending", reply=text, manual=True)
        self._send_message(message, text)
        # 写入本地状态以保持回复冷却逻辑一致；微信记忆层仍会过滤 AI 回复。
        self.storage.add_message(message.chat_id, "assistant", text)
        logger.info("已手动发送回复：%s", text)
        self._emit(
            "generated",
            reply=text,
            sent=True,
            manual=True,
            chat_name=message.chat_name,
            chat_id=message.chat_id,
            message_id=getattr(message, "message_id", ""),
            input_content=message.content,
        )

    def run(self) -> None:
        self._maybe_cleanup_media(force=True)
        # 监听前即预加载，保证启动后第一次回复就有聊天记忆。
        # 每个目标会话使用独立上下文和回复队列；不同联系人可以并行生成回复，
        # 同一联系人仍按消息到达顺序串行处理。
        for chat_id in self.worker_queues:
            self._refresh_history(chat_id=chat_id)
        for worker in self.workers.values():
            worker.start()
        self.adapter.listen(self.enqueue)
        logger.info("服务已启动。dry-run=%s，按 Ctrl+C 停止。", self.dry_run)
        self._emit("service_started", dry_run=self.dry_run)
        try:
            while not self.stop_event.wait(1.0):
                self._maybe_cleanup_media()
        finally:
            self.stop()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.stop_event.set()
            self.adapter.stop()
            for work_queue in getattr(self, "worker_queues", {}).values():
                work_queue.put(_WORKER_STOP)
            for worker in getattr(self, "workers", {}).values():
                if worker.is_alive() and threading.current_thread() is not worker:
                    worker.join(timeout=3)
            worker = getattr(self, "worker", None)
            if worker is not None and worker not in getattr(self, "workers", {}).values():
                if worker.is_alive() and threading.current_thread() is not worker:
                    worker.join(timeout=3)
            logger.info("服务已停止")
            self._emit("service_stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信 4.x 指定联系人 AI 自动回复")
    parser.add_argument("--send", action="store_true", help="覆盖配置，真正自动发送回复")
    parser.add_argument("--dry-run", action="store_true", help="覆盖配置，只生成不发送")
    parser.add_argument("--headless", action="store_true", help="不打开界面，按配置运行监听服务")
    parser.add_argument("--gui", action="store_true", help="强制打开桌面控制台")
    parser.add_argument("--check", action="store_true", help="检查配置和模型 SDK，不连接微信")
    parser.add_argument(
        "--test-send",
        metavar="TEXT",
        help="诊断打包版微信发送链路；正文会自动添加 AI：前缀",
    )
    parser.add_argument(
        "--test-reply",
        metavar="TEXT",
        help="诊断打包版模型生成和微信发送完整链路",
    )
    return parser.parse_args()


def configure_logging(level: str) -> None:
    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "wechat_autoreply.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    numeric_level = getattr(logging, level, logging.INFO)
    logging.basicConfig(level=numeric_level, handlers=[console], force=True)

    # wechatauto 导入时会重配根 logger；把文件处理器直接挂到本项目的
    # 两个 logger 命名空间，确保 exe 的关键日志始终能落盘。
    for name in ("wechat-autoreply", "wechat_autoreply"):
        app_logger = logging.getLogger(name)
        app_logger.setLevel(numeric_level)
        app_logger.handlers.clear()
        app_logger.addHandler(file_handler)
        app_logger.propagate = True


def main() -> None:
    args = parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    wants_gui = args.gui or not any(
        (
            args.send,
            args.dry_run,
            args.headless,
            args.check,
            args.test_send is not None,
            args.test_reply is not None,
        )
    )
    if wants_gui:
        from gui import launch_gui

        launch_gui(ReplyService)
        return

    settings.validate()
    if args.check:
        ReplyGenerator(settings)
        logger.info("LLM 配置和兼容 SDK 检查通过；未连接微信。")
        return

    if args.test_send is not None:
        adapter = WeChatAdapter(settings)
        reply = add_ai_prefix(args.test_send)
        adapter.send_text(reply)
        logger.info("打包版发送诊断通过：%s", reply)
        return

    if args.test_reply is not None:
        generator = ReplyGenerator(settings)
        logger.info("开始执行打包版模型生成诊断")
        reply = add_ai_prefix(generator.generate([], args.test_reply))
        adapter = WeChatAdapter(settings)
        adapter.send_text(reply)
        logger.info("打包版模型生成和微信发送诊断通过：%s", reply)
        return

    if args.send and args.dry_run:
        raise ValueError("--send 和 --dry-run 不能同时使用")
    dry_run = settings.auto_send is False
    if args.send:
        dry_run = False
    if args.dry_run:
        dry_run = True

    service = ReplyService(settings, dry_run=dry_run)
    signal.signal(signal.SIGINT, lambda *_: service.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: service.stop_event.set())
    service.run()


if __name__ == "__main__":
    main()
