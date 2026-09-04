from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
from logging.handlers import RotatingFileHandler

from config import PROJECT_DIR, Settings
from llm import ReplyGenerator
from policy import is_target, should_reply
from storage import Storage
from wechat_autoreply.wechat_adapter import WeChatAdapter


logger = logging.getLogger("wechat-autoreply")


def add_ai_prefix(reply: str) -> str:
    """为最终发送内容统一加上 AI：，避免模型重复添加前缀。"""
    text = reply.strip()
    for prefix in ("AI：", "AI:"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    return f"AI：{text}"


class ReplyService:
    def __init__(self, settings: Settings, dry_run: bool) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.storage = Storage(settings.database_path)
        self.generator = ReplyGenerator(settings)
        self.adapter = WeChatAdapter(settings)
        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._worker, name="reply-worker", daemon=True)

    def enqueue(self, message) -> None:
        if not is_target(message, self.settings.wechat_target):
            logger.debug("忽略非目标联系人消息：%s", message.chat_name)
            return
        logger.info("已加入回复队列：chat=%s, message_id=%s", message.chat_name, message.message_id)
        self.queue.put(message)

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                allowed, reason = should_reply(message, self.settings, self.storage)
                self.storage.mark_processed(message.message_id, message.chat_id, message.content)
                if not allowed:
                    logger.info("跳过消息：%s", reason)
                    continue

                history = self.storage.recent_messages(message.chat_id, self.settings.max_history_messages)
                logger.info("开始调用 Codex 生成回复：history=%s", len(history))
                reply = add_ai_prefix(self.generator.generate(history, message.content))
                self.storage.add_message(message.chat_id, "user", message.content)

                if self.dry_run:
                    logger.warning("[DRY-RUN] %s -> %s", message.content, reply)
                else:
                    self.adapter.send_text(reply)
                    logger.info("已自动回复：%s", reply)
                self.storage.add_message(message.chat_id, "assistant", reply)
            except Exception:
                logger.exception("处理消息失败；本条消息不会自动重试")
            finally:
                self.queue.task_done()

    def run(self) -> None:
        self.worker.start()
        self.adapter.listen(self.enqueue)
        logger.info("服务已启动。dry-run=%s，按 Ctrl+C 停止。", self.dry_run)
        try:
            while not self.stop_event.wait(1.0):
                pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.adapter.stop()
        self.worker.join(timeout=3)
        logger.info("服务已停止")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信 4.x 指定联系人 AI 自动回复")
    parser.add_argument("--send", action="store_true", help="覆盖配置，真正自动发送回复")
    parser.add_argument("--dry-run", action="store_true", help="覆盖配置，只生成不发送")
    parser.add_argument("--check", action="store_true", help="检查配置和 OpenAI SDK，不连接微信")
    parser.add_argument(
        "--test-send",
        metavar="TEXT",
        help="诊断打包版微信发送链路；正文会自动添加 AI：前缀",
    )
    parser.add_argument(
        "--test-reply",
        metavar="TEXT",
        help="诊断打包版 Codex 生成和微信发送完整链路",
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
        logger.info("开始执行打包版 Codex 生成诊断")
        reply = add_ai_prefix(generator.generate([], args.test_reply))
        adapter = WeChatAdapter(settings)
        adapter.send_text(reply)
        logger.info("打包版 Codex 生成和微信发送诊断通过：%s", reply)
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
