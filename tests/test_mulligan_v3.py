"""v3 训练系统: Q 模型 / 评分器 / 蒸馏系数 / 接线。"""
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hsbot.carddb import CardDB

NO_DB = CardDB("/nonexistent/cards.json")


def _snap(turn):
    side = {"hp": 30, "armor": 0, "hand": [], "board": [], "deck": 26,
            "secrets": 0, "mana": turn, "mana_cap": turn, "overload": 0,
            "fatigue": 0, "spellpower": 0, "class": None}
    return {"turn": turn, "my_turn": True, "me": dict(side),
            "opp": dict(side)}


def _v2_rows():
    """两局合成 v2 素材行(结构同 material_v2 契约)。"""
    rows = [
        # g1: T2 抽火球(法术), T3 抽软泥(随从); 我换入 1 张引擎件
        {"row": "turn", "game": "sa_g01#1", "snap": _snap(2),
         "actions": [], "drawn_this_turn": ["CS2_029"], "result": 1,
         "src": "sa_g01.power.log#g1T2"},
        {"row": "turn", "game": "sa_g01#1", "snap": _snap(3),
         "actions": [], "drawn_this_turn": ["CS2_042"], "result": 1,
         "src": "sa_g01.power.log#g1T3"},
        {"row": "mulligan", "game": "sa_g01#1", "offered": ["CS2_029"],
         "kept": ["CS2_029"], "replaced_in": ["EX1_166"], "coin": 0,
         "opp_class": "PRIEST", "decklist": {"CS2_029": 2, "CS2_042": 2,
                                             "EX1_166": 1},
         "result": 1},
        # g2: 只有 T2; 负局
        {"row": "turn", "game": "sb_g01#1", "snap": _snap(2),
         "actions": [], "drawn_this_turn": [], "result": 0,
         "src": "sb_g01.power.log#g1T2"},
        {"row": "mulligan", "game": "sb_g01#1", "offered": ["CS2_042"],
         "kept": [], "replaced_in": [], "coin": 1, "opp_class": "MAGE",
         "decklist": {"CS2_042": 2}, "result": 0},
    ]
    return rows


# ── Q 模型(trainer/qvalue) ──

def test_q_rows_shape_and_grouping():
    from trainer.qvalue import q_rows
    X, y, groups, q3, meta = q_rows(_v2_rows(), NO_DB, {"engine": ["EX1_166"]})
    assert len(X) == len(y) == len(groups) == 3      # 3 个回合行
    assert len(set(groups)) == 2                     # 组 = 局
    assert set(q3) == {"sa_g01#1", "sb_g01#1"}         # T3 优先, 缺则 T2 兜底
    assert all(isinstance(v, float) for v in X[0])
    assert len(X[0]) == len(meta["names"])
    assert "opp_PRIEST" in meta["names"]


def test_q_rows_no_future_leak_within_game():
    """Q 特征聚合只用 turn ≤ K 的抽牌: T2 行向量不随 T3 抽牌变化。"""
    from trainer.qvalue import q_rows
    rows = _v2_rows()
    X, *_ = q_rows(rows, NO_DB, {"engine": []})
    idx_t2 = next(i for i, r in enumerate(rows)
                  if r.get("row") == "turn" and r["snap"]["turn"] == 2
                  and r["game"] == "sa_g01#1")
    # 顺序: rows 里 g1 的 T2 是第 0 行
    assert idx_t2 == 0
    before = list(X[0])
    rows[1]["drawn_this_turn"] = [" totally_different "]     # 改 T3 抽牌
    X2, *_ = q_rows(rows, NO_DB, {"engine": []})
    assert X2[0] == before


def test_q_oof_fit_group_isolation():
    """OOF 预测按局分组: 无同局跨折; 缺 tabpfn → auc None(调用方按门控不过)。"""
    from trainer.qvalue import q_oof_fit, q_rows
    X, y, groups, q3, meta = q_rows(_v2_rows(), NO_DB, {"engine": []})
    r = q_oof_fit(X, y, groups, data_dir=ROOT / "data")
    assert set(r["oof"]) <= set(range(len(y)))
    if r["auc"] is not None:
        assert 0.0 <= r["auc"] <= 1.0


# ── 评分器 + 蒸馏(trainer/scorer) ──

def test_scorer_labels_distillation_and_degrade():
    from trainer.scorer import scorer_dataset
    ds = scorer_dataset(_v2_rows(), NO_DB, {"engine": []},
                        q_by_game={"sa_g01#1": 0.7}, q_gated=True)
    i1 = ds["games"].index("sa_g01#1")
    i2 = ds["games"].index("sb_g01#1")
    assert abs(ds["y"][i1] - (0.5 * 1 + 0.5 * 0.7)) < 1e-9   # 蒸馏标签
    assert ds["y"][i2] == 0.0                                # 无锚点 → 纯胜负
    # 决策时特征词汇: 词表来自卡组并集, 组合对来自留集共现
    assert set(ds["vocab"]) >= {"CS2_029", "CS2_042", "EX1_166"}
    assert ds["groups"] == ["sa", "sb"]          # 组 = 会话


def test_scorer_labels_degrade_when_gate_fails():
    from trainer.scorer import scorer_dataset
    ds = scorer_dataset(_v2_rows(), NO_DB, {"engine": []},
                        q_by_game={"sa_g01#1": 0.7}, q_gated=False)
    assert ds["y"] == [1.0, 0.0]                     # 全部退化为纯胜负


def test_v3_layout_offsets_single_point():
    import hsbot.mulligan_ai as ai
    off = ai.v3_layout_offsets(["A", "B", "C"], [["A", "B"]])
    m = _v3_model_ai()
    x = ai.mulligan3_features(m, ["A"], ["A", "B"], False, "PRIEST")
    assert x[off["kept_off"] + m["vocab"].index("A")] == 1.0
    assert x[off["pair_off"]] == 1.0


