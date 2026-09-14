"""输出层 —— 置顶悬浮窗(三区布局) + 输出枢纽。

* OutputHub: watcher 每行输出的三路分发(控制台 / 会话记录文件 / overlay 队列),
  消息为 Msg 结构: 控制台/悬浮窗收 ui 简洁版, 文件收 full 完整版。
  线程安全, 替代裸 print。
* OverlayWindow: tkinter 置顶窗, 需在主线程跑 mainloop
  (watcher 放后台线程, 经 Queue 传递)。炉石需以"无边框/窗口化"模式运行,
  独占全屏时覆盖窗不可见。窗口位置/大小自动记忆(data/overlay_state.json);
  信息按人物(我/对面)与事件类型分色(颜色表见 config.yaml overlay_colors)。
  三区布局(2026-09-14 用户定版, 上/中上部背景真不透明):
    上区   = 信息面板(KIND_STAT 机读字段驱动, 两行×3格: 敌/理论伤害/法力 +
             回费/减费/法术费);
    中上部 = 推荐区(KIND_ADVICE 留牌建议 + KIND_STAT.plan 可斩/最优线
             +数据行, render.advice_rows/plan_rows 措辞, 推荐打法 top3 封顶,
             值常驻到局终);
    中下   = 日志流(局终清空并重置上两区)。
  不透明实现: 整窗 alpha=1.0, 日志区背景色经 -transparentcolor 镂空
  (游戏透出、面板实底、透明区点击穿透给游戏); 平台不支持或
  overlay_log_transparent=false 时回退旧行为(整窗 overlay_alpha 半透明)。
"""
from __future__ import annotations

import json
import queue
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

from .persist import atomic_write_text
from .render import advice_rows, plan_rows, play_offer_rows

_MAX_LINES = 1500  # 窗口内保留的最大行数(防止内存/渲染膨胀)

# 日志区镂空底色(整窗唯一透明色; 取色远离全部前景/面板色, 防误镂)
_LOG_BG_MAGIC = "#010203"
_LOG_BG_FALLBACK = "#0d1117"   # 旧版日志底色(非镂空模式)
_ADVICE_PANEL_ROWS = 3         # 推荐区行数上限(用户: top3 打法)

# ---- Msg 协议常量(耦合#5: kind/tag 取值唯一在此, watcher/render/overlay 共用) ----
KIND_CHAIN = "chain"
KIND_SNAPSHOT = "snapshot"
KIND_GAME_END = "game_end"
KIND_NOTICE = "notice"          # 系统通知(新对局/监控会话)与未分类兜底
KIND_ERROR = "error"
KIND_ADVICE = "advice"          # 留牌建议(开局高亮; data=advice 机读字段)
KIND_STAT = "stat"              # 上部信息区(敌血/伤害/法力/回费/费用, 整体替换)
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
    "advice": "#ffd700",   # 留牌建议/可斩线(推荐区主行高亮)
    "stat": "#e6edf3",     # 上部信息区数值
    "stat_panel": "#161d2b",  # 上/中上部面板底色(实底不透明)
    "stat_dim": "#7d8590",    # 面板标签/细节小字
    "stat_sep": "#2b3646",    # 面板分隔线
}


@dataclass
class Msg:
    """业务输出消息: ui 版进控制台/悬浮窗, full 版进会话 .log 文件。

    tag = 显示样式标签(my/opp/unknown/header/...), 由产生方显式给出;
    悬浮窗不再从行文本反推 —— render 格式变化不影响分色。
    data = 机读字段(可选): KIND_STAT 携带 render.stat_fields, KIND_ADVICE
    携带留牌建议事实 dict —— 上两区分格/推荐区直接渲染; 缺省时回退原文。"""

    kind: str                  # chain / snapshot / game_end / notice
    ui: str
    full: str = ""             # 缺省 = ui
    tag: str = ""              # 显示样式标签(缺省按 kind/文本兜底分类)
    data: dict | None = None   # 机读字段(KIND_STAT: stat_fields / KIND_ADVICE: advice)

    def __post_init__(self) -> None:
        if not self.full:
            self.full = self.ui


