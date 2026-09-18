"""悬浮窗冒烟: 全形态消息刷新不炸; 单条坏消息不杀死 120ms 刷新链(2026-09-13)。
行分类(人物/事件 → 颜色)与窗口几何记忆。"""
import queue

import pytest

tk = pytest.importorskip("tkinter")
_ORIG_MAINLOOP = tk.Tk.mainloop


@pytest.fixture()
def ensure_display():
    try:
        root = tk.Tk()
        root.withdraw()          # 探测窗不闪现桌面(测试不扰机器前台)
        root.destroy()
    except Exception as exc:  # noqa: BLE001  无显示环境
        pytest.skip(f"无可用显示: {exc}")


def _run_with_auto_close(monkeypatch, q, after_ms=360, data_dir="."):
    # 360ms = 3 个 120ms 刷新周期(消息 run() 前已全部入队, 首 poll 即清空)
    """mainloop 注入定时 destroy: 刷新链若断, 定时器仍会收窗, 测试不悬挂。
    窗口一律 withdraw + 不置顶 —— 测试不显示 UI(2026-09-13 用户要求),
    刷新链(insert/after/坏消息隔离)逻辑照常被覆盖。"""
    from hsbot.config import Config
    from hsbot.overlay import OverlayWindow

    cfg = Config.load({"overlay_enabled": True, "overlay_topmost": False,
                       "data_dir": data_dir})
    orig = tk.Tk.mainloop
    holder: dict = {}          # 销毁前抓取已渲染文本(mainloop 退出后 Tk 命令失效)

    def mainloop_with_close(self, n=0):
        self.withdraw()

        def _close():
            win = holder.get("win")
            if win is not None:
                holder["body"] = win._txt.get("1.0", "end")
            self.destroy()

        self.after(after_ms, _close)
        orig(self, n)

    monkeypatch.setattr(tk.Tk, "mainloop", mainloop_with_close)
    import time
    last = None
    for i in range(3):        # 本机 tk DLL 初始化偶发抖动, 重试
        if i:
            time.sleep(0.5)
        try:
            win = OverlayWindow(cfg, q)
            holder["win"] = win
            win.run()
            return win, holder.get("body", "")
        except tk.TclError as exc:
            last = exc
    raise last


def test_overlay_smoke_all_message_kinds(ensure_display, monkeypatch, tmp_path):
    """全形态消息刷新不炸; 局终(game_end)清空日志流水(2026-09-14 三区布局
    定版): game_end 行本身保留, 其后的行(下一局内容)照常渲染。"""
    q = queue.Queue()
    q.put(("game_end", "──── 对局结束 ──── Player1=WON / Player2=LOST"))
    for kind, text in [("notice", "监控会话: Hearthstone_x │ 卡组=奇迹德"),
                       ("chain", "[T1·我] 起手可留: 黑市拍卖师、雷霆绽放、月火术"),
                       ("snapshot", "── T1 第1局(回合结束) 我:水晶1/1 ──"),
                       ("error", "! 监控线程已退出, 请重启 hsbot")]:
        q.put((kind, text))
    # 新形态: KIND_STAT 带机读字段(4 元组) → 上区走分格面板
    q.put(("stat", "敌 40(40血+0甲) │ 斩杀 14(场0+线0) │ 法强 0\n"
                   "回费 +10(手2+库8) │ 费 组45/库33/手12 │ 减2(生命缚誓者的礼物)",
           "stat",
           {"enemy_total": 40, "enemy_hp": 40, "enemy_armor": 0,
            "lethal": 14, "lethal_hand": 0, "lethal_deck": 14,
            "lethal_board": 0, "can_kill": False, "lethal_est": False,
            "spellpower": 0,
            "ramp": 10, "ramp_hand": 2, "ramp_deck": 8,
            "cost_list": 45, "cost_deck": 33, "cost_hand": 12,
            "discount": {"cards": 1, "total": 2,
                         "sources": ["生命缚誓者的礼物"]}}))
    _, body = _run_with_auto_close(monkeypatch, q, data_dir=str(tmp_path))
    assert q.empty(), "消息队列应被消费完"
    for text in ("起手可留", "对局结束", "监控线程已退出"):
        assert text in body, f"未渲染: {text}"


