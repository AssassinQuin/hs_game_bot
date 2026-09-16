"""SimSnapshot 统一快照层(spec §2/§3): 新字段/advance_turn/memo_key/play 透传。"""
import dataclasses

import pytest

from planner.pieces import Piece
from planner.simstate import (SimSnapshot, advance_turn, initial_state,
                              memo_key, play)


def _snap(**kw):
    base = dict(mana=5, hand=(("MOON", 1),), sp=0)
    base.update(kw)
    return initial_state(base.pop("mana"), base.pop("hand"), base.pop("sp"),
                         **base)


def test_snapshot_new_fields_default_and_hashable():
    s = initial_state(5, (("MOON", 1), ("BIG", 4)), 1)
    assert isinstance(s, SimSnapshot)
    assert (s.turn, s.enemy_total, s.board_atk, s.dealt_total) == (1, None, 0, 0)
    assert hash(s) == hash(initial_state(5, (("MOON", 1), ("BIG", 4)), 1))
    s2 = initial_state(5, (("MOON", 1),), 0, turn=3, enemy_total=26,
                       board_atk=4, dealt_total=7)
    assert (s2.turn, s2.enemy_total, s2.board_atk, s2.dealt_total) == (3, 26, 4, 7)


def test_advance_turn_accumulates_face_into_dealt_and_resets():
    # initial_state 无 face 参(恒 0 起) → face≠0 的快照用 dataclasses.replace 构造
    s = dataclasses.replace(
        _snap(hand=(("MOON", 1), ("BIG", 4)), turn=3, enemy_total=30,
              dealt_total=5),
        face=4)
    nxt = advance_turn(s, enemy_total=28)
    assert nxt.turn == 4 and nxt.mana == 4            # min(10, 新回合)
    assert nxt.dealt_total == 9 and nxt.face == 0     # 5+4 累计, 本回合清零
    assert nxt.enemy_total == 28                      # 逐回合数据替换(None 也合法)
    assert advance_turn(s, enemy_total=None).enemy_total is None


def test_advance_turn_draws_known_head_from_t2_and_sorts():
    s = _snap(hand=(("BIG", 4),), known_draws=(("A", 2), ("B", 1)), turn=1)
    nxt = advance_turn(s, enemy_total=30)             # 1→2: 自然抽 1
    assert nxt.hand == (("A", 2), ("BIG", 4))         # 队头入手且升序规范化
    assert nxt.known_draws == (("B", 1),)
    assert nxt.drawn == 0


def test_advance_turn_empty_known_counts_drawn():
    s = _snap(turn=4)
    nxt = advance_turn(s, enemy_total=30)
    assert nxt.hand == s.hand and nxt.drawn == 1


def test_advance_turn_clears_disc_next_keeps_persistent_fields():
    s = _snap(disc_next=3, disc_next_cat="星灵", disc_hand=2, sp=1, engines=2,
              known_draws=(("A", 2),))
    nxt = advance_turn(s, enemy_total=None)
    assert (nxt.disc_next, nxt.disc_next_cat) == (0, "")   # 一次性余额随回合过期
    assert (nxt.disc_hand, nxt.sp, nxt.engines) == (2, 1, 2)


def test_memo_key_is_transition_projection():
    s = _snap(hand=(("BIG", 4), ("MOON", 1)), disc_next=3, disc_next_cat="法术",
              known_draws=(("A", 2),))
    assert memo_key(s) == (5, (("BIG", 4), ("MOON", 1)), 0, 0, 3, "法术", 0,
                          (("A", 2),))


def test_memo_key_equal_across_bookkeeping_fields():
    # 判定/累计量不进键(后缀不变性): face/drawn/turn/enemy_total/board_atk/
    # dealt_total(控制器裁决: 用此直白版本替换 brief 末尾的恒真 for 循环)
    base = _snap(hand=(("MOON", 1),), known_draws=(("A", 2),))
    assert memo_key(base) == memo_key(dataclasses.replace(
        base, face=9, drawn=3, turn=7, enemy_total=26,
        board_atk=3, dealt_total=11))


def test_play_passes_new_fields_through():
    pieces = {("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True)}
    s = _snap(hand=(("COIN", 0), ("MOON", 1)), turn=2, enemy_total=26,
              board_atk=4, dealt_total=5)
    nxt = play(s, 0, pieces)
    assert (nxt.turn, nxt.enemy_total, nxt.board_atk, nxt.dealt_total) == (2, 26, 4, 5)
    assert nxt.mana == 6                             # 5-0+1