class _StatPanel:
    """上区信息面板(三区布局上区): 两行×3列 分区, 实底不透明。

    行1 敌/理论伤害/法力, 行2 回费/减费/法术费 —— 减费单独成格: 手牌减费在身
    (生命缚誓者的礼物/建造水晶塔等)随引擎 COST 标签动态变化; 法力格缺数据
    默认 1(用户定版); 法强并入理论伤害格细节(它是伤害的乘数语境)。
    视觉: 实底面板 + 细分隔线 + 三层字级(小标签/大数值/细节小字),
    数值用雅黑加大加粗(Consolas 无 CJK, 中文回退发虚)。数据 =
    render.stat_fields 机读字段(不从文本反推); 无字段的原生文本兜底。
    "可斩"时理论伤害数值转金色(advice 色), 敌方数值用对面色(opp),
    有减费时减费数值用我方色(my)。可斩线归中上部推荐区(_AdvicePanel)。"""

    _CELLS = [("enemy", "敌"), ("lethal", "理论伤害"), ("mana", "法力"),
              ("ramp", "回费"), ("discount", "减费"), ("cost", "法术费")]
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
            val = tk.Label(cell, text="—" if key != "mana" else "1",
                           bg=bg, fg=colors["stat"], font=font_v)
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

    def _rewrap(self, _event=None) -> None:
        w = max(60, self._frame.winfo_width() // self._COLS - 10)
        for _val, det in self.cells.values():
            det.configure(wraplength=w)

    @staticmethod
    def _q(v) -> str:
        return "?" if v is None else str(v)

    def update(self, f: dict) -> None:
        """机读字段 → 六格。每次全量覆写(含颜色), 无残留状态。
        mana 键缺失/None(旧消息形态/未解析)→ 法力格按默认 1 显示。"""
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
                      f"+场{f['lethal_board']} · 法强{f['spellpower']}")
        mana = f.get("mana")
        v, d = self.cells["mana"]
        v.configure(text=self._q(mana) if mana is not None else "1",
                    fg=c["stat"])
        d.configure(text="" if mana is None
                    else f"水晶{mana}/{f.get('mana_res')}")
        v, d = self.cells["ramp"]
        v.configure(text=f"+{f['ramp']}", fg=c["stat"])
        d.configure(text=f"库{self._q(f['ramp_deck'])}+手{f['ramp_hand']}")
        disc = f.get("discount") or {}
        total = disc.get("total") or 0
        v, d = self.cells["discount"]
        v.configure(text=f"−{total}" if total else "0",
                    fg=c["my"] if total else c["stat"])
        d.configure(text="·".join(disc.get("sources") or []) or "—")
        v, d = self.cells["cost"]
        v.configure(text=str(f["cost_hand"]), fg=c["stat"])
        d.configure(text=f"组{self._q(f['cost_list'])} 库{self._q(f['cost_deck'])}")
        self._raw.grid_remove()

    def reset(self) -> None:
        """局终重置: 全格回缺省(敌血 —, 法力默认 1)。"""
        for key, (val, det) in self.cells.items():
            val.configure(text="1" if key == "mana" else "—")
            det.configure(text="")
        self._raw.grid_remove()

    def set_raw(self, text: str) -> None:
        """无机读字段的原生文本兜底(旧消息形态)。"""
        self._raw.configure(text=text)
        self._raw.grid()


