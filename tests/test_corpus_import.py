"""corpus 切片回放导入: 客户端会轮转删除老会话原始日志, 语料自带的
.power.log 切片是唯一幸存的原始文本源 —— 用它把旧 schema 样本重建到
当前 meta(2026-09-14 审计遗留: 79 局无 game_id/heroes 缺失)。"""
import json
from pathlib import Path

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.corpus import CorpusExporter

FIXTURE = Path(__file__).parent / "fixtures" / "mini_game.log"
SESSION = "Hearthstone_2026_09_09_15_14_59"
DECK = "奇迹德"


def _exporter(tmp_path) -> CorpusExporter:
    cfg = Config(data_dir=str(tmp_path),
                 training_dir=str(tmp_path / "training"),
                 logs_dir=str(tmp_path / "Logs"))     # 空 = 模拟原日志已删
    return CorpusExporter(cfg, CardDB("/nonexistent.json"))


def _seed_legacy_sample(ex, *, idx=2):
    """旧 schema 样本: meta 无 game_id/heroes, 但卡组归因是历史事实。"""
    d = Path(ex.cfg.training_dir) / DECK
    d.mkdir(parents=True)
    old_meta = {"_meta": True, "source": "live", "session": SESSION,
                "game_index": idx, "start_ts": "2026-09-09 15:20:00",
                "turns": 9, "deck_name": DECK, "deck_code": "dead-code",
                "decklist": {"CS2_029": 2}}
    base = d / f"{SESSION}_g{idx:02d}"
    base.with_suffix(".jsonl").write_text(
        json.dumps(old_meta, ensure_ascii=False) + "\n", encoding="utf-8")
    base.with_suffix(".power.log").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return base


def _meta(ex, base):
    lines = base.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])


def test_import_slices_rebuilds_meta_and_carries_deck(tmp_path):
    ex = _exporter(tmp_path)
    base = _seed_legacy_sample(ex)
    ex.import_slices()
    meta = _meta(ex, base)
    assert meta["game_id"], "新 schema 合并主键必须由切片回放重建"
    assert meta["heroes"], "英雄事实由 store 重建(旧样本 _meta.heroes 大面积缺失)"
    assert meta["deck_code"] == "dead-code", "Decks.log 已删, 归因从旧 meta 携带"
    assert meta["decklist"] == {"CS2_029": 2}
    assert f"{SESSION}|2" in json.loads(ex.index_path.read_text(encoding="utf-8"))


def test_import_slices_skips_indexed_existing(tmp_path):
    ex = _exporter(tmp_path)
    base = _seed_legacy_sample(ex)
    ex._save_index({f"{SESSION}|2"})
    ex.import_slices()                       # 已索引且 jsonl 在: 与 import-all 同纪律, 不动
    assert "game_id" not in _meta(ex, base)


def test_import_slices_yields_to_live_log_session(tmp_path):
    ex = _exporter(tmp_path)
    base = _seed_legacy_sample(ex)
    sess = Path(ex.cfg.logs_dir) / SESSION
    sess.mkdir(parents=True)
    (sess / "Power.log").write_text(
        FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    ex.import_slices()                       # 原始日志还在: 归 import-all 管, 切片导入不插手
    assert "game_id" not in _meta(ex, base)
