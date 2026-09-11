"""OutputHub 双文本分发: ui 进控制台/队列, full 进文件。"""
from hsbot.overlay import Msg, OutputHub


def test_msg_full_defaults_to_ui():
    m = Msg("chain", "[T1·我] 抽到 X")
    assert m.full == m.ui


def test_hub_dispatch_console_queue_file(tmp_path, capsys):
    hub = OutputHub(console=True, to_overlay=True)
    hub.set_file(tmp_path / "session.log")
    hub(Msg("chain", "链路行"))
    hub(Msg("snapshot", ui="── T1 单行 ──", full="完整快照块\n多行"))
    out = capsys.readouterr().out
    assert "链路行" in out and "── T1 单行 ──" in out
    assert "完整快照块" not in out                 # 控制台只见 ui
    assert hub.q is not None
    assert hub.q.get_nowait() == ("chain", "链路行")
    assert hub.q.get_nowait() == ("snapshot", "── T1 单行 ──")
    content = (tmp_path / "session.log").read_text(encoding="utf-8")
    assert "链路行" in content
    assert "完整快照块\n多行" in content            # 文件见 full


def test_hub_without_overlay_and_console(tmp_path):
    hub = OutputHub(console=False, to_overlay=False)
    assert hub.q is None
    hub(Msg("notice", "x"))                     # 不抛错即可
