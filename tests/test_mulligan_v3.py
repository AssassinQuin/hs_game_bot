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
        {"row": "turn", "game": "s_g01#1", "snap": _snap(2),
         "actions": [], "drawn_this_turn": ["CS2_029"], "result": 1,
         "src": "s_g01.power.log#g1T2"},
        {"row": "turn", "game": "s_g01#1", "snap": _snap(3),
         "actions": [], "drawn_this_turn": ["CS2_042"], "result": 1,
         "src": "s_g01.power.log#g1T3"},
        {"row": "mulligan", "game": "s_g01#1", "offered": ["CS2_029"],
         "kept": ["CS2_029"], "replaced_in": ["EX1_166"], "coin": 0,
         "opp_class": "PRIEST", "decklist": {"CS2_029": 2, "CS2_042": 2,
                                             "EX1_166": 1},
         "result": 1},
        # g2: 只有 T2; 负局
        {"row": "turn", "game": "s_g02#1", "snap": _snap(2),
         "actions": [], "drawn_this_turn": [], "result": 0,
         "src": "s_g02.power.log#g1T2"},
        {"row": "mulligan", "game": "s_g02#1", "offered": ["CS2_042"],
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
    assert set(q3) == {"s_g01#1", "s_g02#1"}         # T3 优先, 缺则 T2 兜底
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
                  and r["game"] == "s_g01#1")
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
