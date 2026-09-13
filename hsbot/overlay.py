"""输出层 —— 半透明置顶日志窗 + 输出枢纽。

* OutputHub: watcher 每行输出的三路分发(控制台 / 会话记录文件 / overlay 队列),
  消息为 Msg 结构: 控制台/悬浮窗收 ui 简洁版, 文件收 full 完整版。
  线程安全, 替代裸 print。
* OverlayWindow: tkinter 半透明置顶窗, 需在主线程跑 mainloop
  (watcher 放后台线程, 经 Queue 传递)。炉石需以"无边框/窗口化"模式运行,
  独占全屏时覆盖窗不可见。窗口位置/大小自动记忆(data/overlay_state.json);
  信息按人物(我/对面)与事件类型分色(颜色表见 config.yaml overlay_colors)。
  两区布局: 上区 = 信息面板(KIND_STAT, 六格分区: 敌/斩杀/法强/回费/费用/减费,
  render.stat_fields 机读字段驱动, 可斩线整行附于面板底), 下区 = 日志流。
"""
from __future__ import annotations

import json
import queue
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

from .persist import atomic_write_text
from .render import plan_line

_MAX_LINES = 1500  # 窗口内保留的最大行数(防止内存/渲染膨胀)

# ---- Msg 协议常量(耦合#5: kind/tag 取值唯一在此, watcher/render/overlay 共用) ----
KIND_CHAIN = "chain"
KIND_SNAPSHOT = "snapshot"
KIND_GAME_END = "game_end"
KIND_NOTICE = "notice"          # 系统通知(新对局/监控会话)与未分类兜底
KIND_ERROR = "error"
KIND_ADVICE = "advice"          # 留牌建议(开局高亮)
KIND_STAT = "stat"              # 上部信息区(敌血/斩杀/法强/回费/费用, 整体替换)
TAG_MY = "my"
TAG_OPP = "opp"
TAG_UNKNOWN = "unknown"

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
    "advice": "#ffd700",   # 留牌建议(开局高亮)
    "stat": "#e6edf3",     # 上部信息区数值
    "stat_panel": "#161d2b",  # 上区面板底色(与日志流 #0d1117 区分出分区)
    "stat_dim": "#7d8590",    # 上区标签/细节小字
    "stat_sep": "#2b3646",    # 上区分隔线
}


@dataclass
class Msg:
    """业务输出消息: ui 版进控制台/悬浮窗, full 版进会话 .log 文件。

    tag = 显示样式标签(my/opp/unknown/header/...), 由产生方显式给出;
    悬浮窗不再从行文本反推 —— render 格式变化不影响分色。
    data = 机读字段(可选): KIND_STAT 携带 render.stat_fields, 悬浮窗上区
    分格面板直接渲染; 缺省时上区回退显示 ui 原文。"""

    kind: str                  # chain / snapshot / game_end / notice
    ui: str
    full: str = ""             # 缺省 = ui
    tag: str = ""              # 显示样式标签(缺省按 kind/文本兜底分类)
    data: dict | None = None   # 机读字段(目前仅 KIND_STAT: stat_fields)

    def __post_init__(self) -> None:
        if not self.full:
            self.full = self.ui


