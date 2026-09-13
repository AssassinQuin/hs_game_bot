"""审计 2026-09-13 高危/中危钉子: Config 默认值隔离、SessionStore 防竞争、原子写。"""
import json

from hsbot.config import DEFAULTS, Config
from hsbot.persist import SessionStore, atomic_write_text


def test_config_mutable_defaults_not_shared():
    """高危#2: overlay_colors 是可变 dict 默认值 —— 任一实例原地改色
    不得污染其他实例, 更不得污染 DEFAULTS 表。"""
    a, b = Config(), Config()
    a.overlay_colors["my"] = "#ffffff"
    assert b.overlay_colors["my"] == "#b8e6a0"
    assert DEFAULTS["overlay_colors"]["my"] == "#b8e6a0"


def test_session_store_dir_unique_same_second(tmp_path, monkeypatch):
    """高危#3: 同秒双开两个 bot —— 目录名带随机后缀, 不再交错写同一批文件。"""
    monkeypatch.setattr("hsbot.persist.time.strftime",
                        lambda *_a, **_k: "20260913_120000")
    seq = iter(["aaaa", "bbbb"])
    monkeypatch.setattr("hsbot.persist.secrets.token_hex", lambda n: next(seq))
    s1, s2 = SessionStore(tmp_path), SessionStore(tmp_path)
    assert s1.root != s2.root
    assert s1.root.parent == tmp_path


def test_atomic_write_text_replaces_and_leaves_no_tmp(tmp_path):
    """中#6: 整文件覆盖走 临时文件+os.replace, 中途崩溃不产生截断文件。"""
    p = tmp_path / "x.json"
    atomic_write_text(p, "v1")
    atomic_write_text(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_text_makes_parents(tmp_path):
    p = tmp_path / "a" / "b" / "x.jsonl"
    atomic_write_text(p, "{}\n")
    assert p.exists()


def test_draw_dedup_configurable():
    """store 抽牌去重窗口不再硬编码 0.5, 从配置进入。"""
    assert Config().draw_dedup_seconds == 0.5
    assert Config(draw_dedup_seconds="1.5").draw_dedup_seconds == 1.5


def test_imported_index_is_json_list(tmp_path):
    """_imported.json 原子写 + 排序输出(import-all/live 共用索引)。"""
    from hsbot.carddb import CardDB
    from hsbot.corpus import CorpusExporter
    cfg = Config(data_dir=str(tmp_path),
                 training_dir=str(tmp_path / "training"))
    ex = CorpusExporter(cfg, CardDB("/nonexistent.json"))
    ex._save_index({"b|2", "a|1"})
    done = json.loads(ex.index_path.read_text(encoding="utf-8"))
    assert done == ["a|1", "b|2"]
