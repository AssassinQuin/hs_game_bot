"""三区布局(2026-09-14 用户定版): 上部信息区两行六格(理论伤害含法强/法力默认1)
+ 中上部推荐区(推荐打法 top3, 不透明) + 局终清空日志并重置上部。
上/中上部背景真不透明: 日志区背景色镂空(transparentcolor), 面板保持实底。"""
import queue

import pytest

tk = pytest.importorskip("tkinter")

from .conftest import mk_full, mk_heroes, mk_store, mk_tag  # noqa: E402
from hearthstone.enums import Zone  # noqa: E402


@pytest.fixture()
def ensure_display():
    try:
        root = tk.Tk()
        root.withdraw()          # 探测窗不闪现桌面(测试不扰机器前台)
        root.destroy()
    except Exception as exc:  # noqa: BLE001  无显示环境
        pytest.skip(f"无可用显示: {exc}")

# 与 test_overlay_smoke 同款基础机读字段(+ mana 新键)
_BASE = {"enemy_total": 40, "enemy_hp": 40, "enemy_armor": 0,
         "lethal": 14, "lethal_hand": 0, "lethal_deck": 14,
         "lethal_board": 0, "can_kill": False, "lethal_est": False,
         "spellpower": 0,
         "ramp": 10, "ramp_hand": 2, "ramp_deck": 8,
         "cost_list": 45, "cost_deck": 33, "cost_hand": 12,
         "discount": {"cards": 1, "total": 2, "sources": ["生命缚誓者的礼物"]},
         "mana": 2, "mana_res": 3}

_ADVICE = {"opp_class": "PRIEST", "coin": True, "keep": ["GOOD"],
           "drop": ["BAD"],
           "per_card": {"GOOD": {"name": "好牌", "gain": 0.2},
                        "BAD": {"name": "坏牌", "gain": None}},
           "deck_wr": 0.54, "version": "v001", "scorer": "table"}


# ================= render: 机读字段与措辞 =================

def test_stat_fields_exposes_mana(tmp_path):
    """信息区机读字段补 当前法力: mana=mana_now, mana_res=水晶上限。"""
    from hsbot.carddb import CardDB
    from hsbot.analysis import EffectAnalyzer
    from hsbot.render import stat_fields

    p = tmp_path / "cards.json"
    p.write_text("[]", encoding="utf-8")
    db = CardDB(p)
    st, _ = mk_store()
    mk_heroes(st)
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1,
                     ZONE_POSITION=1))
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=EffectAnalyzer(db))
    assert f["mana"] == st.mana_now(st.friendly_key)
    assert f["mana_res"] == st.mana_fields(st.friendly_key)["res"]


def test_advice_rows_wording():
    """留牌建议事实 → 推荐区行: 主行(留/换, 与链路行同源措辞)+胜率证据行;
    无建议 → []; 缺 deck_wr → 只有主行。结论词/措辞归 render。"""
    from hsbot.render import advice_rows

    rows = advice_rows(_ADVICE)
    assert rows[0][0] == "【留牌建议·vs牧师·后手】留 好牌(+20.0%) │ 换 坏牌"
    assert rows[0][1] == "advice"
    assert rows[1][0] == "卡组胜率 54% · 依据 统计表 v001"
    assert rows[1][1] == "dim"
    assert advice_rows({k: v for k, v in _ADVICE.items()
                        if k not in ("deck_wr", "version", "scorer")}) \
        == rows[:1]
    assert advice_rows(None) == []


# ================= overlay: 面板 =================

