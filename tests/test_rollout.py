"""rollout 多回合推演 + 独立抽牌通道(v3.1 设计 §3)。

假卡表沿用 tests/test_planner.py 的 _db 模式(无网络)。
"""
import json

import pytest

from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB

from planner.pieces import Piece, build_piece
from planner.simstate import initial_state, play

CARDS = [
    {"id": "MOON", "name": "月火术", "type": "SPELL", "cost": 1,
     "text": "造成$1点伤害。"},
    {"id": "AUCTION", "name": "黑市拍卖师", "type": "MINION", "cost": 5,
     "text": "每当你施放一个法术后，抽一张牌。"},
    {"id": "DRAW2", "name": "奥术洞察", "type": "SPELL", "cost": 3,
     "text": "抽2张牌。"},
    {"id": "INNERVATE", "name": "激活", "type": "SPELL", "cost": 0,
     "text": "在本回合中，获得一个 法力水晶。"},
    {"id": "BIG", "name": "星火术", "type": "SPELL", "cost": 4,
     "text": "造成$6点伤害。"},
]


def _db(tmp_path, cards):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


@pytest.fixture
def analyzer(tmp_path):
    return EffectAnalyzer(_db(tmp_path, CARDS))


# ---------------- Task1: 独立抽牌通道 ----------------

def test_play_standalone_draw_pops_known_queue():
    """draw_n=2 的法术: 两张已知牌队头入手, drawn 不增。"""
    pieces = {("DRAW2", 3): Piece(card_id="DRAW2", cost=3, is_spell=True,
                                  draw_n=2)}
    st = initial_state(5, (("DRAW2", 3),), 0,
                       known_draws=(("MOON", 1), ("MOON", 1)))
    nxt = play(st, 0, pieces)
    assert sorted(nxt.hand) == [("MOON", 1), ("MOON", 1)]
    assert nxt.drawn == 0


def test_play_standalone_draw_unknown_counts_drawn():
    pieces = {("DRAW2", 3): Piece(card_id="DRAW2", cost=3, is_spell=True,
                                  draw_n=2)}
    st = initial_state(5, (("DRAW2", 3),), 0)
    nxt = play(st, 0, pieces)
    assert nxt.hand == ()
    assert nxt.drawn == 2


def test_build_piece_never_sets_draw_n(analyzer):
    """live 零变化铁律: build_piece 产出的 draw_n 恒 0(draw_n 只由
    trainer.sim 后填)。触发句(每当)与非触发抽牌都不得进 build_piece。"""
    for cid in ("MOON", "AUCTION", "DRAW2"):
        assert build_piece(cid, 1, analyzer).draw_n == 0


# ---------------- Task2: rollout 核心 ----------------

from planner.rollout import RolloutResult, rollout


def _sim_pieces():
    """手搓 pieces(不经编译器, 测试确定性): 月火1费1伤/星火4费6伤/
    拍卖师5费引擎/激活0费回费。"""
    return {
        ("MOON", 1): Piece("MOON", 1, segments=(1,), spell_scaled=True,
                           is_spell=True),
        ("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                          is_spell=True),
        ("AUCTION", 5): Piece("AUCTION", 5, engine=True),
        ("INNERVATE", 0): Piece("INNERVATE", 0, mana_gain=1, is_spell=True),
        ("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True),
    }


_COST = {"MOON": 1, "BIG": 4, "AUCTION": 5, "INNERVATE": 0, "COIN": 0}


def test_rollout_holds_damage_until_affordable_then_launches():
    """留星火: 策略不打伤害牌(无引擎), T4 费够 → best_line 启动。"""
    r = rollout(("MOON", "MOON", "BIG"), offered=("MOON", "BIG"),
                keep=("BIG",), coin=False, pieces=_sim_pieces(),
                cost_of=_COST, k_max=6,
                enemy_totals=(None, 6, 6, 6, 6, 6))
    assert r.launch_turn == 4


def test_rollout_no_launch_returns_none():
    r = rollout(("MOON",), offered=("MOON",), keep=("MOON",), coin=False,
                pieces=_sim_pieces(), cost_of=_COST, k_max=3,
                enemy_totals=(30, 30, 30))
    assert r.launch_turn is None
    assert r.turns == 3
    assert len(r.hand_sizes) == 3


def test_rollout_policy_deploys_engine_and_damage_becomes_fuel():
    """T5 上拍卖师; T6 月火作为燃料被打出并触发抽牌循环 → 启动。"""
    # 牌序须含 keep 卡: 拍卖师按首次出现移除后, 抽牌流 = 月火×7
    order = ("AUCTION",) + ("MOON",) * 7
    r = rollout(order, offered=("AUCTION", "MOON"), keep=("AUCTION",),
                coin=False, pieces=_sim_pieces(), cost_of=_COST, k_max=6,
                enemy_totals=(None, None, None, None, None, 6))
    assert r.launch_turn == 6
    assert r.engines == 1


def test_rollout_pure_function_same_input_same_output():
    kw = dict(offered=("MOON", "BIG"), keep=("BIG",), coin=False,
              pieces=_sim_pieces(), cost_of=_COST, k_max=5,
              enemy_totals=(None, 6, 6, 6, 6))
    assert rollout(("MOON", "MOON", "BIG"), **kw) == \
        rollout(("MOON", "MOON", "BIG"), **kw)


def test_rollout_keep_card_not_in_order_raises():
    with pytest.raises(ValueError):
        rollout(("MOON",), offered=("MOON", "BIG"), keep=("BIG",),
                coin=False, pieces=_sim_pieces(), cost_of=_COST, k_max=2,
                enemy_totals=(6, 6))
