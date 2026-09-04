"""不移动鼠标的微信 4.x UIAutomation 发送器。

wechatauto 的兼容发送入口为了覆盖更多版本，会在输入时调用
``Control.Click``，并在 UIA 失败时回退到坐标/OCR。这个模块只保留微信 4.x
当前可用的 UIA 控件路径：

* 输入框使用 ``ValuePattern.SetValue``，不调用 ``Click`` 或剪贴板粘贴；
* 搜索结果使用 ``InvokePattern``/``SelectionItemPattern``，不模拟鼠标；
* 复用一个 UIA 引擎实例，不为每条消息重新初始化 GUI；
* 操作前后尽量恢复用户原来的前台窗口，避免回复过程打断正在使用的应用。

如果微信版本没有暴露这些 UIA 控件，会明确失败。是否允许旧的坐标回退由
``WECHAT_ALLOW_MOUSE_FALLBACK`` 控制，默认关闭。
"""

from __future__ import annotations

import time
from typing import Any, Iterable


class BackgroundSendError(RuntimeError):
    """后台 UIA 发送不可用或发送失败。"""


class BackgroundWeChatSender:
    """基于 UIA pattern 的无鼠标发送器。"""

    def __init__(self, target: str, *, timeout: float = 15.0) -> None:
        try:
            from wechatauto.uia_driver import WeChatUIA
        except ImportError as exc:  # pragma: no cover - 安装脚本负责提供依赖
            raise BackgroundSendError("未安装微信 4.x UIAutomation 适配器") from exc

        self.target = target
        self._uia = WeChatUIA(timeout=timeout)
        self._timeout = timeout
        self._ready = False
        self._current_chat: str | None = None
        self._opened_chat_name: str | None = None
        self._window_handle: int | None = None

    @staticmethod
    def _foreground() -> int:
        try:
            import win32gui

            return int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            return 0

    @staticmethod
    def _cursor() -> tuple[int, int] | None:
        try:
            import win32api

            x, y = win32api.GetCursorPos()
            return int(x), int(y)
        except Exception:
            return None

    @staticmethod
    def _restore_cursor(position: tuple[int, int] | None) -> None:
        if position is None:
            return
        try:
            import win32api

            if win32api.GetCursorPos() != position:
                win32api.SetCursorPos(position)
        except Exception:
            pass

    @staticmethod
    def _restore_foreground(hwnd: int) -> None:
        if not hwnd or hwnd == BackgroundWeChatSender._foreground():
            return
        try:
            import win32api
            import win32gui
            import win32process

            current_thread = win32api.GetCurrentThreadId()
            target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
            attached = False
            if target_thread and target_thread != current_thread:
                try:
                    win32process.AttachThreadInput(current_thread, target_thread, True)
                    attached = True
                except Exception:
                    pass
            try:
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    try:
                        win32process.AttachThreadInput(current_thread, target_thread, False)
                    except Exception:
                        pass
        except Exception:
            pass

    def _ensure_background_window(self) -> Any:
        """找到 UIA 主窗口，但不调用第三方的 ensure_window（它会置前）。"""
        win = self._uia._find_main()
        if win is None:
            # 热激活只写微信进程的 accessibility 标志，不移动鼠标。
            self._uia._wake_accessibility()
            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline:
                win = self._uia._find_main()
                if win is not None:
                    break
                time.sleep(0.2)
        if win is None:
            raise BackgroundSendError(
                "微信 UIA 控件不可用；请确认微信 4.x 已登录且未锁定，"
                "或临时设置 WECHAT_ALLOW_MOUSE_FALLBACK=true"
            )
        self._uia._win = win
        handle = getattr(win, "NativeWindowHandle", None)
        if self._window_handle is not None and handle != self._window_handle:
            # 微信重启/重登后旧会话标记不可复用。
            self._current_chat = None
            self._opened_chat_name = None
        self._window_handle = handle
        self._ready = True
        return win

    @staticmethod
    def _set_value(control: Any, value: str) -> None:
        try:
            pattern = control.GetValuePattern()
            if not pattern.SetValue(value, waitTime=0.05):
                raise BackgroundSendError("UIA ValuePattern.SetValue 返回失败")
        except BackgroundSendError:
            raise
        except Exception as exc:
            raise BackgroundSendError(f"UIA 无法写入控件：{exc}") from exc

    @staticmethod
    def _invoke(control: Any) -> None:
        """以 UIA pattern 触发控件，不使用 Control.Click。"""
        for getter in ("GetInvokePattern", "GetSelectionItemPattern", "GetLegacyIAccessiblePattern"):
            try:
                pattern = getattr(control, getter)()
                if pattern is None:
                    continue
                method = (
                    getattr(pattern, "Invoke", None)
                    or getattr(pattern, "Select", None)
                    or getattr(pattern, "DoDefaultAction", None)
                )
                if method is not None and method():
                    return
            except Exception:
                continue
        raise BackgroundSendError("UIA 控件没有可用的 Invoke/SelectionItem pattern")

    def _find_search_result(self, keyword: str) -> Any | None:
        results = self._uia._collect_results(keyword)
        if not results:
            return None
        exact = [item for item in results if item.get("name") == keyword]
        return (exact or results)[0].get("cell")

    def _open_chat_background(self, keyword: str) -> bool:
        win = self._ensure_background_window()
        box = self._uia._search_box(win)
        if box is None:
            raise BackgroundSendError("微信 UIA 搜索框不可用")

        keywords: Iterable[str]
        try:
            keywords = self._uia._resolve_search_keyword(keyword)
        except Exception:
            keywords = (keyword,)
        for candidate in keywords:
            # 切换候选词前先清空，确保微信刷新 search_list；ValuePattern 本身
            # 不移动鼠标，清空也能避免上一次无结果查询留下的旧列表。
            self._set_value(box, "")
            time.sleep(0.2)
            self._set_value(box, candidate)
            time.sleep(1.0)
            cell = self._find_search_result(candidate)
            if cell is None:
                continue
            self._invoke(cell)
            time.sleep(0.6)
            current = self._uia.current_chat()
            if current and (current == candidate or current == keyword):
                # current_chat 返回昵称/备注，而 target 是 wxid；用稳定的
                # target 标记会话已打开，后续回复不再重复搜索。
                self._current_chat = self.target
                self._opened_chat_name = current
                return True

        # 清理搜索框，避免下次打开时残留关键词。
        try:
            self._set_value(box, "")
        except Exception:
            pass
        return False

    def send_text(self, text: str) -> dict[str, str]:
        """发送文本并返回与 wechatauto 兼容的结果字典。"""
        if not text or not text.strip():
            raise BackgroundSendError("不能发送空消息")

        previous_foreground = self._foreground()
        previous_cursor = self._cursor()
        try:
            self._ensure_background_window()
            current_name = self._uia.current_chat()
            current = self._current_chat
            if current != self.target or (
                self._opened_chat_name is not None
                and current_name != self._opened_chat_name
            ):
                if not self._open_chat_background(self.target):
                    raise BackgroundSendError(f"UIA 无法打开目标会话：{self.target}")

            edit = self._uia._chat_input()
            if edit is None:
                raise BackgroundSendError("微信 UIA 聊天输入框不可用")

            # SetValue 不移动鼠标；SendKeys 只发送 Enter，不调用 Click。
            self._set_value(edit, text)
            time.sleep(0.1)
            edit.SendKeys("{Enter}", waitTime=0.05)
            self._current_chat = self.target
            return {"status": "成功", "message": "后台 UIA 已发送"}
        except BackgroundSendError:
            raise
        except Exception as exc:
            raise BackgroundSendError(f"后台 UIA 发送失败：{exc}") from exc
        finally:
            # UIA provider 可能临时激活微信；尽量还原用户原来正在使用的窗口。
            self._restore_foreground(previous_foreground)
            # 某些微信版本的 UIA provider 自身会轻微移动光标；发送结束后还原，
            # 让调用方看到的鼠标位置保持不变。
            self._restore_cursor(previous_cursor)