def test_stat_panel_new_cells():
    """六格布局: 可斩伤害格(2026-09-18 口径)细节双形态 —— 法力可行链
    (场+线+潜力库) / 理论粗估(手+库+场); 可斩数值转亮橙(lethal 新色键);
    法力格缺数据默认 1; 回费细节按 库+手(卡组、手牌)序; 减费格不变。"""
    root = tk.Tk()
    root.withdraw()                    # 测试不显示 UI
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _StatPanel
        assert ("lethal", "可斩伤害") in _StatPanel._CELLS   # 标签更名
        p = _StatPanel(tk, root, dict(_DEFAULT_COLORS), 10)
        p.update(dict(_BASE))
        assert p.cells["mana"][0].cget("text") == "2"
        assert p.cells["mana"][1].cget("text") == "水晶2/3"
        assert p.cells["lethal"][1].cget("text") == \
            "场0+线0 · 潜力库14 · 法强0"
        assert p.cells["lethal"][0].cget("foreground") == \
            _DEFAULT_COLORS["stat"]                    # 不可斩: 常规色
        p.update({**_BASE, "can_kill": True})
        assert p.cells["lethal"][1].cget("text") == \
            "可斩 场0+线0 · 潜力库14 · 法强0"
        assert p.cells["lethal"][0].cget("foreground") == \
            _DEFAULT_COLORS["lethal"]                  # 可斩: 亮橙新色键
        p.update({**_BASE, "lethal_est": True})        # 理论粗估形态
        assert p.cells["lethal"][1].cget("text") == \
            "理论 手0+库14+场0 · 法强0"
        p.update({k: v for k, v in _BASE.items()
                  if k not in ("mana", "mana_res")})
        assert p.cells["mana"][0].cget("text") == "1"     # 默认 1(用户定版)
        assert p.cells["mana"][1].cget("text") == ""
        p.reset()
        assert p.cells["enemy"][0].cget("text") == "—"
        assert p.cells["mana"][0].cget("text") == "1"
    finally:
        root.destroy()


def test_advice_panel_top3():
    """推荐区: 线行(plan)占首行(可斩金/advice 色), 留牌行其后的 dim 证据行;
    总行数封顶 3; reset 清空。行值驻留: plan 空更新不清线(对手回合),
    仅局终 reset 清空。"""
    root = tk.Tk()
    root.withdraw()
    try:
        from hsbot.overlay import _ADVICE_PANEL_ROWS, _DEFAULT_COLORS, \
            _AdvicePanel
        p = _AdvicePanel(tk, root, dict(_DEFAULT_COLORS), 10)
        p.set_advice([("留 好牌", "advice"), ("卡组胜率 54%", "dim"),
                      ("多余行", "dim")])
        assert [l.cget("text") for l in p._labels] == \
            ["留 好牌", "卡组胜率 54%", "多余行"]
        p.set_plan([("可斩: 火球术(4费) 伤6 ≥ 2", "advice")])
        texts = [l.cget("text") for l in p._labels]
        assert texts[0] == "可斩: 火球术(4费) 伤6 ≥ 2"
        assert len(texts) == _ADVICE_PANEL_ROWS          # 封顶 3(用户 top3)
        assert texts[1:] == ["留 好牌", "卡组胜率 54%"]   # 多余行被截断
        assert p._labels[0].cget("foreground") == _DEFAULT_COLORS["advice"]
        p.set_plan([])                       # 对手回合/未解析: 值驻留不清线
        assert p._labels[0].cget("text") == "可斩: 火球术(4费) 伤6 ≥ 2"
        p.reset()
        assert all(l.cget("text") == "" for l in p._labels)
    finally:
        root.destroy()


def test_advice_panel_line_data_advice_priority():
    """共存截断优先级(用户定版): 线行 > 留牌行 > 数据行, 保持三行内 ——
    线行+留牌行×2 时数据行让位; 线行+留牌行×1 时数据行保留;
    非可斩最优线用常规 stat 色(金只给可斩, 语义两级)。"""
    root = tk.Tk()
    root.withdraw()
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _AdvicePanel
        p = _AdvicePanel(tk, root, dict(_DEFAULT_COLORS), 10)
        opt = [("最优: 火球术(4费) 伤6+场0 vs 敌22", "stat"),
               ("剩费3 · 未覆盖2张", "dim")]
        p.set_plan(opt)
        assert [l.cget("text") for l in p._labels[:2]] == \
            ["最优: 火球术(4费) 伤6+场0 vs 敌22", "剩费3 · 未覆盖2张"]
        assert p._labels[0].cget("foreground") == _DEFAULT_COLORS["stat"]
        assert p._labels[1].cget("foreground") == _DEFAULT_COLORS["stat_dim"]
        p.set_advice([("留 好牌", "advice"), ("卡组胜率 54%", "dim")])
        assert [l.cget("text") for l in p._labels] == \
            ["最优: 火球术(4费) 伤6+场0 vs 敌22",
             "留 好牌", "卡组胜率 54%"]          # 数据行让位(留牌行优先)
        p.set_advice([("留 好牌", "advice")])
        assert [l.cget("text") for l in p._labels] == \
            ["最优: 火球术(4费) 伤6+场0 vs 敌22",
             "留 好牌", "剩费3 · 未覆盖2张"]       # 三行内全容纳
    finally:
        root.destroy()


