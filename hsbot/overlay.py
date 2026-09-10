"""输出层 —— 半透明置顶日志窗 + 输出枢纽。

* OutputHub: watcher 每行输出的三路分发(控制台 / 会话记录文件 / overlay 队列),
  消息为 Msg 结构: 控制台/悬浮窗收 ui 简洁版, 文件收 full 完整版。
  线程安全, 替代裸 print。
* OverlayWindow: tkinter 半透明置顶窗, 需在主线程跑 mainloop
  (watcher 放后台线程, 经 Queue 传递)。炉石需以"无边框/窗口化"模式运行,
  独占全屏时覆盖窗不可见。
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

_MAX_LINES = 1500  # 窗口内保留的最大行数(防止内存/渲染膨胀)

_KIND_COLORS = {"snapshot": "#ffd479", "game_end": "#7ec8ff"}  # 其余 kind 默认色


@dataclass
class Msg:
    """业务输出消息: ui 版进控制台/悬浮窗, full 版进会话 .log 文件。"""

    kind: str                  # chain / snapshot / game_end / notice
    ui: str
    full: str | None = None    # 缺省 = ui

    def __post_init__(self) -> None:
        if self.full is None:
            self.full = self.ui


class OutputHub:
    """可调用对象: watcher 每行输出走这里。"""

    def __init__(self, console: bool = True, to_overlay: bool = False) -> None:
        self.console = console
        self.q: queue.Queue | None = queue.Queue() if to_overlay else None
        self._file: Path | None = None
        self._lock = threading.Lock()

    def set_file(self, path: str | Path | None) -> None:
        with self._lock:
            self._file = Path(path) if path else None

    def __call__(self, msg: Msg) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    with self._file.open("a", encoding="utf-8") as fp:
                        fp.write(msg.full + "\n")
                except OSError:
                    pass
        if self.q is not None:
            self.q.put((msg.kind, msg.ui))
        if self.console:
            print(msg.ui)


class OverlayWindow:
    def __init__(self, cfg, q: queue.Queue) -> None:
        import tkinter as tk
        self._tk = tk
        self.cfg = cfg
        self.q = q

    def run(self) -> None:
        tk = self._tk
        root = tk.Tk()
        root.title("hsbot")
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", float(self.cfg.overlay_alpha))
        except Exception:  # noqa: BLE001  部分平台不支持透明
            pass
        root.geometry(self.cfg.overlay_geometry)
        if self.cfg.overlay_borderless:
            root.overrideredirect(True)

        frm = tk.Frame(root, bg="#0d1117")
        txt = tk.Text(frm, wrap="char", bg="#0d1117", fg="#b8e6a0",
                      insertbackground="#b8e6a0", relief="flat",
                      font=("Consolas", self.cfg.overlay_font_size),
                      state="disabled", padx=6, pady=4)
        for tag, color in _KIND_COLORS.items():
            txt.tag_configure(tag, foreground=color)
        sb = tk.Scrollbar(frm, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        frm.pack(fill="both", expand=True)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def poll() -> None:
            lines = []
            while len(lines) < 400:
                try:
                    lines.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if lines:
                txt.configure(state="normal")
                for kind, text in lines:
                    txt.insert("end", text + "\n", kind)
                count = int(float(txt.index("end-1c")))
                if count > _MAX_LINES:
                    txt.delete("1.0", f"{count - _MAX_LINES + 1}.0")
                txt.see("end")
                txt.configure(state="disabled")
            root.after(120, poll)

        poll()
        root.mainloop()
