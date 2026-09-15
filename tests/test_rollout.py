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
    {"id": "OPP_DRAW2", "name": "自然平衡", "type": "SPELL", "cost": 2,
     "text": "消灭一个随从。你的对手抽两张牌。"},
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


# ---------------- Task3: sim pieces 构建 + 完备性硬门 ----------------

from trainer.sim import COIN_CID, build_sim_pieces, pieces_completeness


def test_build_sim_pieces_injects_coin_and_draws(analyzer, tmp_path):
    db = _db(tmp_path, CARDS)
    decklist = {"MOON": 2, "AUCTION": 1, "DRAW2": 2}
    pieces, cost_of = build_sim_pieces(decklist, db, analyzer)
    assert cost_of["MOON"] == 1 and cost_of["AUCTION"] == 5
    coin = pieces[(COIN_CID, 0)]
    assert coin.mana_gain == 1 and coin.is_spell and cost_of[COIN_CID] == 0
    # 独立抽牌后填: 奥术洞察 Draw(2) → draw_n=2; 拍卖师(每当)守卫 → 0
    assert pieces[("DRAW2", 3)].draw_n == 2
    assert pieces[("AUCTION", 5)].draw_n == 0


def test_build_sim_pieces_excludes_opponent_draws(analyzer, tmp_path):
    """scope=opponent 的抽牌(自然平衡族)不计我方 draw_n(审查 Important 修复)。"""
    db = _db(tmp_path, CARDS)
    pieces, _cost = build_sim_pieces({"OPP_DRAW2": 2}, db, analyzer)
    assert pieces[("OPP_DRAW2", 2)].draw_n == 0


def test_pieces_completeness_gates_missing_card_and_key(analyzer, tmp_path):
    db = _db(tmp_path, CARDS)
    decklist = {"MOON": 2, "GHOST": 1}        # GHOST 不在卡表
    pieces, cost_of = build_sim_pieces(decklist, db, analyzer)
    assert any("GHOST" in s for s in
               pieces_completeness(decklist, db, pieces, cost_of))
    del pieces[("MOON", 1)]                   # 人为抽走键
    assert any("MOON" in s for s in
               pieces_completeness(decklist, db, pieces, cost_of))
    ok_deck = {"MOON": 2}
    p2, c2 = build_sim_pieces(ok_deck, db, analyzer)
    assert pieces_completeness(ok_deck, db, p2, c2) == []


# ---------------- Task4: CRN 枚举 + 组合维度输出 ----------------

import time as _time

from trainer.sim import ANTI_SYNERGY_THRESHOLD, combo_outputs, sim_mulligan


def test_combo_outputs_hand_computed():
    """纯函数: 手算值精确钉死(spec §5.3 公式)。"""
    scores = {frozenset(): 0.40, frozenset(("A",)): 0.50,
              frozenset(("B",)): 0.38, frozenset(("A", "B")): 0.44}
    out = combo_outputs(scores, ("A", "B"))
    assert out["keep"] == ("A",)
    assert out["per_card_marginal"] == {"A": pytest.approx(0.10)}
    assert out["pair_synergy"][("A", "B")] == pytest.approx(-0.04)
    assert out["anti_synergy"] == [("A", "B")]     # -0.04 < -0.02
    assert out["reject"] == ["B"]                  # 0.38-0.40 = -0.02 < 0


def test_sim_mulligan_deterministic_and_ordered():
    deck = {"MOON": 2, "BIG": 1}
    kw = dict(pieces=_sim_pieces(), cost_of=_COST, coin=False, orders=8,
              k_max=5, enemy_totals=(None, 6, 6, 6, 6),
              survive=[1.0] * 5, seed=0)
    r1 = sim_mulligan(deck, ("MOON", "BIG"), **kw)
    r2 = sim_mulligan(deck, ("MOON", "BIG"), **kw)
    assert r1["scores"] == r2["scores"]            # 同种子可复现
    assert len(r1["scores"]) == 4                  # 2^2 个 keep 集
    assert r1["scores"][frozenset(("BIG",))] >= \
        r1["scores"][frozenset()]                  # 留星火不劣于全换


def test_sim_mulligan_launch_hist_bounded():
    deck = {"MOON": 2, "BIG": 1}
    r = sim_mulligan(deck, ("MOON", "BIG"), pieces=_sim_pieces(),
                     cost_of=_COST, orders=5, k_max=4,
                     enemy_totals=(None, 6, 6, 6), survive=[1.0] * 4, seed=1)
    for s, hist in r["launch_hist"].items():
        assert 0 <= sum(hist) <= 5
        assert hist[0] == 0                        # [0] 弃用位


def test_sim_mulligan_perf_small_deck():
    deck = {"MOON": 12, "AUCTION": 2, "DRAW2": 6}
    pieces, cost_of = None, None                   # 手搓 pieces, 免编译器
    pieces = {("MOON", 1): Piece("MOON", 1, segments=(1,), spell_scaled=True,
                                 is_spell=True),
              ("AUCTION", 5): Piece("AUCTION", 5, engine=True),
              ("DRAW2", 3): Piece("DRAW2", 3, is_spell=True, draw_n=2),
              ("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True)}
    cost_of = {"MOON": 1, "AUCTION": 5, "DRAW2": 3, "COIN": 0}
    t0 = _time.time()
    r = sim_mulligan(deck, ("MOON", "AUCTION", "DRAW2"), pieces=pieces,
                     cost_of=cost_of, orders=5, k_max=6,
                     enemy_totals=(30,) * 6, survive=[1.0] * 6, seed=0)
    assert _time.time() - t0 < 5.0, "5 牌序×8 集合×6 回合须 5s 内(性能钉子)"
    assert set(r) >= {"scores", "keep", "per_card_marginal", "pair_synergy",
                      "anti_synergy", "reject", "launch_hist"}