# ================= overlay: 整窗行为 =================

def _run_capture(monkeypatch, q, data_dir, cfg_extra=None, after_ms=800):
    """冒烟跑完整窗(mainloop 定时销毁, withdraw 不扰前台), 关窗前抓取
    日志文本/面板/推荐区终态。"""
    from hsbot.config import Config
    from hsbot.overlay import OverlayWindow

    cfg = Config.load({"overlay_enabled": True, "overlay_topmost": False,
                       "data_dir": data_dir, **(cfg_extra or {})})
    orig = tk.Tk.mainloop
    holder: dict = {}

    def mainloop_with_close(self, n=0):
        self.withdraw()

        def _close():
            win = holder.get("win")
            if win is not None:
                holder["body"] = win._txt.get("1.0", "end")
                holder["enemy"] = win._panel.cells["enemy"][0].cget("text")
                holder["mana"] = win._panel.cells["mana"][0].cget("text")
                holder["advice"] = [l.cget("text") for l in win._advice._labels]
            self.destroy()

        self.after(after_ms, _close)
        orig(self, n)

    monkeypatch.setattr(tk.Tk, "mainloop", mainloop_with_close)
    import time
    last = None
    for i in range(3):                 # tk DLL 初始化偶发抖动重试(同冒烟测试)
        if i:
            time.sleep(0.5)
        try:
            win = OverlayWindow(cfg, q)
            holder["win"] = win
            win.run()
            return holder
        except tk.TclError as exc:
            last = exc
    raise last


def test_game_end_clears_log_and_resets_panels(ensure_display, monkeypatch,
                                               tmp_path):
    """局终(打完一局): 清空日志流水(仅留终局行) + 重置上区面板(敌血回 —,
    法力回默认 1) + 清空推荐区 —— 用户 2026-09-14 定版。"""
    q = queue.Queue()
    q.put(("chain", "[T1·我] 起手可留: X"))
    q.put(("stat", "敌 40 │ 斩杀 14", "stat", dict(_BASE)))
    q.put(("advice", "【留牌建议·vs牧师·后手】留 好牌 │ 换 坏牌", "advice",
           dict(_ADVICE)))
    end_text = "── T3 g(终局) ──\n──── 对局结束 ──── a=WON / b=LOST"
    q.put(("game_end", end_text))
    h = _run_capture(monkeypatch, q, str(tmp_path))
    assert q.empty()
    assert h["body"].rstrip() == end_text, "局终旧日志未清空"  # get() 恒多一个
    # Text 强制尾换行, 用 rstrip 归一
    assert h["enemy"] == "—", "上区敌血未重置"
    assert h["mana"] == "1", "上区法力未回默认 1"
    assert h["advice"] == ["", "", ""], "推荐区未清空"


_PLAN_STAT = {"enemy_total": 22, "enemy_hp": 22, "enemy_armor": 0,
              "lethal": 11, "lethal_hand": 8, "lethal_deck": None,
              "lethal_board": 3, "can_kill": False, "spellpower": 0,
              "ramp": 0, "ramp_hand": 0, "ramp_deck": None,
              "cost_list": None, "cost_deck": None, "cost_hand": 4,
              "discount": {"cards": 0, "total": 0, "sources": []},
              "mana": 3, "mana_res": 4,
              # analysis.lethal_plan 同构(非可斩): 最优线 + 数据行事实
              "plan": {"actions": [("CS2_029", 4)], "total": 11,
                       "face_det": 8, "face_exp": 0, "lethal": False,
                       "enemy_total": 22, "board_atk": 3,
                       "uncovered_n": 2, "mana_trace": (4, 3)}}

