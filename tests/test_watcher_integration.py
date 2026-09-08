"""端到端: fixture 日志 -> watcher 回放 -> 链路/快照/JSONL 输出。"""
import json
from pathlib import Path

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.watcher import Watcher

FIXTURE = Path(__file__).parent / "fixtures" / "mini_game.log"


def _run(tmp_path):
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path)})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    lines: list[str] = []
    w = Watcher(cfg, carddb, out=lines.append)
    w.run_replay(FIXTURE)
    return "\n".join(lines), tmp_path / "sessions"


def test_replay_produces_chain_snapshot_jsonl(tmp_path):
    text, sessions = _run(tmp_path)
    assert "新对局 #1" in text
    assert "完整快照" in text                      # 回合切换触发了快照
    assert "对局结束" in text                      # 终局行
    assert "1=WON" in text and "2=LOST" in text     # 终局行双方胜负完整(批尾快照)
    assert "快照导出失败" not in text              # 旧路径的失败模式必须消失
    assert "打出 CS2_029" in text                  # PLAY 块延迟发(fixture 友方未解析, 事件仍按 ctrl 发)
    assert "触发" in text                          # 新事件(spec §3.4)
    # 注: 三条抽牌路径均以 friendly 为门, fixture 未解析友方故无"抽到"行 ——
    # 抽牌路径由 test_store_core / test_store_blocks 单测覆盖
    jsonl = sorted(sessions.glob("*/game_001.jsonl"))
    assert jsonl, "JSONL 未落盘"
    payload = json.loads(jsonl[0].read_text(encoding="utf-8").splitlines()[0])
    assert payload["reason"] and payload["players"] and "me" in payload


def test_gamestate_module_deleted():
    import hsbot
    assert not (Path(hsbot.__file__).parent / "gamestate.py").exists()
