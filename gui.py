from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox

from config import ENV_PATH, PROJECT_DIR, Settings, load_project_env, save_project_env
from media_cleanup import cleanup_media_cache


COLORS = {
    "canvas": "#F7F8F6",
    "panel": "#FFFFFF",
    "ink": "#17232F",
    "ink_2": "#263640",
    "green": "#07C160",
    "green_dark": "#087A43",
    "green_soft": "#DFF3E8",
    "green_pale": "#F0FAF4",
    "text": "#26343C",
    "muted": "#7D8B95",
    "muted_2": "#A1ABB1",
    "line": "#E2E6E3",
    "soft": "#EFF2F0",
    "warning": "#F5A524",
    "danger": "#D9485F",
}

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

NUMERIC_DEFAULTS = {
    "REPLY_COOLDOWN_SECONDS": 0,
    "MAX_HISTORY_MESSAGES": 100,
    "MAX_REPLY_CHARS": 500,
    "MAX_INPUT_CHARS": 4000,
    "CODEX_TIMEOUT_SECONDS": 120,
    "MEDIA_RETENTION_DAYS": 7,
    "MEDIA_CACHE_MAX_MB": 512,
    "MEDIA_CLEANUP_INTERVAL_SECONDS": 3600,
}


def normalize_numeric_setting(label: str, env_key: str, value: str) -> str:
    """Validate a numeric form value and fill omitted optional defaults."""
    cleaned = value.strip()
    if not cleaned:
        cleaned = str(NUMERIC_DEFAULTS.get(env_key, 0))
    try:
        number = int(cleaned, 10)
    except ValueError as exc:
        raise ValueError(f"{label}（{env_key}）必须是整数，当前为“{value}”") from exc
    if number < 0:
        raise ValueError(f"{label}（{env_key}）不能小于 0")
    return str(number)


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


class QueueLogHandler(logging.Handler):
    def __init__(self, sink: queue.Queue[dict[str, Any]]) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(
                {
                    "kind": "log",
                    "level": record.levelname,
                    "message": record.getMessage(),
                    "formatted": self.format(record),
                    "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                }
            )
        except Exception:
            self.handleError(record)


class Toggle(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        variable: tk.BooleanVar,
        command: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, width=44, height=26, bg=master.cget("bg"), highlightthickness=0)
        self.variable = variable
        self.command = command
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self._toggle)
        self.variable.trace_add("write", lambda *_: self.redraw())
        self.redraw()

    def _toggle(self, _event: tk.Event[Any]) -> None:
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def redraw(self) -> None:
        self.delete("all")
        on = self.variable.get()
        fill = COLORS["green"] if on else "#D8DFDB"
        self.create_oval(2, 3, 22, 23, fill=fill, outline=fill)
        self.create_oval(22, 3, 42, 23, fill=fill, outline=fill)
        self.create_rectangle(12, 3, 32, 23, fill=fill, outline=fill)
        knob_x = 30 if on else 14
        self.create_oval(knob_x - 8, 5, knob_x + 8, 21, fill="#FFFFFF", outline="#FFFFFF")


class PipelineCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, height=84, bg=COLORS["panel"], highlightthickness=0)
        self.state = ["idle", "idle", "idle", "idle"]
        self.meta = ["等待消息", "等待检查", "等待生成", "等待发送"]
        self._redraw_job: str | None = None
        self._last_width = 0
        self.bind("<Configure>", self._on_configure)
        self.after_idle(self._schedule_redraw)

    def _on_configure(self, event: tk.Event[Any]) -> None:
        if abs(int(event.width) - self._last_width) < 4:
            return
        self._schedule_redraw(80)

    def _schedule_redraw(self, delay: int = 0) -> None:
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(delay, self.redraw)

    def cancel_pending(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
            self._redraw_job = None

    def set_stage(self, index: int, meta: str, status: str = "done") -> None:
        for position in range(4):
            if position < index:
                self.state[position] = "done"
            elif position == index:
                self.state[position] = status
            elif status != "done":
                self.state[position] = "idle"
        self.meta[index] = meta
        self._schedule_redraw()

    def reset(self) -> None:
        self.state = ["idle", "idle", "idle", "idle"]
        self.meta = ["等待消息", "等待检查", "等待生成", "等待发送"]
        self._schedule_redraw()

    def _round_rect(self, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs: Any) -> None:
        points = (
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        )
        self.create_polygon(points, smooth=True, splinesteps=8, **kwargs)

    def redraw(self) -> None:
        self._redraw_job = None
        self.delete("all")
        width = max(self.winfo_width(), 480)
        self._last_width = width
        names = ("收到消息", "规则检查", "AI 生成", "后台发送")
        gap = 28 if width < 900 else 42
        margin = 2
        node_w = (width - margin * 2 - gap * 3) / 4
        for index, name in enumerate(names):
            x = margin + index * (node_w + gap)
            state = self.state[index]
            active = state in {"done", "active"}
            fill = COLORS["green_pale"] if active else "#F2F4F3"
            outline = "#BDE8CE" if active else "#DDE2DF"
            self._round_rect(x, 12, x + node_w, 72, 12, fill=fill, outline=outline, width=1)
            dot = COLORS["green"] if active else "#B7C1BC"
            self.create_oval(x + 14, 33, x + 32, 51, fill=dot, outline=dot)
            # Canvas 坐标使用像素；负字号也按像素解释，可避免 Windows
            # 125%–200% DPI 下标题被按点数放大后压住第二行状态文字。
            self.create_text(
                x + 23,
                42,
                text="✓" if active else "–",
                fill="#FFFFFF",
                font=(FONT, -12, "bold"),
            )
            self.create_text(
                x + 43,
                19,
                anchor="nw",
                text=name,
                fill=COLORS["text"],
                font=(FONT, -16, "bold"),
            )
            self.create_text(
                x + 43,
                44,
                anchor="nw",
                text=self.meta[index],
                fill=COLORS["green_dark"] if active else COLORS["muted"],
                font=(FONT, -12),
            )
            if index < 3:
                line_color = COLORS["green"] if self.state[index] == "done" else "#C6CFCA"
                self.create_line(x + node_w, 42, x + node_w + gap - 6, 42, fill=line_color, width=2)
                self.create_polygon(x + node_w + gap - 8, 38, x + node_w + gap - 2, 42, x + node_w + gap - 8, 46, fill=line_color, outline=line_color)
            if state == "active":
                self.create_oval(x + 9, 28, x + 37, 56, outline="#A9E7C2", width=2)


class AutoReplyApp:
    NAV_ITEMS = (
        ("工作台", "dashboard", "▰"),
        ("回复规则", "rules", "☷"),
        ("联系人与语气", "contact", "●"),
        ("AI 与图片", "ai", "✦"),
        ("运行日志", "logs", "▥"),
        ("数据与安全", "data", "▣"),
    )

    def __init__(self, root: tk.Tk, service_class: type[Any] | None = None) -> None:
        self.root = root
        self.root.title("微信自动回复 · 本地控制台")
        self.root.geometry("1440x900")
        self.root.minsize(1180, 760)
        self.root.configure(bg=COLORS["canvas"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ui_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.service_class = service_class
        self.service: Any | None = None
        self.service_thread: threading.Thread | None = None
        self.stop_requested = False
        self.mode_auto = tk.BooleanVar(value=False)
        self.selected_page = "dashboard"
        self.last_reply = ""
        self.last_reply_sent = False
        self.last_message_id: str | None = None
        self.activities: deque[tuple[str, str, str]] = deque(maxlen=4)
        self.all_logs: deque[tuple[str, str]] = deque(maxlen=2000)
        self.log_filter = "all"
        self.form_vars: dict[str, tk.Variable | tk.Text] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self.page_builders: dict[str, Callable[[], None]] = {}
        self.storage_sizes: dict[str, int] = {}
        self._storage_scan_running = False
        self._page_resize_job: str | None = None
        self._pending_page_size = (1, 1)

        self._read_mode_default()
        self._build_shell()
        self._build_dashboard()
        self._build_settings_pages()
        self._show_page("dashboard")
        self._refresh_storage_stats()

        self.log_handler = QueueLogHandler(self.ui_queue)
        logging.getLogger().addHandler(self.log_handler)
        self._queue_job = self.root.after(250, self._drain_queue)
        self._clock_job = self.root.after(1500, self._refresh_clock)

    def _read_mode_default(self) -> None:
        settings = Settings.from_env()
        self.mode_auto.set(settings.auto_send)

    def _build_shell(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        sidebar = tk.Frame(self.root, width=238, bg=COLORS["ink"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        brand = tk.Frame(sidebar, bg=COLORS["ink"], height=88)
        brand.pack(fill="x")
        brand.pack_propagate(False)
        mark = tk.Label(brand, text="•••", fg=COLORS["ink"], bg=COLORS["green"], font=(FONT, -12, "bold"), width=3, height=1)
        mark.place(x=20, y=20, width=42, height=42)
        brand_copy = tk.Frame(brand, bg=COLORS["ink"], width=154, height=58)
        brand_copy.place(x=74, y=13)
        brand_copy.pack_propagate(False)
        tk.Label(
            brand_copy, text="微信自动回复", fg="#FFFFFF", bg=COLORS["ink"],
            font=(FONT, -20, "bold"), anchor="w",
        ).pack(fill="x", anchor="w")
        tk.Label(
            brand_copy, text="本地控制台", fg="#91A1AA", bg=COLORS["ink"],
            font=(FONT, -13), anchor="w",
        ).pack(fill="x", anchor="w", pady=(2, 0))

        nav = tk.Frame(sidebar, bg=COLORS["ink"])
        nav.pack(fill="x", padx=12, pady=(10, 0))
        for label, key, icon in self.NAV_ITEMS:
            button = tk.Button(
                nav,
                text=f"  {icon}    {label}",
                command=lambda value=key: self._show_page(value),
                bg=COLORS["ink"], fg="#C7D0D5", activebackground=COLORS["ink_2"],
                activeforeground="#FFFFFF", relief="flat", bd=0, padx=10, pady=11,
                anchor="w", cursor="hand2", font=(FONT, 11),
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button

        bottom = tk.Frame(sidebar, bg=COLORS["ink"])
        bottom.pack(side="bottom", fill="x", padx=20, pady=20)
        tk.Frame(bottom, bg="#31414B", height=1).pack(fill="x", pady=(0, 12))
        settings_button = tk.Button(
            bottom, text="⚙    设置", command=lambda: self._show_page("settings"),
            bg=COLORS["ink"], fg="#C7D0D5", activebackground=COLORS["ink_2"],
            activeforeground="#FFFFFF", relief="flat", bd=0, anchor="w",
            cursor="hand2", font=(FONT, 10),
        )
        settings_button.pack(fill="x", pady=4)
        self.nav_buttons["settings"] = settings_button
        tk.Label(bottom, text="v1.0  本地运行", bg=COLORS["ink"], fg="#71838F", font=(MONO, 8), anchor="w").pack(fill="x", pady=(14, 0))

        main = tk.Frame(self.root, bg=COLORS["canvas"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        topbar = tk.Frame(main, bg=COLORS["canvas"], height=62)
        topbar.grid(row=0, column=0, sticky="ew", padx=26)
        topbar.grid_propagate(False)
        self.breadcrumb = tk.Label(topbar, text="工作台  /  实时概览", bg=COLORS["canvas"], fg=COLORS["muted"], font=(FONT, 10), anchor="w")
        self.breadcrumb.pack(side="left")
        self.top_connection = tk.Label(topbar, text="●  等待启动", bg=COLORS["canvas"], fg=COLORS["muted"], font=(FONT, 9))
        self.top_connection.pack(side="right", padx=(18, 0))
        tk.Label(topbar, text="本机", bg=COLORS["soft"], fg=COLORS["text"], font=(FONT, 9), padx=16, pady=7).pack(side="right")

        self.page_host = tk.Frame(main, bg=COLORS["canvas"])
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=26, pady=(0, 22))
        self.page_host.bind("<Configure>", self._schedule_page_resize)

    def _schedule_page_resize(self, event: tk.Event[Any]) -> None:
        self._pending_page_size = (max(1, int(event.width)), max(1, int(event.height)))
        if self._page_resize_job is not None:
            self.root.after_cancel(self._page_resize_job)
        self._page_resize_job = self.root.after(240, self._apply_page_resize)

    def _apply_page_resize(self) -> None:
        self._page_resize_job = None
        page = self.pages.get(self.selected_page)
        if page is None:
            return
        width, height = self._pending_page_size
        page.place_configure(x=0, y=0, width=width, height=height)
        if self.selected_page == "dashboard" and hasattr(self, "dashboard_activity"):
            if height < 720:
                self.dashboard_activity.grid_remove()
                self.runtime_quick.pack_forget()
            else:
                self.dashboard_activity.grid(
                    row=3, column=0, sticky="ew"
                )
                self.runtime_quick.pack(fill="x")

    def _card(self, master: tk.Misc, **grid: Any) -> tk.Frame:
        frame = tk.Frame(master, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1, bd=0)
        if grid:
            frame.grid(**grid)
        return frame

    def _button(self, master: tk.Misc, text: str, command: Callable[[], None], primary: bool = False, width: int | None = None) -> tk.Button:
        return tk.Button(
            master, text=text, command=command,
            bg=COLORS["ink"] if primary else COLORS["panel"],
            fg="#FFFFFF" if primary else COLORS["text"],
            activebackground=COLORS["ink_2"] if primary else COLORS["soft"],
            activeforeground="#FFFFFF" if primary else COLORS["ink"],
            highlightbackground=COLORS["line"], highlightthickness=1,
            relief="flat", bd=0, padx=16, pady=8, cursor="hand2",
            font=(FONT, 10, "bold" if primary else "normal"), width=width,
        )

    def _build_dashboard(self) -> None:
        page = tk.Frame(self.page_host, bg=COLORS["canvas"])
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        self.pages["dashboard"] = page

        status = self._card(page, row=0, column=0, sticky="ew", pady=(0, 14))
        status.grid_columnconfigure(1, weight=1)
        left = tk.Frame(status, bg=COLORS["panel"])
        left.grid(row=0, column=0, sticky="w", padx=24, pady=18)
        self.service_title = tk.Label(left, text="等待启动", bg=COLORS["panel"], fg=COLORS["ink"], font=(FONT, 20, "bold"), anchor="w")
        self.service_title.pack(anchor="w")
        settings = Settings.from_env()
        target_text = "、".join(settings.target_contacts) or "尚未配置"
        self.target_label = tk.Label(left, text=f"目标联系人：{target_text}", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 9), anchor="w")
        self.target_label.pack(anchor="w", pady=(6, 0))

        mode = tk.Frame(status, bg=COLORS["panel"])
        mode.grid(row=0, column=1, padx=24, pady=14)
        tk.Label(mode, text="运行模式", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 9)).pack(anchor="w")
        switch = tk.Frame(mode, bg=COLORS["soft"], padx=3, pady=3)
        switch.pack(anchor="w", pady=(6, 2))
        self.preview_button = tk.Button(switch, text="仅生成预览", command=lambda: self._set_mode(False), relief="flat", bd=0, padx=22, pady=7, cursor="hand2", font=(FONT, 9, "bold"))
        self.preview_button.pack(side="left")
        self.send_button = tk.Button(switch, text="自动发送", command=lambda: self._set_mode(True), relief="flat", bd=0, padx=22, pady=7, cursor="hand2", font=(FONT, 9))
        self.send_button.pack(side="left")
        self.mode_hint = tk.Label(mode, text="回复不会发给对方", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 8))
        self.mode_hint.pack(anchor="w")
        self._paint_mode_buttons()

        self.start_button = self._button(status, "开始监听", self._toggle_service, primary=True, width=11)
        self.start_button.grid(row=0, column=2, padx=24, pady=24)

        pipeline_card = self._card(page, row=1, column=0, sticky="ew", pady=(0, 14))
        pipeline_head = tk.Frame(pipeline_card, bg=COLORS["panel"])
        pipeline_head.pack(fill="x", padx=22, pady=(13, 0))
        tk.Label(pipeline_head, text="消息流水线", bg=COLORS["panel"], fg=COLORS["ink"], font=(FONT, 11, "bold")).pack(side="left")
        tk.Label(pipeline_head, text="最近一次处理路径", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 8)).pack(side="left", padx=18)
        self.pipeline = PipelineCanvas(pipeline_card)
        self.pipeline.pack(fill="x", padx=22, pady=(0, 6))

        middle = tk.Frame(page, bg=COLORS["canvas"])
        middle.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
        middle.grid_rowconfigure(0, weight=1)
        middle.grid_columnconfigure(0, weight=3)
        middle.grid_columnconfigure(1, weight=1)

        reply = self._card(middle, row=0, column=0, sticky="nsew", padx=(0, 10))
        reply.grid_columnconfigure(0, weight=1)
        tk.Label(reply, text="实时回复", bg=COLORS["panel"], fg=COLORS["ink"], font=(FONT, 13, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=22, pady=(13, 0))
        tk.Label(reply, text="最近一条消息与生成结果", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 8), anchor="w").grid(row=1, column=0, sticky="ew", padx=22, pady=(3, 12))
        self.incoming_box = tk.Frame(reply, bg="#F2F4F3")
        self.incoming_box.grid(row=2, column=0, sticky="ew", padx=22)
        self.incoming_sender = tk.Label(self.incoming_box, text="暂无消息", bg="#F2F4F3", fg=COLORS["muted"], font=(FONT, 8), anchor="w")
        self.incoming_sender.pack(fill="x", padx=16, pady=(7, 1))
        self.incoming_text = tk.Label(self.incoming_box, text="启动监听后，新消息会显示在这里。", bg="#F2F4F3", fg=COLORS["text"], font=(FONT, 11), anchor="w", justify="left", wraplength=660)
        self.incoming_text.pack(fill="x", padx=16, pady=(0, 8))

        self.reply_box = tk.Frame(reply, bg=COLORS["green_pale"], highlightbackground="#BDE8CE", highlightthickness=1)
        self.reply_box.grid(row=3, column=0, sticky="nsew", padx=22, pady=8)
        self.reply_box.grid_columnconfigure(0, weight=1)
        self.reply_status = tk.Label(self.reply_box, text="AI 回复预览", bg=COLORS["green_pale"], fg=COLORS["green_dark"], font=(FONT, 9, "bold"), anchor="w")
        self.reply_status.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 3))
        self.reply_text = tk.Label(self.reply_box, text="等待生成回复。", bg=COLORS["green_pale"], fg=COLORS["text"], font=(FONT, 11), anchor="nw", justify="left", wraplength=690)
        self.reply_text.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 6))
        reply_actions = tk.Frame(self.reply_box, bg=COLORS["green_pale"])
        reply_actions.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.reply_meta = tk.Label(reply_actions, text="AI  ·  等待消息", bg=COLORS["green_pale"], fg=COLORS["muted"], font=(MONO, 8))
        self.reply_meta.pack(side="left")
        self.send_reply_button = self._button(reply_actions, "发送回复", self._send_reply, primary=True)
        self.send_reply_button.pack(side="right", padx=(8, 0))
        self._update_send_reply_button()
        self._button(reply_actions, "重新生成", self._regenerate).pack(side="right", padx=(8, 0))
        self._button(reply_actions, "复制回复", self._copy_reply).pack(side="right")

        runtime = self._card(middle, row=0, column=1, sticky="nsew")
        runtime_head = tk.Frame(runtime, bg=COLORS["panel"])
        runtime_head.pack(fill="x", padx=22, pady=(12, 3))
        tk.Label(runtime_head, text="运行状态", bg=COLORS["panel"], fg=COLORS["ink"], font=(FONT, 13, "bold"), anchor="w").pack(side="left")
        self._link_button(runtime_head, "调整设置  →", lambda: self._show_page("ai")).pack(side="right")
        self.runtime_values: dict[str, tk.Label] = {}
        for name in ("微信连接", "模型服务", "后台发送"):
            row = tk.Frame(runtime, bg=COLORS["panel"])
            row.pack(fill="x", padx=22, pady=5)
            tk.Label(row, text="●", bg=COLORS["panel"], fg="#B7C1BC", font=(FONT, 9)).pack(side="left")
            tk.Label(row, text=name, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9)).pack(side="left", padx=8)
            value = tk.Label(row, text="未检查", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 9, "bold"))
            value.pack(side="right")
            self.runtime_values[name] = value
        self.runtime_quick = tk.Frame(runtime, bg=COLORS["panel"])
        self.runtime_quick.pack(fill="x")
        tk.Frame(self.runtime_quick, bg=COLORS["line"], height=1).pack(fill="x", padx=22, pady=(4, 6))
        tk.Label(self.runtime_quick, text="快速设置", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 8), anchor="w").pack(fill="x", padx=22)
        self.quick_cooldown = self._status_pair(self.runtime_quick, "回复冷却", f"{settings.reply_cooldown_seconds} 秒")
        self.quick_history = self._status_pair(self.runtime_quick, "上下文", f"{settings.max_history_messages} 条")
        image_row = tk.Frame(self.runtime_quick, bg=COLORS["panel"])
        image_row.pack(fill="x", padx=22, pady=3)
        tk.Label(image_row, text="图片识别", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9)).pack(side="left")
        self.quick_image_var = tk.BooleanVar(value=settings.image_recognition_enabled)
        Toggle(image_row, self.quick_image_var, self._quick_image_changed).pack(side="right")

        activity = self._card(page, row=3, column=0, sticky="ew")
        self.dashboard_activity = activity
        head = tk.Frame(activity, bg=COLORS["panel"])
        head.pack(fill="x", padx=22, pady=(13, 5))
        tk.Label(head, text="最近活动", bg=COLORS["panel"], fg=COLORS["ink"], font=(FONT, 11, "bold")).pack(side="left")
        self._link_button(head, "打开日志", self._open_log_file).pack(side="right")
        self.activity_frame = tk.Frame(activity, bg=COLORS["panel"])
        self.activity_frame.pack(fill="x", padx=22, pady=(0, 10))
        self._add_activity("等待启动服务", "info")

    def _status_pair(self, master: tk.Misc, label: str, value: str) -> tk.Label:
        row = tk.Frame(master, bg=COLORS["panel"])
        row.pack(fill="x", padx=22, pady=3)
        tk.Label(row, text=label, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 9)).pack(side="left")
        result = tk.Label(row, text=value, bg=COLORS["panel"], fg=COLORS["ink"], font=(MONO, 9))
        result.pack(side="right")
        return result

    def _link_button(self, master: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(master, text=text, command=command, bg=master.cget("bg"), fg=COLORS["green_dark"], activebackground=master.cget("bg"), activeforeground=COLORS["green"], relief="flat", bd=0, cursor="hand2", font=(FONT, 9, "bold"))

    def _build_settings_pages(self) -> None:
        self.page_builders = {
            "rules": lambda: self._create_form_page(
                "rules", "回复规则", "控制回复频率、上下文范围与输出边界。",
                (
                    ("回复冷却（秒）", "REPLY_COOLDOWN_SECONDS", "entry", "0 表示每条新消息都允许回复"),
                    ("上下文消息数", "MAX_HISTORY_MESSAGES", "entry", "启动时及每次回复前读取的真实双方消息数量（过滤 AI 回复）"),
                    ("最大回复字数", "MAX_REPLY_CHARS", "entry", "超出后由模型层截断"),
                    ("最大输入字数", "MAX_INPUT_CHARS", "entry", "过长消息将被安全跳过"),
                    ("回复指令", "SYSTEM_PROMPT", "text", "定义语气、边界与回复原则"),
                ),
            ),
            "contact": lambda: self._create_form_page(
                "contact", "联系人与语气", "最多监听 3 个联系人；用逗号分隔备注名，并可加载离线语气画像。",
                (
                    ("目标联系人（最多 3 个）", "WECHAT_TARGETS", "entry", "填写备注名，多个联系人用逗号分隔"),
                    ("语气画像文件", "PERSONA_PATH", "file", "可选的 persona.json 文件"),
                ),
            ),
            "ai": lambda: self._create_form_page(
                "ai", "AI 与图片", "配置生成模型、图片理解和缓存保留策略。",
                (
                    ("模型来源", "LLM_PROVIDER", "choice:ccswitch|codex_cli|lmstudio|openai_compatible", "可选择 CC Switch、Codex CLI、LM Studio 本地模型或其他兼容接口"),
                    ("CC Switch 地址", "CCSWITCH_BASE_URL", "entry", "默认 http://127.0.0.1:15721/v1"),
                    ("CC Switch 模型", "CCSWITCH_MODEL", "entry", "留空时自动读取 CC Switch 当前模型"),
                    ("Codex 命令", "CODEX_COMMAND", "entry", "通常保持 codex"),
                    ("Codex 模型", "CODEX_MODEL", "entry", "留空时使用 Codex 当前默认模型"),
                    ("LM Studio 地址", "LMSTUDIO_BASE_URL", "entry", "默认 http://127.0.0.1:1234/v1；需先在 LM Studio 启动本地服务器"),
                    ("LM Studio 模型", "LMSTUDIO_MODEL", "entry", "填写 LM Studio 的模型 ID；留空时自动使用 /v1/models 返回的第一个模型"),
                    ("LM Studio Key", "LMSTUDIO_API_KEY", "secret", "LM Studio 通常可填写 lm-studio；仅保存在本机 .env"),
                    ("调用超时（秒）", "CODEX_TIMEOUT_SECONDS", "entry", "建议不少于 120 秒"),
                    ("兼容接口地址", "LLM_BASE_URL", "entry", "仅 openai_compatible 模式需要"),
                    ("兼容接口 Key", "LLM_API_KEY", "secret", "只保存在本机 .env"),
                    ("兼容接口模型", "LLM_MODEL", "entry", "仅兼容接口模式需要"),
                    ("启用图片识别", "IMAGE_RECOGNITION_ENABLED", "bool", "解密微信图片后交给视觉模型理解"),
                    ("图片保留天数", "MEDIA_RETENTION_DAYS", "entry", "到期后自动清理"),
                    ("图片缓存上限（MB）", "MEDIA_CACHE_MAX_MB", "entry", "超过上限时优先删除旧文件"),
                ),
            ),
            "logs": self._create_logs_page,
            "data": self._create_data_page,
            "settings": lambda: self._create_form_page(
                "settings", "设置", "调整日志与后台发送行为。",
                (
                    ("日志级别", "LOG_LEVEL", "choice:DEBUG|INFO|WARNING|ERROR", "日常使用建议 INFO"),
                    ("后台无鼠标发送", "WECHAT_BACKGROUND_MODE", "bool", "使用 UIAutomation，不移动鼠标"),
                    ("允许鼠标回退", "WECHAT_ALLOW_MOUSE_FALLBACK", "bool", "UIA 失败时允许旧的坐标/OCR 路径"),
                    ("下次启动默认自动发送", "AUTO_SEND", "bool", "建议保持关闭，启动后手动确认"),
                ),
            ),
        }

    def _create_form_page(self, key: str, title: str, subtitle: str, fields: tuple[tuple[str, str, str, str], ...]) -> None:
        page = tk.Frame(self.page_host, bg=COLORS["canvas"])
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages[key] = page
        header = tk.Frame(page, bg=COLORS["canvas"])
        header.grid(row=0, column=0, sticky="ew", pady=(6, 18))
        tk.Label(header, text=title, bg=COLORS["canvas"], fg=COLORS["ink"], font=(FONT, 20, "bold"), anchor="w").pack(anchor="w")
        tk.Label(header, text=subtitle, bg=COLORS["canvas"], fg=COLORS["muted"], font=(FONT, 9), anchor="w").pack(anchor="w", pady=(5, 0))

        outer = self._card(page, row=1, column=0, sticky="nsew")
        canvas = tk.Canvas(outer, bg=COLORS["panel"], highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=COLORS["panel"])
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        resize_state: dict[str, str | int | None] = {"job": None, "width": 0}

        def sync_form_canvas(event: tk.Event[Any] | None = None) -> None:
            if event is not None:
                resize_state["width"] = int(event.width)
            pending = resize_state.get("job")
            if isinstance(pending, str):
                canvas.after_cancel(pending)

            def apply_geometry() -> None:
                resize_state["job"] = None
                width = int(resize_state.get("width") or canvas.winfo_width())
                canvas.itemconfigure(window, width=width)
                canvas.configure(scrollregion=canvas.bbox("all"))

            resize_state["job"] = canvas.after(80, apply_geometry)

        inner.bind("<Configure>", lambda _event: sync_form_canvas())
        canvas.bind("<Configure>", sync_form_canvas)
        inner.grid_columnconfigure(0, minsize=310)
        inner.grid_columnconfigure(1, weight=1)

        grid_row = 0
        for label, env_key, kind, help_text in fields:
            left = tk.Frame(inner, bg=COLORS["panel"])
            left.grid(row=grid_row, column=0, sticky="new", padx=(30, 28), pady=16)
            tk.Label(left, text=label, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(left, text=help_text, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 8), anchor="w", justify="left", wraplength=270).pack(fill="x", pady=(4, 0))

            control = tk.Frame(inner, bg=COLORS["panel"])
            control.grid(row=grid_row, column=1, sticky="nsew", padx=(0, 30), pady=16)
            raw = os.getenv(env_key, "")
            if env_key == "WECHAT_TARGETS" and not raw.strip():
                raw = os.getenv("WECHAT_TARGET", "")
            if env_key == "LMSTUDIO_BASE_URL" and not raw.strip():
                raw = "http://127.0.0.1:1234/v1"
            if env_key == "LMSTUDIO_API_KEY" and not raw.strip():
                raw = "lm-studio"
            if env_key in NUMERIC_DEFAULTS and not raw.strip():
                raw = str(NUMERIC_DEFAULTS[env_key])
            if kind == "bool":
                var = tk.BooleanVar(value=raw.strip().lower() in {"1", "true", "yes", "on"})
                Toggle(control, var).pack(side="right", padx=8, pady=4)
                self.form_vars[env_key] = var
            elif kind == "text":
                widget = tk.Text(control, height=6, wrap="word", bg="#F8FAF8", fg=COLORS["text"], insertbackground=COLORS["ink"], relief="flat", highlightbackground=COLORS["line"], highlightthickness=1, padx=12, pady=10, font=(FONT, 10))
                widget.insert("1.0", raw)
                widget.pack(fill="both", expand=True)
                self.form_vars[env_key] = widget
            elif kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split("|")
                var = tk.StringVar(value=raw or options[0])
                menu = tk.OptionMenu(control, var, *options)
                menu.configure(bg="#F8FAF8", fg=COLORS["text"], activebackground=COLORS["soft"], relief="flat", highlightbackground=COLORS["line"], width=26, font=(FONT, 9))
                menu.pack(side="right")
                self.form_vars[env_key] = var
            else:
                var = tk.StringVar(value=raw)
                show = "•" if kind == "secret" else ""
                entry = tk.Entry(control, textvariable=var, show=show, bg="#F8FAF8", fg=COLORS["text"], insertbackground=COLORS["ink"], relief="flat", highlightbackground=COLORS["line"], highlightcolor=COLORS["green"], highlightthickness=1, font=(FONT, 10))
                if kind == "file":
                    self._button(control, "选择文件", lambda value=var: self._pick_file(value)).pack(side="right")
                entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
                self.form_vars[env_key] = var
            grid_row += 1
            tk.Frame(inner, bg=COLORS["line"], height=1).grid(
                row=grid_row, column=0, columnspan=2, sticky="ew", padx=30
            )
            grid_row += 1

        footer = tk.Frame(inner, bg=COLORS["panel"])
        footer.grid(row=grid_row, column=0, columnspan=2, sticky="ew", padx=30, pady=22)
        self._button(footer, "保存更改", lambda group=fields: self._save_fields(group), primary=True).pack(side="right")
        tk.Label(footer, text="监听运行中时，保存的配置会在下次启动监听后生效。", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, 8)).pack(side="right", padx=14)

    def _create_logs_page(self) -> None:
        page = tk.Frame(self.page_host, bg=COLORS["canvas"])
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self.pages["logs"] = page
        header = tk.Frame(page, bg=COLORS["canvas"])
        header.grid(row=0, column=0, sticky="ew", pady=(6, 18))
        tk.Label(header, text="运行日志", bg=COLORS["canvas"], fg=COLORS["ink"], font=(FONT, 20, "bold")).pack(side="left")
        self._button(header, "打开日志文件", self._open_log_file).pack(side="right")
        self._button(header, "清空界面", self._clear_log_view).pack(side="right", padx=8)
        self.log_filter_button = self._button(header, "仅异常：关", self._toggle_log_filter)
        self.log_filter_button.pack(side="right")
        card = self._card(page, row=1, column=0, sticky="nsew")
        self.log_text = tk.Text(card, bg="#111B24", fg="#D6E1E6", insertbackground="#FFFFFF", relief="flat", padx=18, pady=16, wrap="word", font=(MONO, 9), state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)
        self.log_text.tag_configure("ERROR", foreground="#FF8E9E")
        self.log_text.tag_configure("WARNING", foreground="#F5C36A")
        self.log_text.tag_configure("INFO", foreground="#C9D6DC")
        self.log_text.tag_configure("DEBUG", foreground="#78909C")

    def _create_data_page(self) -> None:
        page = tk.Frame(self.page_host, bg=COLORS["canvas"])
        page.grid_columnconfigure(0, weight=1)
        self.pages["data"] = page
        tk.Label(page, text="数据与安全", bg=COLORS["canvas"], fg=COLORS["ink"], font=(FONT, 20, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", pady=(6, 4))
        tk.Label(page, text="聊天上下文、图片缓存和日志均保存在本机。", bg=COLORS["canvas"], fg=COLORS["muted"], font=(FONT, 9), anchor="w").grid(row=1, column=0, sticky="ew", pady=(0, 18))
        card = self._card(page, row=2, column=0, sticky="ew")
        self.data_labels: dict[str, tk.Label] = {}
        rows = (("对话数据库", "database"), ("图片缓存", "media"), ("日志文件", "logs"))
        for index, (label, key) in enumerate(rows):
            row = tk.Frame(card, bg=COLORS["panel"])
            row.pack(fill="x", padx=26, pady=18)
            tk.Label(row, text=label, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 10, "bold")).pack(side="left")
            value = tk.Label(row, text="正在统计…", bg=COLORS["panel"], fg=COLORS["muted"], font=(MONO, 10))
            value.pack(side="right")
            self.data_labels[key] = value
            if index < len(rows) - 1:
                tk.Frame(card, bg=COLORS["line"], height=1).pack(fill="x", padx=26)
        actions = tk.Frame(page, bg=COLORS["canvas"])
        actions.grid(row=3, column=0, sticky="ew", pady=16)
        self._button(actions, "打开数据目录", lambda: self._open_path(PROJECT_DIR / "data")).pack(side="left")
        self._button(actions, "打开日志目录", lambda: self._open_path(PROJECT_DIR / "logs")).pack(side="left", padx=8)
        self._button(actions, "清理图片缓存", self._clean_media).pack(side="left")

    def _pick_file(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askopenfilename(title="选择语气画像文件", filetypes=(("JSON 文件", "*.json"), ("所有文件", "*.*")))
        if chosen:
            variable.set(chosen)

    def _save_fields(self, fields: tuple[tuple[str, str, str, str], ...]) -> None:
        updates: dict[str, str] = {}
        try:
            for label, env_key, _kind, _help in fields:
                widget = self.form_vars[env_key]
                if isinstance(widget, tk.Text):
                    value = widget.get("1.0", "end-1c").strip()
                elif isinstance(widget, tk.BooleanVar):
                    value = "true" if widget.get() else "false"
                else:
                    value = str(widget.get()).strip()
                if env_key in NUMERIC_DEFAULTS:
                    value = normalize_numeric_setting(label, env_key, value)
                updates[env_key] = value
        except ValueError as exc:
            messagebox.showerror("无法保存", f"请检查数字配置：{exc}", parent=self.root)
            return

        try:
            save_project_env(updates)
        except OSError as exc:
            messagebox.showerror("无法保存", f"配置文件写入失败：{exc}", parent=self.root)
            return
        self._sync_settings_summary()
        self._add_activity("配置已保存，下次启动仍会保留", "info")
        messagebox.showinfo("已保存", f"配置已保存到：\n{ENV_PATH}", parent=self.root)

    def _sync_settings_summary(self) -> None:
        settings = Settings.from_env()
        target_text = "、".join(settings.target_contacts) or "尚未配置"
        self.target_label.configure(text=f"目标联系人：{target_text}")
        self.quick_cooldown.configure(text=f"{settings.reply_cooldown_seconds} 秒")
        self.quick_history.configure(text=f"{settings.max_history_messages} 条")
        self.quick_image_var.set(settings.image_recognition_enabled)

    def _quick_image_changed(self) -> None:
        value = "true" if self.quick_image_var.get() else "false"
        try:
            save_project_env({"IMAGE_RECOGNITION_ENABLED": value})
        except OSError as exc:
            messagebox.showerror("无法保存", f"配置文件写入失败：{exc}", parent=self.root)
            return
        form = self.form_vars.get("IMAGE_RECOGNITION_ENABLED")
        if isinstance(form, tk.BooleanVar):
            form.set(self.quick_image_var.get())
        self._add_activity("图片识别已开启" if self.quick_image_var.get() else "图片识别已关闭", "info")

    def _show_page(self, key: str) -> None:
        if key not in self.pages:
            builder = self.page_builders.get(key)
            if builder is None:
                return
            builder()
        for page in self.pages.values():
            page.place_forget()
        width = max(1, self.page_host.winfo_width())
        height = max(1, self.page_host.winfo_height())
        self._pending_page_size = (width, height)
        self.pages[key].place(x=0, y=0, width=width, height=height)
        self.selected_page = key
        self.root.after_idle(self._apply_page_resize)
        title = next((label for label, nav_key, _icon in self.NAV_ITEMS if nav_key == key), "设置")
        self.breadcrumb.configure(text=f"{title}  /  {'实时概览' if key == 'dashboard' else '本机配置'}")
        for nav_key, button in self.nav_buttons.items():
            selected = nav_key == key
            button.configure(bg=COLORS["ink_2"] if selected else COLORS["ink"], fg="#FFFFFF" if selected else "#C7D0D5")
        if key == "logs":
            self._render_logs()
        if key == "data":
            self._refresh_storage_stats()

    def _paint_mode_buttons(self) -> None:
        auto = self.mode_auto.get()
        self.preview_button.configure(bg=COLORS["soft"] if auto else COLORS["green_soft"], fg=COLORS["muted"] if auto else COLORS["green_dark"])
        self.send_button.configure(bg=COLORS["green_soft"] if auto else COLORS["soft"], fg=COLORS["green_dark"] if auto else COLORS["muted"])
        self.mode_hint.configure(text="回复将自动发送给对方" if auto else "回复不会发给对方", fg=COLORS["danger"] if auto else COLORS["muted"])
        self._update_send_reply_button()

    def _update_send_reply_button(self) -> None:
        """只允许发送当前尚未发出的预览回复。

        手动发送是对某一条预览的明确操作，因此不应该再被当前运行模式
        （自动发送/仅预览）锁死。自动发送成功后 ``last_reply_sent`` 会阻止
        重复发送；重新生成或切回预览后，新的未发送内容仍然可以手动发送。
        """
        if not hasattr(self, "send_reply_button"):
            return
        can_send = bool(self.last_reply.strip()) and not self.last_reply_sent and self.service is not None
        self.send_reply_button.configure(
            state="normal" if can_send else "disabled",
            bg=COLORS["ink"] if can_send else COLORS["soft"],
            fg="#FFFFFF" if can_send else COLORS["muted_2"],
            cursor="hand2" if can_send else "arrow",
        )

    def _set_mode(self, auto: bool) -> None:
        if auto and not self.mode_auto.get():
            confirmed = messagebox.askyesno(
                "开启自动发送",
                "开启后，AI 生成的内容会直接发送给目标联系人。\n\n确认开启自动发送吗？",
                icon="warning", parent=self.root,
            )
            if not confirmed:
                return
        self.mode_auto.set(auto)
        self._paint_mode_buttons()
        if self.service is not None:
            self.service.set_dry_run(not auto)
        self._add_activity("已切换为自动发送" if auto else "已切换为仅生成预览", "warning" if auto else "info")

    def _toggle_service(self) -> None:
        if self.service is not None or (self.service_thread and self.service_thread.is_alive()):
            self._stop_service()
        else:
            self._start_service()

    def _start_service(self) -> None:
        self.stop_requested = False
        dry_run = not self.mode_auto.get()
        self.service_title.configure(text="正在启动…")
        self.start_button.configure(text="取消启动", state="normal")
        self.top_connection.configure(text="●  正在连接", fg=COLORS["warning"])
        self.pipeline.reset()
        self._add_activity("正在检查微信与模型服务", "info")

        def run() -> None:
            try:
                load_project_env()
                settings = Settings.from_env()
                settings.validate()
                if self.service_class is None:
                    from main import ReplyService

                    service_class = ReplyService
                else:
                    service_class = self.service_class
                service = service_class(
                    settings,
                    dry_run=dry_run,
                    event_callback=self._service_event,
                )
                self.service = service
                if self.stop_requested:
                    service.stop()
                    return
                service.run()
            except Exception as exc:
                logging.getLogger("wechat-autoreply").exception("界面启动服务失败")
                self.ui_queue.put({"kind": "event", "event": "service_error", "payload": {"message": str(exc)}})
            finally:
                self.service = None
                self.ui_queue.put({"kind": "event", "event": "service_thread_ended", "payload": {}})

        self.service_thread = threading.Thread(target=run, name="gui-service", daemon=True)
        self.service_thread.start()

    def _stop_service(self) -> None:
        self.stop_requested = True
        self.start_button.configure(text="正在停止…", state="disabled")
        self.service_title.configure(text="正在停止…")
        service = self.service
        if service is None:
            return

        def stop() -> None:
            try:
                service.stop()
            except Exception as exc:
                self.ui_queue.put({"kind": "event", "event": "service_error", "payload": {"message": str(exc)}})

        threading.Thread(target=stop, name="gui-stop", daemon=True).start()

    def _service_event(self, event: str, payload: dict[str, Any]) -> None:
        self.ui_queue.put({"kind": "event", "event": event, "payload": payload})

    def _is_current_message(self, payload: dict[str, Any]) -> bool:
        """并行处理时只让当前预览对应的消息更新回复面板。"""
        message_id = payload.get("message_id")
        return not self.last_message_id or not message_id or message_id == self.last_message_id

    def _handle_event(self, event: str, payload: dict[str, Any]) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        if event == "service_started":
            self.service_title.configure(text="正在监听")
            self.start_button.configure(text="停止监听", state="normal")
            self.top_connection.configure(text="●  微信已连接", fg=COLORS["green_dark"])
            for key, text in (("微信连接", "正常"), ("模型服务", "已就绪"), ("后台发送", "UIA 可用")):
                self.runtime_values[key].configure(text=text, fg=COLORS["green_dark"])
            self._add_activity("服务启动，微信与模型服务检查通过", "success")
        elif event == "received":
            # 新消息到来时，旧预览不能继续作为可发送内容，避免误发上一条回复。
            self.last_reply = ""
            self.last_reply_sent = False
            self.last_message_id = str(payload.get("message_id", "")) or None
            self.incoming_sender.configure(text=f"{payload.get('chat_name', '目标联系人')}  ·  {now}")
            self.incoming_text.configure(text=payload.get("content", ""))
            self.reply_status.configure(text="正在处理")
            self.reply_text.configure(text="正在检查回复规则…")
            self._update_send_reply_button()
            self.pipeline.set_stage(0, now, "done")
            self._add_activity(f"收到来自 {payload.get('chat_name', '目标联系人')} 的{payload.get('message_type', '文本')}消息", "success")
        elif event == "policy_passed":
            if self._is_current_message(payload):
                self.pipeline.set_stage(1, "已通过", "done")
                self.reply_text.configure(text="规则已通过，准备生成回复…")
        elif event == "generating":
            if self._is_current_message(payload):
                self.pipeline.set_stage(2, "生成中", "active")
                self.reply_status.configure(text="AI 正在生成")
                self.reply_text.configure(text="AI 正在结合聊天上下文生成回复…")
                self.reply_meta.configure(text=f"AI  ·  使用 {payload.get('history_count', 0)} 条上下文")
                self.send_reply_button.configure(state="disabled", bg=COLORS["soft"], fg=COLORS["muted_2"], cursor="arrow")
        elif event == "sending":
            if self._is_current_message(payload):
                self.pipeline.set_stage(3, "发送中", "active")
                self.reply_status.configure(text="正在发送")
        elif event == "generated":
            reply = str(payload.get("reply", ""))
            sent = bool(payload.get("sent"))
            manual = bool(payload.get("manual"))
            if self._is_current_message(payload):
                self.last_reply = reply
                self.last_reply_sent = sent
                self.last_message_id = str(payload.get("message_id", "")) or self.last_message_id
                self.reply_text.configure(text=reply)
                self.reply_status.configure(text=("已发送" if manual else "已自动发送") if sent else "AI 回复预览 · 未发送")
                self.reply_meta.configure(text=f"AI  ·  {len(reply)} 字")
                self.pipeline.set_stage(3, "已发送" if sent else "仅预览", "done" if sent else "idle")
                self._update_send_reply_button()
            self._add_activity(("已手动发送回复" if manual else "已自动发送回复") if sent else "已生成回复，仅预览未发送", "success")
        elif event == "skipped":
            reason = str(payload.get("reason", "不符合回复条件"))
            if self._is_current_message(payload):
                self.pipeline.set_stage(1, reason, "idle")
                self.reply_status.configure(text="已跳过")
                self.reply_text.configure(text=f"本条消息未回复：{reason}")
            self._add_activity(f"已跳过消息：{reason}", "warning")
        elif event in {"error", "service_error"}:
            message = str(payload.get("message", "未知错误"))
            if self._is_current_message(payload):
                self.reply_status.configure(text="处理失败", fg=COLORS["danger"])
                self.reply_text.configure(text=message)
                self._update_send_reply_button()
            self.service_title.configure(text="启动失败" if event == "service_error" else "运行异常")
            self.top_connection.configure(text="●  需要处理", fg=COLORS["danger"])
            self._add_activity(message, "error")
        elif event == "service_stopped":
            self._set_stopped_state()
            self._add_activity("服务已停止", "info")
        elif event == "service_thread_ended":
            self._set_stopped_state()
        elif event == "storage_stats":
            self._storage_scan_running = False
            sizes = {
                "database": int(payload.get("database", 0)),
                "media": int(payload.get("media", 0)),
                "logs": int(payload.get("logs", 0)),
            }
            self.storage_sizes.update(sizes)
            for key, size in sizes.items():
                self.data_labels[key].configure(text=format_bytes(size))
        elif event == "cleanup_ready":
            before = int(payload.get("size", 0))
            if before == 0:
                messagebox.showinfo("无需清理", "图片缓存当前为空。", parent=self.root)
                return
            confirmed = messagebox.askyesno(
                "清理图片缓存",
                f"将删除图片缓存中的文件（约 {format_bytes(before)}）。\n"
                "微信原始文件不会受到影响。",
                icon="warning",
                parent=self.root,
            )
            if confirmed:
                self._run_media_cleanup()
        elif event == "cleanup_done":
            removed = int(payload.get("removed", 0))
            freed = int(payload.get("freed", 0))
            self._refresh_storage_stats()
            self._add_activity(
                f"已清理 {removed} 个缓存文件，释放 {format_bytes(freed)}", "success"
            )
            messagebox.showinfo(
                "清理完成",
                f"已删除 {removed} 个缓存文件，释放 {format_bytes(freed)}。",
                parent=self.root,
            )

    def _set_stopped_state(self) -> None:
        self.service_title.configure(text="等待启动")
        self.start_button.configure(text="开始监听", state="normal")
        self.top_connection.configure(text="●  未连接", fg=COLORS["muted"])
        for value in self.runtime_values.values():
            value.configure(text="未检查", fg=COLORS["muted"])
        if hasattr(self, "send_reply_button"):
            self.send_reply_button.configure(
                state="disabled",
                bg=COLORS["soft"],
                fg=COLORS["muted_2"],
                cursor="arrow",
            )

    def _regenerate(self) -> None:
        service = self.service
        if service is None:
            messagebox.showinfo("无法重新生成", "请先启动监听，并等待收到一条消息。", parent=self.root)
            return
        def run() -> None:
            try:
                service.regenerate_last(self.last_message_id)
            except Exception as exc:
                self.ui_queue.put({"kind": "event", "event": "error", "payload": {"message": str(exc)}})
        threading.Thread(target=run, name="gui-regenerate", daemon=True).start()

    def _send_reply(self) -> None:
        service = self.service
        if service is None:
            messagebox.showinfo("无法发送", "请先启动监听，并等待生成一条回复。", parent=self.root)
            return
        if not self.last_reply:
            messagebox.showinfo("暂无回复", "目前没有可发送的回复。", parent=self.root)
            return
        self.send_reply_button.configure(state="disabled", bg=COLORS["soft"], fg=COLORS["muted_2"], cursor="arrow")

        def run() -> None:
            try:
                service.send_reply(self.last_reply, self.last_message_id)
            except Exception as exc:
                self.ui_queue.put({"kind": "event", "event": "error", "payload": {"message": str(exc)}})

        threading.Thread(target=run, name="gui-send-reply", daemon=True).start()

    def _copy_reply(self) -> None:
        if not self.last_reply:
            messagebox.showinfo("暂无回复", "目前没有可复制的回复。", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_reply)
        self._add_activity("回复已复制到剪贴板", "info")

    def _add_activity(self, message: str, level: str) -> None:
        self.activities.appendleft((datetime.now().strftime("%H:%M:%S"), message, level))
        if not hasattr(self, "activity_frame"):
            return
        for child in self.activity_frame.winfo_children():
            child.destroy()
        dot_colors = {"success": COLORS["green"], "warning": COLORS["warning"], "error": COLORS["danger"], "info": "#AAB5B0"}
        for timestamp, text, item_level in self.activities:
            row = tk.Frame(self.activity_frame, bg=COLORS["panel"])
            row.pack(fill="x", pady=3)
            tk.Label(row, text="●", bg=COLORS["panel"], fg=dot_colors.get(item_level, COLORS["muted"]), font=(FONT, 7)).pack(side="left")
            tk.Label(row, text=timestamp, bg=COLORS["panel"], fg=COLORS["muted"], font=(MONO, 8), width=10, anchor="w").pack(side="left", padx=(8, 4))
            tk.Label(row, text=text, bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, 8), anchor="w").pack(side="left", fill="x", expand=True)

    def _drain_queue(self) -> None:
        processed = 0
        try:
            while processed < 120:
                item = self.ui_queue.get_nowait()
                processed += 1
                if item.get("kind") == "log":
                    level = str(item.get("level", "INFO"))
                    formatted = str(item.get("formatted", ""))
                    self.all_logs.append((level, formatted))
                    if self.selected_page == "logs" and (self.log_filter == "all" or level in {"WARNING", "ERROR", "CRITICAL"}):
                        self._append_log(level, formatted)
                else:
                    self._handle_event(str(item.get("event", "")), dict(item.get("payload", {})))
        except queue.Empty:
            pass
        if not self.ui_queue.empty():
            delay = 10
        elif self.service is not None:
            delay = 120
        else:
            delay = 320
        self._queue_job = self.root.after(delay, self._drain_queue)

    def _append_log(self, level: str, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _render_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for level, text in self.all_logs:
            if self.log_filter == "all" or level in {"WARNING", "ERROR", "CRITICAL"}:
                self.log_text.insert("end", text + "\n", level)
        self.log_text.configure(state="disabled")

    def _toggle_log_filter(self) -> None:
        self.log_filter = "errors" if self.log_filter == "all" else "all"
        self.log_filter_button.configure(text="仅异常：开" if self.log_filter == "errors" else "仅异常：关")
        self._render_logs()

    def _clear_log_view(self) -> None:
        self.all_logs.clear()
        self._render_logs()

    def _open_log_file(self) -> None:
        path = PROJECT_DIR / "logs" / "wechat_autoreply.log"
        if not path.exists():
            messagebox.showinfo("暂无日志", "日志文件会在服务运行后生成。", parent=self.root)
            return
        self._open_path(path)

    def _open_path(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True) if path.suffix == "" else None
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc), parent=self.root)

    def _clean_media(self) -> None:
        media = PROJECT_DIR / "data" / "media"

        def scan() -> None:
            self.ui_queue.put(
                {
                    "kind": "event",
                    "event": "cleanup_ready",
                    "payload": {"size": directory_size(media)},
                }
            )

        threading.Thread(target=scan, name="gui-cleanup-scan", daemon=True).start()

    def _run_media_cleanup(self) -> None:
        media = PROJECT_DIR / "data" / "media"

        def clean() -> None:
            removed, freed = cleanup_media_cache(
                media, retention_days=0, max_bytes=0
            )
            self.ui_queue.put(
                {
                    "kind": "event",
                    "event": "cleanup_done",
                    "payload": {"removed": removed, "freed": freed},
                }
            )

        threading.Thread(target=clean, name="gui-media-cleanup", daemon=True).start()

    def _refresh_storage_stats(self) -> None:
        if not hasattr(self, "data_labels") or self._storage_scan_running:
            return
        self._storage_scan_running = True
        settings = Settings.from_env()
        for label in self.data_labels.values():
            label.configure(text="正在统计…")

        def scan() -> None:
            try:
                database_size = (
                    settings.database_path.stat().st_size
                    if settings.database_path.exists()
                    else 0
                )
            except OSError:
                database_size = 0
            self.ui_queue.put(
                {
                    "kind": "event",
                    "event": "storage_stats",
                    "payload": {
                        "database": database_size,
                        "media": directory_size(PROJECT_DIR / "data" / "media"),
                        "logs": directory_size(PROJECT_DIR / "logs"),
                    },
                }
            )

        threading.Thread(target=scan, name="gui-storage-scan", daemon=True).start()

    def _refresh_clock(self) -> None:
        if self.service is not None:
            self.top_connection.configure(text=f"●  微信已连接  {datetime.now():%H:%M}")
        self._clock_job = self.root.after(30000, self._refresh_clock)

    def _on_close(self) -> None:
        if self.service is not None:
            if not messagebox.askyesno("退出控制台", "退出会停止微信自动回复服务。确认退出吗？", parent=self.root):
                return
            try:
                self.service.stop()
            except Exception:
                logging.getLogger("wechat-autoreply").exception("退出时停止服务失败")
        logging.getLogger().removeHandler(self.log_handler)
        for job in (self._page_resize_job, self._queue_job, self._clock_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.pipeline.cancel_pending()
        self.root.destroy()


def launch_gui(service_class: type[Any] | None = None) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    AutoReplyApp(root, service_class=service_class)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