_OPT_LINE = "最优: CS2_029(4费) 伤8+场3 vs 敌22"
_DATA_ROW = "剩费3 · 未覆盖2张"


def test_advice_line_persists_through_opp_turn(ensure_display, monkeypatch,
                                               tmp_path):
    """常驻语义(2026-09-14 用户实测反馈): 我方回合算出的 最优线+数据行
    在后续 stat 更新(plan=None, 对手回合)后持续显示, 不被中途清掉;
    推荐区值只被新事实替换或局终 reset 清空。"""
    q = queue.Queue()
    q.put(("stat", "敌 22 │ 斩杀 11", "stat", dict(_PLAN_STAT)))
    q.put(("stat", "敌 22 │ 斩杀 11", "stat",
           {**_PLAN_STAT, "plan": None}))       # 对手回合: 不携带 plan
    h = _run_capture(monkeypatch, q, str(tmp_path))
    assert q.empty()
    assert h["advice"][:2] == [_OPT_LINE, _DATA_ROW], "推荐区被中途清掉"


def test_advice_line_cleared_on_game_end(ensure_display, monkeypatch,
                                         tmp_path):
    """常驻的唯一出口是局终: game_end 重置推荐区(含线行/数据行),
    绝不残留上一局的建议。"""
    q = queue.Queue()
    q.put(("stat", "敌 22 │ 斩杀 11", "stat", dict(_PLAN_STAT)))
    end_text = "──── 对局结束 ──── a=WON / b=LOST"
    q.put(("game_end", end_text))
    h = _run_capture(monkeypatch, q, str(tmp_path))
    assert q.empty()
    assert h["advice"] == ["", "", ""], "局终推荐区未清空"


def test_transparent_log_and_opaque_panels(ensure_display, monkeypatch,
                                           tmp_path):
    """上/中上部真不透明: 整窗 alpha=1.0, 日志区背景色被 transparentcolor
    镂空(游戏透出, 面板实底); 关闭开关回退旧行为(整窗 overlay_alpha)。"""
    from hsbot.overlay import OverlayWindow, _LOG_BG_MAGIC

    def run_once(cfg_extra):
        cfg_extra = {"overlay_enabled": True, "overlay_topmost": False,
                     "data_dir": str(tmp_path), **cfg_extra}
        from hsbot.config import Config
        cfg = Config.load(cfg_extra)
        orig = tk.Tk.mainloop
        holder: dict = {}

        def mainloop_probe(self, n=0):
            self.withdraw()
            win = holder["win"]
            holder["tc"] = self.attributes("-transparentcolor")
            holder["alpha"] = float(self.attributes("-alpha"))
            holder["txt_bg"] = win._txt.cget("bg")
            self.destroy()

        monkeypatch.setattr(tk.Tk, "mainloop", mainloop_probe)
        win = OverlayWindow(cfg, queue.Queue())
        holder["win"] = win
        win.run()
        return holder

    h = run_once({})
    assert h["tc"] == _LOG_BG_MAGIC, "日志区背景未镂空"
    assert h["alpha"] == 1.0, "不透明模式下整窗应为全不透明"
    assert h["txt_bg"] == _LOG_BG_MAGIC
    h = run_once({"overlay_log_transparent": False, "overlay_alpha": 0.72})
    assert h["tc"] in ("", "transparent")           # 未设置镂空色
    assert h["alpha"] == 0.72
    assert h["txt_bg"] == "#0d1117"


def test_stat_panel_lethal_color_missing_key_falls_back():
    """旧 config 无 lethal 色键: can_kill 回退 stat 色(向后兼容)。"""
    root = tk.Tk()
    root.withdraw()
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _StatPanel
        colors = {k: v for k, v in _DEFAULT_COLORS.items() if k != "lethal"}
        p = _StatPanel(tk, root, colors, 10)
        p.update({**_BASE, "can_kill": True})
        assert p.cells["lethal"][0].cget("foreground") == \
            _DEFAULT_COLORS["stat"]
    finally:
        root.destroy()
