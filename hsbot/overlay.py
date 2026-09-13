"""输出层 —— 半透明置顶日志窗 + 输出枢纽。

* OutputHub: watcher 每行输出的三路分发(控制台 / 会话记录文件 / overlay 队列),
  消息为 Msg 结构: 控制台/悬浮窗收 ui 简洁版, 文件收 full 完整版。
  线程安全, 替代裸 print。
* OverlayWindow: tkinter 半透明置顶窗, 需在主线程跑 mainloop
  (watcher 放后台线程, 经 Queue 传递)。炉石需以"无边框/窗口化"模式运行,
  独占全屏时覆盖窗不可见。窗口位置/大小自动记忆(data/overlay_state.json);
  信息按人物(我/对面)与事件类型分色(颜色表见 config.yaml overlay_colors)。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

_MAX_LINES = 1500  # 窗口内保留的最大行数(防止内存/渲染膨胀)

# 信息分色默认表(可被 config.yaml 的 overlay_colors 覆盖)
_DEFAULT_COLORS = {
    "my": "#b8e6a0",       # 我方动作
    "opp": "#ffa477",      # 对面动作
    "unknown": "#9aa4ad",  # 未知方([T·?])
    "header": "#7ec8ff",   # 回合标题
    "snapshot": "#ffd479", # 快照
    "game_end": "#7ec8ff", # 终局
    "notice": "#8fb7d4",   # 系统通知(新对局/监控会话)
    "error": "#ff6b6b",    # 错误
}


@dataclass
class Msg:
    """业务输出消息: ui 版进控制台/悬浮窗, full 版进会话 .log 文件。

    tag = 显示样式标签(my/opp/unknown/header/...), 由产生方显式给出;
    悬浮窗不再从行文本反推 —— render 格式变化不影响分色。"""

    kind: str                  # chain / snapshot / game_end / notice
    ui: str
    full: str = ""             # 缺省 = ui
    tag: str = ""              # 显示样式标签(缺省按 kind/文本兜底分类)

    def __post_init__(self) -> None:
        if not self.full:
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
            self.q.put((msg.kind, msg.ui, msg.tag))
        if self.console:
            print(msg.ui)


class OverlayWindow:
    def __init__(self, cfg, q: queue.Queue) -> None:
        import tkinter as tk
        self._tk = tk
        self.cfg = cfg
        self.q = q
        self._colors = {**_DEFAULT_COLORS,
                        **(getattr(cfg, "overlay_colors", None) or {})}
        self._state_path = Path(cfg.data_dir) / "overlay_state.json"
        self.root = None
        self._save_job = None
        self._last_geom = None

    # ---------- 窗口位置/大小记忆 ----------
    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  缺文件/坏 JSON: 用默认几何
            return {}

    def _save_state(self) -> None:
        if not self._last_geom:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"geometry": self._last_geom}, ensure_ascii=False),
                encoding="utf-8")
            os.replace(tmp, self._state_path)   # 原子替换, 防截断
        except OSError:
            pass

    def _on_configure(self, event) -> None:
        if self.root is None or event.widget is not self.root:
            return
        geom = self.root.geometry()
        if geom and geom != self._last_geom:
            self._last_geom = geom
            if self._save_job:
                try:
                    self.root.after_cancel(self._save_job)
                except Exception:  # noqa: BLE001
                    pass
            self._save_job = self.root.after(800, self._save_state)  # 防抖

    # ---------- 行分类 → 颜色标签 ----------
    @staticmethod
    def _classify(kind: str, text: str, tag: str = "") -> str:
        if tag:
            return tag                    # 产生方显式指定, 最高优先
        if text.startswith("!"):
            return "error"
        if kind in ("snapshot", "game_end"):
            return kind
        if text.startswith("────"):
            return "header"
        if "·我]" in text:
            return "my"
        if "·对面]" in text:
            return "opp"
        if text.startswith("[T") and "] " in text:
            return "unknown"
        return "notice"

    def run(self) -> None:
        tk = self._tk
        root = tk.Tk()
        self.root = root
        root.title("hsbot")
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", float(self.cfg.overlay_alpha))
        except Exception:  # noqa: BLE001  部分平台不支持透明
            pass
        state = self._load_state()
        root.geometry(state.get("geometry") or self.cfg.overlay_geometry)
        if self.cfg.overlay_borderless:
            root.overrideredirect(True)
        root.bind("<Configure>", self._on_configure)

        frm = tk.Frame(root, bg="#0d1117")
        txt = tk.Text(frm, wrap="char", bg="#0d1117", fg="#b8e6a0",
                      insertbackground="#b8e6a0", relief="flat",
                      font=("Consolas", self.cfg.overlay_font_size),
                      state="disabled", padx=6, pady=4)
        for name, color in self._colors.items():
            txt.tag_configure(name, foreground=color)
        sb = tk.Scrollbar(frm, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        frm.pack(fill="both", expand=True)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def poll() -> None:
            try:
                lines = []
                while len(lines) < 400:
                    try:
                        lines.append(self.q.get_nowait())
                    except queue.Empty:
                        break
                if lines:
                    txt.configure(state="normal")
                    for item in lines:
                        kind, text = item[0], item[1]
                        tag = item[2] if len(item) > 2 else ""   # 兼容旧 2 元组
                        txt.insert("end", text + "\n", self._classify(kind, text, tag))
                    count = int(float(txt.index("end-1c")))
                    if count > _MAX_LINES:
                        txt.delete("1.0", f"{count - _MAX_LINES + 1.0}")
                    txt.see("end")
                    txt.configure(state="disabled")
            except Exception:  # noqa: BLE001  单次刷新失败不让 120ms 刷新链断掉
                traceback.print_exc()
            finally:
                try:
                    root.after(120, poll)
                except Exception:  # noqa: BLE001  窗口已销毁: 刷新链自然终止
                    pass

        poll()
        try:
            root.mainloop()
        except Exception:  # noqa: BLE001  mainloop 抛异常=窗口异常退出, 打全堆栈便于排查
            traceback.print_exc()
            print("! 悬浮窗主循环异常退出(堆栈见上), 监控随之停止。")
        finally:
            self._save_state()
