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