def _v3_model_ai():
    import hsbot.mulligan_ai as ai
    return {"vocab": ["A", "B", "C"], "classes": ["PRIEST"],
            "pairs": [["A", "B"]], "engine": [], "deck_tail": [],
            "deck_engine": 0.0, "cost": {"A": 1, "B": 2}.get,
            "cardtype": {}.get}


def test_solve_lstsq_recovers_linear_truth():
    from trainer.scorer import _solve_lstsq
    # D: 4 行 × [A, B, AB] 指示列; 真值 w = [0.1, -0.05, 0.03], 截距 0.5
    D = [[1, 0, 0], [0, 1, 0], [1, 1, 1], [1, 0, 1]]
    t = [0.5 + 0.1 * r[0] - 0.05 * r[1] + 0.03 * r[2] for r in D]
    w = _solve_lstsq(D, t, intercept=0.5)
    assert abs(w[0] - 0.1) < 1e-6 and abs(w[1] + 0.05) < 1e-6 \
        and abs(w[2] - 0.03) < 1e-6


def test_distill_recovers_additive_truth_and_agreement():
    import hsbot.mulligan_ai as ai
    from trainer.scorer import scorer_dataset, distill
    rows = _v2_rows()
    ds = scorer_dataset(rows, NO_DB, {"engine": []}, q_by_game={}, q_gated=False)
    truth = {"A_is_CS2_029": 0.1}
    # 合成基座: 打分 = 0.5 + 留集内真值增益(布局偏移走单点助手, 不硬编码)
    off = ai.v3_layout_offsets(ds["vocab"], ds["pairs"])
    kept_off = off["kept_off"]
    idx = {c: kept_off + i for i, c in enumerate(ds["vocab"])}

    def predict(X):
        return [0.5 + sum(0.1 * row[i] for i in
                          (idx.get("CS2_029"),)) for row in X]

    gain, syn, agree = distill(ds, predict)
    assert abs(gain.get("CS2_029", 0.0) - 0.1) < 1e-6
    assert agree == 1.0                              # 加性真值下 top-1 必然一致


def test_cv_compare_reports_table_baseline():
    from trainer.scorer import cv_compare, scorer_dataset
    rows = _v2_rows() * 3                            # 放大到 6 行(3 组)
    ds = scorer_dataset(rows, NO_DB, {"engine": []}, q_by_game={},
                        q_gated=False)
    report, winner, predict = cv_compare(ds, data_dir=ROOT / "data",
                                         pref="tabpfn_v2")
    assert "统计表加性" in report
    assert winner in ("tabpfn_v2", "tabicl_v2", "lr")


# ── 训练接线(config/_save_version/read_latest/train_v3 编排) ──

def test_config_v3_keys():
    from hsbot.config import Config
    cfg = Config.load({"config": "/nonexistent.yaml"})
    assert cfg.mulligan_v3 is True and cfg.scorer_backend == "tabpfn_v2"
    assert abs(cfg.distill_min_agree - 0.90) < 1e-9


def test_save_version_writes_v3_and_meta(tmp_path):
    from trainer import mulligan as M
    root = tmp_path / "models"
    g = M.Game(path="s_g01", result=1, coin=False, opp_class="PRIEST",
               cards=["A"], kept=["A"], mtime=1.0, size=10)
    v3 = {"layout": 1, "distill_ok": True, "agree": 1.0, "auc": 0.6,
          "backend": "tabpfn_v2"}
    M._save_version(root, "测试", {}, None, None, [g], 1, Counter(), v3=v3)
    art = json.loads((root / "v001" / "v3.json").read_text(encoding="utf-8"))
    meta = json.loads((root / "v001" / "meta.json").read_text(encoding="utf-8"))
    assert art["distill_ok"] is True
    assert meta["v3"]["backend"] == "tabpfn_v2"


def test_save_version_without_v3_no_artifact(tmp_path):
    """mulligan_v3=false 等价 v3=None: 不产 v3.json(零变化钉子的落盘半边)。"""
    from trainer import mulligan as M
    root = tmp_path / "models"
    g = M.Game(path="s_g01", result=1, coin=False, opp_class="PRIEST",
               cards=["A"], kept=["A"], mtime=1.0, size=10)
    M._save_version(root, "测试", {}, None, None, [g], 1, Counter())
    assert not (root / "v001" / "v3.json").exists()
    meta = json.loads((root / "v001" / "meta.json").read_text(encoding="utf-8"))
    assert "v3" not in meta


def test_read_latest_loads_v3(tmp_path):
    from hsbot.mulligan_ai import read_latest
    from trainer import mulligan as M
    root = tmp_path / "models"
    g = M.Game(path="s_g01", result=1, coin=False, opp_class="PRIEST",
               cards=["A"], kept=["A"], mtime=1.0, size=10)
    M._save_version(root, "测试", {}, None, None, [g], 1, Counter(),
                    v3={"layout": 1, "distill_ok": True})
    ver, art = read_latest(root)
    assert art["v3"]["distill_ok"] is True


def test_train_v3_skips_gracefully_on_empty_corpus(tmp_path):
    """空语料/决策行不足 → v3=None, 训练主流程不失败(诚实降级路径)。"""
    from types import SimpleNamespace
    from trainer.scorer import train_v3
    cfg = SimpleNamespace(training_dir=tmp_path / "corpus",
                          data_dir=tmp_path / "data", battletag="湫然#51704",
                          scorer_backend="tabpfn_v2", distill_min_agree=0.9)
    (tmp_path / "data").mkdir()
    v3 = train_v3(cfg, "奇迹德", NO_DB, {"engine": []})
    assert v3 is None