class _StatPanel:
    """上区信息面板(两区布局上区): 两行×3列 分区。

    行1 敌/斩杀/法强, 行2 回费/费用/减费 —— 减费单独成格: 手牌减费在身
    (生命缚誓者的礼物/建造水晶塔等)随引擎 COST 标签动态变化。
    视觉: 独立底色面板 + 细分隔线 + 三层字级(小标签/大数值/细节小字),
    数值用雅黑加大加粗(Consolas 无 CJK, 中文回退发虚)。数据 =
    render.stat_fields 机读字段(不从文本反推); 无字段的原生文本兜底。
    "可斩"时斩杀数值转金色(advice 色), 敌方数值用对面色(opp),
    有减费时减费数值用我方色(my)。
    可斩线(plan.lethal)为面板底部整行(措辞统一出自 render.plan_line,
    悬浮窗不二次格式化); 无 plan/非可斩隐藏不占位。"""

    _CELLS = [("enemy", "敌"), ("lethal", "斩杀"), ("spellpower", "法强"),
              ("ramp", "回费"), ("cost", "费用"), ("discount", "减费")]
    _COLS = 3

    def __init__(self, tk, parent, colors: dict, font_size: int) -> None:
        self._colors = colors
        bg = colors["stat_panel"]
        cjk = "Microsoft YaHei UI"
        font_v = (cjk, font_size + 3, "bold")      # 数值: 大一号加粗
        font_l = (cjk, font_size - 1)              # 标签
        font_d = (cjk, font_size - 2)              # 细节小字
        frame = tk.Frame(parent, bg=bg)
        frame.pack(side="top", fill="x")
        self._frame = frame
        self.cells: dict[str, tuple] = {}
        n = len(self._CELLS)
        for i, (key, label) in enumerate(self._CELLS):
            row, col = i // self._COLS, i % self._COLS
            if col:                                 # 列分隔线(跨两行, 不参与均分)
                tk.Frame(frame, width=1, bg=colors["stat_sep"]) \
                    .grid(row=0, rowspan=2, column=2 * col - 1, sticky="ns",
                          pady=6)
            cell = tk.Frame(frame, bg=bg)
            cell.grid(row=row, column=2 * col,
                      pady=(6, 2) if row == 0 else (2, 6),
                      padx=2, sticky="nsew")
            tk.Label(cell, text=label, bg=bg, fg=colors["stat_dim"],
                     font=font_l).pack()
            val = tk.Label(cell, text="—", bg=bg, fg=colors["stat"], font=font_v)
            val.pack()
            det = tk.Label(cell, text="", bg=bg, fg=colors["stat_dim"],
                           font=font_d)
            det.pack()
            self.cells[key] = (val, det)
        for c in range(2 * self._COLS - 1):
            if c % 2 == 0:
                frame.columnconfigure(c, weight=1, uniform="stat")
        # 细节小字按"窗宽/列数"自动折行: 长来源名(减费归属等)不再把列挤出窗口
        frame.bind("<Configure>", self._rewrap)
        self._raw = tk.Label(frame, text="", bg=bg, fg=colors["stat"],
                             font=("Consolas", font_size), justify="left",
                             anchor="w")
        self._raw.grid(row=2, column=0, columnspan=2 * self._COLS - 1,
                       sticky="we", padx=8, pady=(0, 4))
        self._raw.grid_remove()                    # 兜底行: 默认隐藏
        # 可斩线整行(plan.lethal 才显示): advice 金色, 默认隐藏不占位
        self._plan = tk.Label(frame, text="", bg=bg, fg=colors["advice"],
                              font=(cjk, font_size - 1), justify="left",
                              anchor="w")
        self._plan.grid(row=3, column=0, columnspan=2 * self._COLS - 1,
                        sticky="we", padx=8, pady=(0, 4))
        self._plan.grid_remove()

    def _rewrap(self, _event=None) -> None:
        w = max(60, self._frame.winfo_width() // self._COLS - 10)
        for _val, det in self.cells.values():
            det.configure(wraplength=w)

    @staticmethod
    def _q(v) -> str:
        return "?" if v is None else str(v)

    def update(self, f: dict) -> None:
        """机读字段 → 六格。每次全量覆写(含颜色), 无残留状态。"""
        c = self._colors
        v, d = self.cells["enemy"]
        v.configure(text=self._q(f["enemy_total"]), fg=c["opp"])
        d.configure(text="" if f["enemy_total"] is None else
                    f"{f['enemy_hp']}血+{f['enemy_armor']}甲")
        v, d = self.cells["lethal"]
        v.configure(text=str(f["lethal"]),
                    fg=c["advice"] if f["can_kill"] else c["stat"])
        d.configure(text=("可斩 " if f["can_kill"] else "")
                    + f"手{f['lethal_hand']}+库{self._q(f['lethal_deck'])}"
                      f"+场{f['lethal_board']}")
        v, d = self.cells["spellpower"]
        v.configure(text=str(f["spellpower"]), fg=c["stat"])
        d.configure(text="")
        v, d = self.cells["ramp"]
        v.configure(text=f"+{f['ramp']}", fg=c["stat"])
        d.configure(text=f"手{f['ramp_hand']}+库{self._q(f['ramp_deck'])}")
        v, d = self.cells["cost"]
        v.configure(text=str(f["cost_hand"]), fg=c["stat"])
        d.configure(text=f"组{self._q(f['cost_list'])} 库{self._q(f['cost_deck'])}")
        disc = f.get("discount") or {}
        total = disc.get("total") or 0
        v, d = self.cells["discount"]
        v.configure(text=f"−{total}" if total else "0",
                    fg=c["my"] if total else c["stat"])
        d.configure(text="·".join(disc.get("sources") or []) or "—")
        self._raw.grid_remove()
        line = plan_line(f)          # 可斩线(无 plan/非可斩 → None, 隐藏不占位)
        if line:
            self._plan.configure(text=line)
            self._plan.grid()
        else:
            self._plan.grid_remove()

    def set_raw(self, text: str) -> None:
        """无机读字段的原生文本兜底(旧消息形态)。"""
        self._raw.configure(text=text)
        self._raw.grid()
        self._plan.grid_remove()     # 旧形态无 plan 字段: 可斩线一并隐藏


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
            self.q.put((msg.kind, msg.ui, msg.tag, msg.data))
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
            atomic_write_text(self._state_path,
                              json.dumps({"geometry": self._last_geom},
                                         ensure_ascii=False))
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
            return KIND_ERROR
        if kind in (KIND_SNAPSHOT, KIND_GAME_END, KIND_ADVICE, KIND_STAT):
            return kind
        if text.startswith("────"):
            return "header"
        if "·我]" in text:
            return TAG_MY
        if "·对面]" in text:
            return TAG_OPP
        if text.startswith("[T") and "] " in text:
            return TAG_UNKNOWN
        return KIND_NOTICE

    def run(self) -> None:
        tk = self._tk
        root = tk.Tk()
        self.root = root
        root.title("hsbot")
        if self.cfg.overlay_topmost:
            # 置顶只应真实运行开启; 测试/回放关掉, 不抢机器前台
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

        self._panel = _StatPanel(tk, root, self._colors,
                                 self.cfg.overlay_font_size)

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
                        if kind == KIND_STAT:      # 上区: 整体替换, 不进日志流
                            data = item[3] if len(item) > 3 else None
                            if data:
                                self._panel.update(data)
                            else:                  # 旧形态无机读字段: 原文兜底
                                self._panel.set_raw(text)
                            continue
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