class _AdvicePanel:
    """中上部推荐区(三区布局中上部): 推荐打法 top3 行, 实底不透明。

    行来源全部是机读事实, 措辞归 render(悬浮窗不二次格式化):
      * KIND_STAT.plan → render.plan_rows(可斩/最优线首行 + 支撑数据行);
      * KIND_ADVICE.data(kind="play_offer") → render.play_offer_rows
        (出牌建议主行 + 语料/版本证据行, T2);
      * KIND_ADVICE.data(其余=留牌建议) → render.advice_rows
        (留牌主行 + 胜率证据行)。
    行集按 线行 > play_offer 行 > 留牌行 > 数据行 优先级封顶
    _ADVICE_PANEL_ROWS(用户: top3)。定版依据(2026-09-14 T2): 线行是确定性
    引擎(可证明、当拍可执行)必须居首; play_offer 主行是本回合的统计最优,
    时效以回合为限, 优先于早已过期的开局留牌行(留牌只服务 T1, 之后是陈旧
    事实); 数据行是支撑小字, 恒殿后 —— 恰好 top3 = 线行/play/留牌。
    常驻语义(2026-09-14 用户实测反馈: 中局推荐区别空着): 线行/play 行/
    数据行值驻留 —— plan=None / play 空更新的更新不出新行也不清旧值, 持续
    显示到被新事实替换或局终重置(留牌建议同理, 不闪烁);
    诚实口径: 没算出过线/建议时本来就空, 绝不编造。"""

    _TONE_FG = {"advice": "advice", "dim": "stat_dim"}   # 色调 → 颜色表键

    def __init__(self, tk, parent, colors: dict, font_size: int) -> None:
        self._colors = colors
        bg = colors["stat_panel"]
        cjk = "Microsoft YaHei UI"
        frame = tk.Frame(parent, bg=bg)
        frame.pack(side="top", fill="x")
        self._frame = frame
        tk.Frame(frame, height=1, bg=colors["stat_sep"]).pack(fill="x")
        tk.Label(frame, text="推荐打法", bg=bg, fg=colors["stat_dim"],
                 font=(cjk, font_size - 2), anchor="w") \
            .pack(fill="x", padx=8, pady=(2, 0))
        self._plan_rows: list[tuple[str, str]] = []
        self._play: list[tuple[str, str]] = []
        self._advice: list[tuple[str, str]] = []
        self._labels = [tk.Label(frame, text="", bg=bg, fg=colors["stat_dim"],
                                 font=(cjk, font_size - 1), justify="left",
                                 anchor="w")
                        for _ in range(_ADVICE_PANEL_ROWS)]
        frame.bind("<Configure>", self._rewrap)

    def _rewrap(self, _event=None) -> None:
        w = max(60, self._frame.winfo_width() - 20)
        for lab in self._labels:
            lab.configure(wraplength=w)

    def set_plan(self, rows: list[tuple[str, str]]) -> None:
        """线行+数据行(plan_rows 产物: (文本, 色调) 列表); 空行集 = 本次
        更新未携带 plan(对手回合/未解析/无手牌) → 值驻留, 不清旧行。"""
        if rows:
            self._plan_rows = list(rows)
            self._render()

    def set_play(self, rows: list[tuple[str, str]]) -> None:
        """出牌建议行(play_offer_rows 产物); 空行集 = 本次更新未携带建议
        (无模型/门槛未达/无候选) → 值驻留, 不清旧行。"""
        if rows:
            self._play = list(rows)
            self._render()

    def set_advice(self, rows: list[tuple[str, str]]) -> None:
        """留牌建议行(advice_rows 产物: (文本, 色调) 列表)。"""
        self._advice = list(rows or [])
        self._render()

    def reset(self) -> None:
        """局终重置: 清空全部推荐行。"""
        self._plan_rows = []
        self._play = []
        self._advice = []
        self._render()

    def _render(self) -> None:
        # 截断优先级(T2 定版): 线行 > play_offer 主行 > 留牌行 > 数据行
        # (play 证据行与 plan 数据行同为 dim 支撑行, 恒在主行之后让位);
        # _labels 恰为 _ADVICE_PANEL_ROWS 个, 超出的行自然不渲染(封顶三行内)
        rows = (self._plan_rows[:1] + self._play[:1] + self._advice
                + self._play[1:] + self._plan_rows[1:])
        for i, lab in enumerate(self._labels):
            if i < len(rows):
                text, tone = rows[i]
                lab.configure(text=text,
                              fg=self._colors[self._TONE_FG.get(tone, "stat")])
                lab.pack(fill="x", padx=8, pady=1)
            else:
                lab.configure(text="")    # 隐藏行一并清文本, 无残留措辞
                lab.pack_forget()


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

    # ---------- 不透明/半透明模式 ----------
    def _apply_opacity(self, root) -> str:
        """上/中上部真不透明: 整窗全不透明 + 日志区背景色镂空。
        返回日志区实际底色; 平台不支持镂空 → 回退整窗半透明(旧行为)。"""
        log_bg = _LOG_BG_FALLBACK
        if bool(getattr(self.cfg, "overlay_log_transparent", True)):
            try:
                root.attributes("-transparentcolor", _LOG_BG_MAGIC)
                log_bg = _LOG_BG_MAGIC
            except Exception:  # noqa: BLE001  非 Windows/老 tk: 回退
                pass
        alpha = 1.0 if log_bg == _LOG_BG_MAGIC else float(self.cfg.overlay_alpha)
        try:
            root.attributes("-alpha", alpha)
        except Exception:  # noqa: BLE001  部分平台不支持透明
            pass
        return log_bg

    def run(self) -> None:
        tk = self._tk
        root = tk.Tk()
        self.root = root
        root.title("hsbot")
        if self.cfg.overlay_topmost:
            # 置顶只应真实运行开启; 测试/回放关掉, 不抢机器前台
            root.attributes("-topmost", True)
        log_bg = self._apply_opacity(root)
        state = self._load_state()
        root.geometry(state.get("geometry") or self.cfg.overlay_geometry)
        if self.cfg.overlay_borderless:
            root.overrideredirect(True)
        root.bind("<Configure>", self._on_configure)

        self._panel = _StatPanel(tk, root, self._colors,
                                 self.cfg.overlay_font_size)
        self._advice = _AdvicePanel(tk, root, self._colors,
                                    self.cfg.overlay_font_size)

        frm = tk.Frame(root, bg=log_bg)
        txt = tk.Text(frm, wrap="char", bg=log_bg, fg="#b8e6a0",
                      insertbackground="#b8e6a0", relief="flat",
                      font=("Consolas", self.cfg.overlay_font_size),
                      state="disabled", padx=6, pady=4,
                      selectbackground="#264f78")
        self._txt = txt                    # 冒烟测试断言"行已渲染"用
        for name, color in self._colors.items():
            txt.tag_configure(name, foreground=color)
        try:                               # 滚动条融入镂空底色(拇指仍可见)
            sb = tk.Scrollbar(frm, command=txt.yview, bg=log_bg,
                              troughcolor=log_bg, activebackground="#30363d")
        except Exception:  # noqa: BLE001  选项不受支持: 朴素滚动条
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
                        try:
                            kind, text = item[0], item[1]
                            data = item[3] if len(item) > 3 else None
                            if kind == KIND_STAT:      # 上区: 整体替换,
                                if data:               # 线行+数据行进推荐区
                                    self._panel.update(data)
                                    self._advice.set_plan(plan_rows(data))
                                else:                  # 旧形态无机读字段
                                    self._panel.set_raw(text)
                                continue
                            if kind == KIND_ADVICE and data:
                                # 推荐区: 机读字段驱动, 按 data.kind 分流
                                # (play_offer=出牌建议; 其余=留牌建议)
                                if data.get("kind") == "play_offer":
                                    self._advice.set_play(
                                        play_offer_rows(data))
                                else:
                                    self._advice.set_advice(
                                        advice_rows(data))
                            if kind == KIND_GAME_END:
                                # 局终(打完一局): 清空日志流水 + 重置上两区,
                                # 终局行本身保留为新一屏的首行
                                txt.delete("1.0", "end")
                                self._panel.reset()
                                self._advice.reset()
                            tag = item[2] if len(item) > 2 else ""  # 兼容旧 2 元组
                            txt.insert("end", text + "\n",
                                       self._classify(kind, text, tag))
                        except Exception:  # noqa: BLE001  单条坏消息只丢自己,
                            traceback.print_exc()     # 不拖累同批其余行(2026-09-14)
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