def test_stat_panel_fields_and_kill_color(ensure_display):
    """上区六格面板: 机读字段驱动数值/细节三层字级; 可斩转亮橙(lethal 色);
    法力格缺数据默认 1(2026-09-14 三区布局定版);
    无机读字段的旧形态走原文兜底行。"""
    root = tk.Tk()
    root.withdraw()                    # 测试不显示 UI
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _StatPanel
        p = _StatPanel(tk, root, dict(_DEFAULT_COLORS), 10)
        base = {"enemy_total": 40, "enemy_hp": 40, "enemy_armor": 0,
                "lethal": 14, "lethal_hand": 0, "lethal_deck": 14,
                "lethal_board": 0, "can_kill": False, "lethal_est": False,
                "spellpower": 0,
                "ramp": 10, "ramp_hand": 2, "ramp_deck": 8,
                "cost_list": 45, "cost_deck": 33, "cost_hand": 12,
                "discount": {"cards": 1, "total": 2,
                             "sources": ["生命缚誓者的礼物"]}}
        p.update(base)
        assert p.cells["enemy"][0].cget("text") == "40"
        assert p.cells["enemy"][1].cget("text") == "40血+0甲"
        assert p.cells["lethal"][0].cget("text") == "14"
        assert p.cells["lethal"][1].cget("text") == "场0+线0 · 潜力库14 · 法强0"
        assert p.cells["mana"][0].cget("text") == "1"      # 缺数据: 默认 1
        assert p.cells["ramp"][0].cget("text") == "+10"
        assert p.cells["ramp"][1].cget("text") == "库8+手2"
        assert p.cells["cost"][0].cget("text") == "12"
        assert p.cells["cost"][1].cget("text") == "组45 库33"
        assert p.cells["discount"][0].cget("text") == "−2"
        assert p.cells["discount"][1].cget("text") == "生命缚誓者的礼物"
        p.update({**base, "can_kill": True})
        assert p.cells["lethal"][0].cget("foreground") == _DEFAULT_COLORS["lethal"]
        assert p.cells["lethal"][1].cget("text") == "可斩 场0+线0 · 潜力库14 · 法强0"
        p.update({**base, "mana": 5, "mana_res": 10})
        assert p.cells["mana"][0].cget("text") == "5"
        assert p.cells["mana"][1].cget("text") == "水晶5/10"
        p.update({**base, "lethal_deck": None, "cost_list": None})
        assert "库?" in p.cells["lethal"][1].cget("text")
        p.update({**base, "discount": {"cards": 0, "total": 0, "sources": []}})
        assert p.cells["discount"][0].cget("text") == "0"
        assert p.cells["discount"][1].cget("text") == "—"
        p.set_raw("兜底文本行")
        assert p._raw.cget("text") == "兜底文本行"
    finally:
        root.destroy()


def test_overlay_survives_bad_message(ensure_display, monkeypatch, tmp_path):
    """坏消息(None 文本)曾会让 after 刷新链断掉 → 悬浮窗永久冻结。"""
    q = queue.Queue()
    q.put(("chain", "[T1·我] 正常行"))
    q.put((None, None))                        # 坏形态: insert 时 TypeError
    q.put(("chain", "[T1·我] 坏消息之后的正常行"))
    _, body = _run_with_auto_close(monkeypatch, q, data_dir=str(tmp_path))
    # 坏消息只丢自己: 之后的正常行必须已渲染(否则刷新链断了 → 冻结)
    assert "坏消息之后的正常行" in body
    assert q.empty()


def test_overlay_classify_lines():
    """行分类 → 颜色标签: 人物(我/对面/未知)+事件类型。"""
    from hsbot.overlay import OverlayWindow
    C = OverlayWindow._classify
    assert C("chain", "[T1·我] 抽到 月火术") == "my"
    assert C("chain", "[T2·对面] 打出 洛欧塞布 (5费)") == "opp"
    assert C("chain", "[T9·?] 攻击 X → Y") == "unknown"
    assert C("snapshot", "── T1 第1局(回合结束) ...") == "snapshot"
    assert C("game_end", "──── 对局结束 ──── a=LOST") == "game_end"
    assert C("notice", "──── 第1局 · 我的第1回合开始 (T1) ────") == "header"
    assert C("notice", "── 新对局 #1 ── 所选卡组: 奇迹德") == "notice"
    assert C("notice", "! 监控线程已退出") == "error"
    assert C("stat", "敌 30 │ 斩杀 0") == "stat"


def test_overlay_geometry_persisted(tmp_path, monkeypatch):
    """窗口位置/大小记忆: 拖动后关窗, overlay_state.json 留下 geometry。"""
    import json
    from hsbot.config import Config
    from hsbot.overlay import OverlayWindow

    cfg = Config.load({"overlay_enabled": True, "overlay_topmost": False,
                       "data_dir": str(tmp_path)})
    q = queue.Queue()
    win = OverlayWindow(cfg, q)

    class _Ev:                                 # Configure 事件最小桩
        widget = None

    def fake_mainloop(self, n=0):
        self.withdraw()                                           # 测试不弹窗
        _Ev.widget = self

        def drag():
            self.geometry("500x600+40+50")
            win._on_configure(_Ev())    # withdraw 无 Configure 事件, 手动喂

        self.after(100, drag)
        self.after(1050, self.destroy)                            # drag+防抖800ms=900, 余150ms
        _ORIG_MAINLOOP(self, n)

    monkeypatch.setattr(tk.Tk, "mainloop", fake_mainloop)
    win.run()

    state_file = tmp_path / "overlay_state.json"
    assert state_file.exists(), "几何状态未保存"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    # withdraw 窗口: Windows Tk 不改未映射窗口的尺寸, 位置生效 —— 校验拖动
    # 后的位置被捕获即覆盖"变更→防抖→落盘"链(初始是 +8+120)
    assert data["geometry"].endswith("+40+50")
