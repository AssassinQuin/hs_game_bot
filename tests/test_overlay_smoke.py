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


def _run_with_auto_close(monkeypatch, q, after_ms=800, data_dir="."):
    """mainloop 注入定时 destroy: 刷新链若断, 定时器仍会收窗, 测试不悬挂。
    窗口一律 withdraw + 不置顶 —— 测试不显示 UI(2026-09-13 用户要求),
    刷新链(insert/after/坏消息隔离)逻辑照常被覆盖。"""
    from hsbot.config import Config
    from hsbot.overlay import OverlayWindow

    cfg = Config.load({"overlay_enabled": True, "overlay_topmost": False,
                       "data_dir": data_dir})
    orig = tk.Tk.mainloop

    def mainloop_with_close(self, n=0):
        self.withdraw()
        self.after(after_ms, self.destroy)
        orig(self, n)

    monkeypatch.setattr(tk.Tk, "mainloop", mainloop_with_close)
    import time
    last = None
    for i in range(3):        # 本机 tk DLL 初始化偶发抖动, 重试
        if i:
            time.sleep(0.5)
        try:
            OverlayWindow(cfg, q).run()
            return
        except tk.TclError as exc:
            last = exc
    raise last


def test_overlay_smoke_all_message_kinds(ensure_display, monkeypatch, tmp_path):
    q = queue.Queue()
    for kind, text in [("notice", "监控会话: Hearthstone_x │ 卡组=奇迹德"),
                       ("chain", "[T1·我] 起手可留: 黑市拍卖师、雷霆绽放、月火术"),
                       ("snapshot", "── T1 第1局(回合结束) 我:水晶1/1 ──"),
                       ("game_end", "──── 对局结束 ──── Player1=WON / Player2=LOST"),
                       ("error", "! 监控线程已退出, 请重启 hsbot")]:
        q.put((kind, text))
    _run_with_auto_close(monkeypatch, q, data_dir=str(tmp_path))


def test_overlay_survives_bad_message(ensure_display, monkeypatch, tmp_path):
    """坏消息(None 文本)曾会让 after 刷新链断掉 → 悬浮窗永久冻结。"""
    q = queue.Queue()
    q.put(("chain", "[T1·我] 正常行"))
    q.put((None, None))                        # 坏形态: insert 时 TypeError
    q.put(("chain", "[T1·我] 坏消息之后的正常行"))
    _run_with_auto_close(monkeypatch, q, data_dir=str(tmp_path))


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

        self.after(300, drag)
        self.after(1800, self.destroy)                            # > 防抖 800ms
        _ORIG_MAINLOOP(self, n)

    monkeypatch.setattr(tk.Tk, "mainloop", fake_mainloop)
    win.run()

    state_file = tmp_path / "overlay_state.json"
    assert state_file.exists(), "几何状态未保存"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    # withdraw 窗口: Windows Tk 不改未映射窗口的尺寸, 位置生效 —— 校验拖动
    # 后的位置被捕获即覆盖"变更→防抖→落盘"链(初始是 +8+120)
    assert data["geometry"].endswith("+40+50")
