"""留牌 AI 训练器: 语料提取 / 平滑统计表 / 版本化训练 / 建议。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "train_mulligan", ROOT / "scripts" / "train_mulligan.py")
tm = importlib.util.module_from_spec(_spec)
sys.modules["train_mulligan"] = tm          # dataclass 解析注解需要可查到模块
_spec.loader.exec_module(tm)

NO_DB = tm.CardDB("/nonexistent/cards.json")   # 降级: name=card_id, cost=None


def _write_game(corpus: Path, name: str, *, battletag="湫然#51704",
                self_pid="1", result="WON", opp_class="PALADIN",
                offered=("GOOD", "BAD", "OK"), kept=("GOOD",),
                decided=True, opp_decided=False, heroes_meta=None) -> Path:
    opp_pid = "1" if self_pid == "2" else "2"
    meta = {
        "_meta": True,
        "players": {self_pid: {"name": battletag, "player_id": int(self_pid)},
                    opp_pid: {"name": "对手#51829", "player_id": int(opp_pid)}},
        "results": {self_pid: result,
                    opp_pid: "LOST" if result == "WON" else "WON"},
        "mulligan": {
            self_pid: {"decided": decided, "offered": list(offered),
                       "kept": list(kept)},
            opp_pid: {"decided": opp_decided, "offered": ["X_1"], "kept": []}},
    }
    if heroes_meta is not None:
        meta["heroes"] = heroes_meta
    lines = [json.dumps(meta, ensure_ascii=False)]
    for pid, cls in ((self_pid, "DRUID"), (opp_pid, opp_class)):
        lines.append(json.dumps({
            "t": "FullEntity", "entity": 70 + int(pid), "card_id": "HERO_06x",
            "tags": [["CONTROLLER", int(pid)], ["CARDTYPE", "HERO"],
                     ["ZONE", "PLAY"], ["CLASS", cls]]}, ensure_ascii=False))
    path = corpus / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── 语料提取 ──

def test_load_game_extracts_class_from_events(tmp_path):
    p = _write_game(tmp_path, "s_g01.jsonl",
                    offered=("GOOD", "GAME_005"), kept=("GOOD",))
    g, why = tm.load_game(p, "湫然#51704")
    assert g is not None and why == ""
    assert g.opp_class == "PALADIN"            # 事件流 CLASS 标签
    assert g.result == 1
    assert g.coin is True                      # 幸运币 → 后手
    assert g.cards == ["GOOD"]                 # 幸运币不进决策
    assert g.kept == ["GOOD"]


def test_load_game_skip_reasons(tmp_path):
    p = _write_game(tmp_path, "a.jsonl", result="PLAYING")
    assert tm.load_game(p, "湫然#51704")[1].startswith("未出结果")
    p = _write_game(tmp_path, "b.jsonl", decided=False)
    assert "留牌未广播" in tm.load_game(p, "湫然#51704")[1]
    # 战网名不匹配 → 兜底取唯一 decided 的一方(pid 2, 对手则是 pid 1 德鲁伊)
    p = _write_game(tmp_path, "c.jsonl", battletag="别人#1", self_pid="1",
                    decided=False, opp_decided=True)
    _write_game(tmp_path, "z.jsonl")           # 同目录另一局不受影响
    g, why = tm.load_game(p, "湫然#51704")
    assert g is not None and g.opp_class == "DRUID"


def test_unknown_class_when_no_hero_info(tmp_path):
    p = _write_game(tmp_path, "s_g01.jsonl")
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join([lines[0]]), encoding="utf-8")  # 只留 _meta
    g, _ = tm.load_game(p, "湫然#51704")
    assert g.opp_class == "UNKNOWN"


# ── 平滑统计表 ──

def _stats_from(games):
    stats = {}
    for g in games:
        tm.table_update(stats, g)
    return stats


def test_table_signal_ordering(tmp_path):
    stats = _stats_from([
        tm.Game("a.jsonl", 1, False, "PALADIN", ["GOOD", "BAD"], ["GOOD"]),
        tm.Game("b.jsonl", 1, False, "PALADIN", ["GOOD", "BAD"], ["GOOD"]),
        tm.Game("c.jsonl", 1, False, "PALADIN", ["GOOD", "BAD"], ["GOOD"]),
        tm.Game("d.jsonl", 1, False, "PALADIN", ["GOOD", "BAD"], ["BAD"]),
        tm.Game("e.jsonl", 0, False, "PALADIN", ["GOOD", "BAD"], ["GOOD", "BAD"]),
        tm.Game("f.jsonl", 0, False, "PALADIN", ["GOOD", "BAD"], ["BAD"]),
    ])
    deck_wr = 4 / 6
    good = tm.card_advice(stats, "GOOD", "PALADIN", None, deck_wr, NO_DB)
    bad = tm.card_advice(stats, "BAD", "PALADIN", None, deck_wr, NO_DB)
    assert good["label"] == "建议留" and bad["label"] == "建议换"
    assert good["gain"] > 0.03 > bad["gain"]
    cell = stats["GOOD"]["PALADIN|0"]
    assert cell["keep"] == {"w": 3, "l": 1} and cell["drop"] == {"w": 1, "l": 1}


def test_cascade_falls_back_to_all():
    # 萨满行只有 1 局(且输了) → 查询时级联回全体; 德鲁伊行 8 局样本足
    stats = _stats_from([tm.Game("a.jsonl", 0, False, "SHAMAN", ["C"], ["C"])])
    for i, (r, kept) in enumerate([(1, ["C"])] * 4 + [(0, ["C"])] + [(0, [])] * 3):
        tm.table_update(stats, tm.Game(f"b{i}.jsonl", r, False, "DRUID",
                                       ["C"], kept))
    adv = tm.card_advice(stats, "C", "SHAMAN", None, 0.5, NO_DB)
    assert adv["src"] == "全体"
    adv_druid = tm.card_advice(stats, "C", "DRUID", None, 0.5, NO_DB)
    assert adv_druid["src"] == "本职业"
    assert adv_druid["gain"] > adv["gain"]   # 本职业正面数据优于混合池


def test_coin_cells_separate():
    stats = _stats_from([tm.Game("a.jsonl", 0, True, "PALADIN", ["C"], [])])
    assert stats["C"]["PALADIN|1"]["drop"]["l"] == 1
    assert "PALADIN|0" not in stats["C"]


# ── 训练 / 版本化 / 增量 ──

def _run(tmp_path, corpus, *extra):
    argv = ["--config", "no-such.yaml", "--data-dir", str(tmp_path / "data"),
            *extra]
    return tm.main(argv)


def test_train_versions_and_increment(tmp_path, capsys):
    corpus = tmp_path / "corpus" / "奇迹德"
    corpus.mkdir(parents=True)
    for i in range(3):
        _write_game(corpus, f"s_g{i:02d}.jsonl")
    assert _run(tmp_path, corpus, "--data", str(tmp_path / "corpus"),
                "--deck", "奇迹德") == 0
    out1 = capsys.readouterr().out
    assert "语料 3 局" in out1 and "逻辑回归: 未启用" in out1
    root = tmp_path / "data" / "models" / "mulligan" / "奇迹德"
    assert (root / "LATEST.json").exists()
    assert json.loads((root / "v001" / "meta.json").read_text(
        encoding="utf-8"))["n_games"] == 3
    # 增量: 追加一局 → 新版本只报新增 1
    _write_game(corpus, "s_g03.jsonl", result="LOST")
    assert _run(tmp_path, corpus, "--data", str(tmp_path / "corpus"),
                "--deck", "奇迹德") == 0
    out2 = capsys.readouterr().out
    assert "语料 4 局(较上一版新增 1)" in out2
    assert "v002" in out2 and "上一版 v001" in out2
    # 内容未变 → 重跑新增 0
    assert _run(tmp_path, corpus, "--data", str(tmp_path / "corpus"),
                "--deck", "奇迹德") == 0
    assert "新增 0" in capsys.readouterr().out


def test_advise_without_lr_uses_table(tmp_path, capsys):
    corpus = tmp_path / "corpus" / "奇迹德"
    corpus.mkdir(parents=True)
    # 8 局带信号: GOOD 6胜留/1负换 → 建议留; BAD 只在败局留 → 建议换
    outcomes = [("WON", ["GOOD", "BAD", "OK"], ["GOOD"])] * 6 + \
               [("LOST", ["GOOD", "BAD", "OK"], ["GOOD", "BAD"]),
                ("LOST", ["GOOD", "BAD", "OK"], ["BAD"])]
    for i, (r, off, kept) in enumerate(outcomes):
        _write_game(corpus, f"s_g{i:02d}.jsonl", result=r, offered=off,
                    kept=kept)
    pre = ["--config", "no-such.yaml", "--data-dir", str(tmp_path / "data")]
    post = ["--data", str(tmp_path / "corpus"), "--deck", "奇迹德"]
    assert tm.main(pre + ["train"] + post) == 0
    capsys.readouterr()
    assert tm.main(pre + ["advise"] + post +
                   ["--vs", "圣骑士", "--first", "--hand", "GOOD,BAD"]) == 0
    out = capsys.readouterr().out
    assert "统计表" in out                     # 语料过小, 无 LR
    assert "留 ── GOOD" in out and "建议留" in out and "建议换" in out
