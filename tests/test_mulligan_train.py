"""留牌 AI 训练器: 语料提取 / 平滑统计表 / 版本化训练 / 建议。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hsbot.render import mulligan_verdict
from trainer import mulligan as tm

ROOT = Path(__file__).resolve().parents[1]

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
    assert mulligan_verdict(good) == "建议留" and mulligan_verdict(bad) == "建议换"
    assert good["gain"] > 0.03 > bad["gain"]
    assert good["src"] == "class" and bad["src"] == "class"   # 未指定先/后手 → 职业格
    cell = stats["GOOD"]["PALADIN|0"]
    assert cell["keep"] == {"w": 3, "l": 1} and cell["drop"] == {"w": 1, "l": 1}


def test_cascade_falls_back_to_all():
    # 萨满行只有 1 局(且输了) → 查询时级联回全体; 德鲁伊行 8 局样本足
    stats = _stats_from([tm.Game("a.jsonl", 0, False, "SHAMAN", ["C"], ["C"])])
    for i, (r, kept) in enumerate([(1, ["C"])] * 4 + [(0, ["C"])] + [(0, [])] * 3):
        tm.table_update(stats, tm.Game(f"b{i}.jsonl", r, False, "DRUID",
                                       ["C"], kept))
    adv = tm.card_advice(stats, "C", "SHAMAN", None, 0.5, NO_DB)
    assert adv["src"] == "all"
    adv_druid = tm.card_advice(stats, "C", "DRUID", None, 0.5, NO_DB)
    assert adv_druid["src"] == "class"
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
    # 内容未变 → 重跑不空转版本(守卫: 沿用旧版)
    assert _run(tmp_path, corpus, "--data", str(tmp_path / "corpus"),
                "--deck", "奇迹德") == 0
    assert "无新增对局, 沿用 v002" in capsys.readouterr().out


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


# ── v2: 专家先验 / 集合枚举 / Thompson / 同情境匹配 ──

PRIOR = {"cards": {"X": {"keep": 0.10, "keep_coin": 0.20},
                   "Y": {"keep": -0.10}},
         "pairs": {}, "engine": []}


def test_prior_fills_cards_without_data():
    stats = {}
    adv = tm.card_advice(stats, "X", "PALADIN", None, 0.5, NO_DB, PRIOR)
    assert adv["src"] == "prior" and mulligan_verdict(adv) == "建议留"
    assert adv["gain"] == 0.10
    adv_coin = tm.card_advice(stats, "X", "PALADIN", 1, 0.5, NO_DB, PRIOR)
    assert adv_coin["gain"] == 0.20            # keep_coin 覆盖
    adv_y = tm.card_advice(stats, "Y", "PALADIN", None, 0.5, NO_DB, PRIOR)
    assert mulligan_verdict(adv_y) == "建议换"
    # 无先验卡仍走费用启发 → 样本不足
    assert mulligan_verdict(
        tm.card_advice(stats, "Z", "PALADIN", None, 0.5, NO_DB, PRIOR)) == "样本不足"


def test_prior_file_loader(tmp_path):
    p = tmp_path / "mulligan_prior.yaml"
    p.write_text(
        "奇迹德:\n"
        "  engine: [GOOD]\n"
        "  cards:\n"
        "    GOOD: {keep: 0.08}\n"
        "  pairs:\n"
        "    - {cards: [GOOD, BAD], bonus: 0.05}\n", encoding="utf-8")
    prior = tm.load_prior(p, "奇迹德", NO_DB, {"GOOD", "BAD"})
    assert prior["cards"]["GOOD"]["keep"] == 0.08
    assert prior["pairs"] == {("BAD", "GOOD"): 0.05}
    assert prior["engine"] == ["GOOD"]
    # 卡组不匹配 → 空先验, 不报错
    assert tm.load_prior(p, "别的卡组", NO_DB, {"GOOD"}) == \
        {"cards": {}, "pairs": {}, "engine": []}


def test_best_keep_set_synergy_beats_additive():
    gains = {"A": -0.01, "B": -0.01}           # 单看都不留
    assert tm.best_keep_set(["A", "B"], gains, {}) == []
    keep = tm.best_keep_set(["A", "B"], gains, {("A", "B"): 0.05})
    assert sorted(keep) == ["A", "B"]          # 协同让组合变最优
    # 正增益卡必留; 零增益卡不并入(平分取最小集)
    assert tm.best_keep_set(["C", "D"], {"C": 0.05, "D": 0.0}, {}) == ["C"]


def test_thompson_explores_uncertain_cards():
    stats = {}
    tm.table_update(stats, tm.Game("a.jsonl", 0, False, "PALADIN", ["C"], []))
    tm.table_update(stats, tm.Game("b.jsonl", 0, False, "PALADIN", ["C"], []))
    rng = tm_random(1)
    samples = [tm.thompson_gain(stats, "C", "PALADIN", 0, 0.5, NO_DB,
                                {"cards": {}, "pairs": {}, "engine": []}, rng)
               for _ in range(40)]
    assert any(s > 0 for s in samples)         # 从未留过 → 后验宽 → 会探索留
    # 双侧高样本且胜率各半 → 采样收敛到均值附近(利用)
    for i in range(90):                        # 留/换两侧都是约一半胜
        kept = ["D"] if i % 2 == 0 else []
        result = 1 if i % 4 in (0, 3) else 0
        tm.table_update(stats, tm.Game(f"d{i}.jsonl", result, False, "PALADIN",
                                       ["D"], kept))
    rng = tm_random(2)
    tight = [tm.thompson_gain(stats, "D", "PALADIN", 0, 0.5, NO_DB,
                              {"cards": {}, "pairs": {}, "engine": []}, rng)
             for _ in range(30)]
    assert max(abs(s) for s in tight) < 0.25


def tm_random(seed):
    import random
    return random.Random(seed)


def test_matched_evidence_levels():
    g = lambda f, k, r: {"c": "PALADIN", "o": 0, "f": f, "k": k, "r": r}
    near = [g(["X", "A", "B"], ["X"], 1), g(["X", "A", "C"], ["X"], 1),
            g(["X", "A", "D"], [], 0), g(["X", "A", "E"], ["X"], 0)]
    far = [g(["Y", "Z"], ["X"], 1)]            # 共享不足 2 张
    digest = near + far
    ev = tm.matched_evidence(digest, "X", ["X", "A", "Q"], "PALADIN", 0)
    assert ev["level"] == "near" and ev["n"] == 4
    assert ev["keep"] == (2, 1) and ev["drop"] == (0, 1)
    ev1 = tm.matched_evidence(digest[:1], "X", ["X", "A"], "PALADIN", 0)
    assert ev1["level"] == "none"
    # 手牌不近似但同职业同手样本足 → 降级到同职业同手
    many_far = [g(["X", "Y"], ["X"], r) for r in (1, 0, 1, 0)]
    ev2 = tm.matched_evidence(many_far, "X", ["X", "A"], "PALADIN", 0)
    assert ev2["level"] == "same"


def test_train_saves_digest_and_advise_shows_matched(tmp_path, capsys):
    corpus = tmp_path / "corpus" / "奇迹德"
    corpus.mkdir(parents=True)
    # 4 局近似手牌(共享≥2张), X 留换两侧都有
    for i, (kept, r) in enumerate([(["GOOD", "BAD"], 1), (["GOOD"], 1),
                                   ([], 0), (["GOOD"], 0)]):
        _write_game(corpus, f"s_g{i:02d}.jsonl", result="WON" if r else "LOST",
                    offered=("GOOD", "BAD", "OK"), kept=tuple(kept))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "mulligan_prior.yaml").write_text(
        "奇迹德:\n  cards:\n    GOOD: {keep: 0.08}\n", encoding="utf-8")
    pre = ["--config", "no-such.yaml", "--data-dir", str(tmp_path / "data")]
    post = ["--data", str(tmp_path / "corpus"), "--deck", "奇迹德"]
    assert tm.main(pre + ["train"] + post) == 0
    capsys.readouterr()
    digest_path = (tmp_path / "data" / "models" / "mulligan" / "奇迹德"
                   / "v001" / "games_digest.json")
    assert digest_path.exists()
    assert tm.main(pre + ["advise"] + post +
                   ["--vs", "圣骑士", "--first", "--hand", "GOOD,BAD"]) == 0
    out = capsys.readouterr().out
    assert "近似手牌" in out or "同职业同手" in out
    # 探索局: 固定种子可复现, 输出标注探索
    assert tm.main(pre + ["advise"] + post +
                   ["--vs", "圣骑士", "--first", "--explore", "--seed", "3",
                    "--hand", "GOOD,BAD"]) == 0
    assert "Thompson 探索局" in capsys.readouterr().out
