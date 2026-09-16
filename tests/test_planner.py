"""planner 包(T1 切片2+3): Piece 编译 / SimState 转移 / DFS 最优线。

行为口径全部以 .superpowers/sdd/t1/interface-contract.md 为准:
- build_piece: IR → Piece(诚实降级, 缺牌=inert);
- play: 纯函数转移, 法强逐段 (base+sp)×hits, 不可支付 raise ValueError;
- best_line: 记忆化+上界剪枝的 DFS, 最优 face_det 与暴力全排列一致;
- lethal 只由 face_det + board_atk 判定, 期望分量绝不计入。
"""
import json
import random
import time
from dataclasses import replace

import pytest

from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB

from planner.dfs import best_line
from planner.pieces import Piece, build_piece
from planner.plan import Plan
from planner.simstate import initial_state, play


# ---------------- 假卡表(无网络, 参照 tests/test_analysis.py 的 _db 模式) ----------------

CARDS = [
    {"id": "MOON", "name": "月火术", "type": "SPELL", "cost": 1,
     "text": "造成$1点伤害。"},
    {"id": "TWO_HIT", "name": "月光射线", "type": "SPELL", "cost": 2,
     "text": "对一个敌人造成$1点伤害两次。"},
    {"id": "INNERVATE", "name": "激活", "type": "SPELL", "cost": 0,
     "text": "在本回合中，获得一个 法力水晶。"},
    {"id": "GADGET", "name": "虚灵改装师", "type": "MINION", "cost": 3,
     "text": "<b>战吼：</b>对一个随从造成1点伤害，并使其获得<b>法术伤害+1</b>。"},
    {"id": "AUCTION", "name": "黑市拍卖师", "type": "MINION", "cost": 5,
     "text": "每当你施放一个法术后，抽一张牌。"},
    {"id": "HAND_DISC", "name": "生命缚誓者的礼物", "type": "SPELL", "cost": 2,
     "text": "使你手牌中法术牌的法力值消耗减少（1）点。"},
    {"id": "NEXT_DISC", "name": "建造水晶塔", "type": "SPELL", "cost": 0,
     "set": "SPACE",
     "text": "在本回合中，你的下一张星灵牌法力值消耗减少（2）点。"},
    {"id": "PROTOSS", "name": "星灵守卫", "type": "MINION", "cost": 2,
     "set": "SPACE",
     "text": "<b>嘲讽</b>", "mechanics": ["TAUNT"]},
    {"id": "CHOOSE_SP", "name": "顺水漂流", "type": "SPELL", "cost": 1,
     "text": "选择一个随从。使其获得法术伤害+1。"},
    {"id": "VANILLA", "name": "白板嘲讽", "type": "MINION", "cost": 2,
     "text": "<b>嘲讽</b>", "mechanics": ["TAUNT"]},
    {"id": "MOON_B", "name": "月火二号", "type": "SPELL", "cost": 1,
     "text": "造成$2点伤害。"},
    {"id": "BIG_SPELL", "name": "大伤害法术", "type": "SPELL", "cost": 4,
     "text": "对一个敌人造成$3点伤害两次。"},
    {"id": "SP3", "name": "三费箭", "type": "SPELL", "cost": 3,
     "text": "造成$1点伤害。"},
    {"id": "AOE_MIN", "name": "扫场", "type": "SPELL", "cost": 4,
     "text": "对所有敌方随从造成$3点伤害。"},
    {"id": "RAND_SPLIT", "name": "随机火", "type": "SPELL", "cost": 2,
     "text": "造成$4点伤害，随机分配给所有敌人。"},
]

# (card_id, 实际费) 池: 供随机交叉验证使用
POOL = [("MOON", 1), ("TWO_HIT", 2), ("INNERVATE", 0), ("GADGET", 3),
        ("AUCTION", 5), ("HAND_DISC", 2), ("NEXT_DISC", 0), ("VANILLA", 2),
        ("PROTOSS", 2)]


def _db(tmp_path, cards):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


@pytest.fixture
def analyzer(tmp_path):
    return EffectAnalyzer(_db(tmp_path, CARDS))


def _mk_pieces(analyzer, entries):
    """(cid, cost) 表 → pieces dict。"""
    return {(cid, cost): build_piece(cid, cost, analyzer)
            for cid, cost in entries}


# ---------------- build_piece: 编译层 ----------------

def test_build_piece_gadgetzan_fixed_damage_not_in_segments(analyzer):
    """虚灵改装师: 法强增益 +1 入 piece; 裸数字固定伤(scaled=False)不进 segments
    —— 斩杀口径不认目标受限的固定伤(宁漏勿错)。"""
    p = build_piece("GADGET", 3, analyzer)
    assert p.card_id == "GADGET" and p.cost == 3
    assert p.spellpower_gain == 1
    assert p.segments == ()
    assert not p.spell_scaled and not p.is_spell


def test_build_piece_moonfire_segments(analyzer):
    """月火术: 首个 $N 缩放伤害 → segments=(base,)*hits; 法术吃法强。"""
    p = build_piece("MOON", 1, analyzer)
    assert p.segments == (1,)
    assert p.spell_scaled is True and p.is_spell is True
    assert build_piece("TWO_HIT", 2, analyzer).segments == (1, 1)   # 两次=两段


def test_build_piece_mana_gain(analyzer):
    """激活: 回费合计进 mana_gain。"""
    p = build_piece("INNERVATE", 0, analyzer)
    assert p.mana_gain == 1
    assert p.is_spell and p.segments == ()


def test_build_piece_cost_down_scopes(analyzer):
    """减费按 scope 分流: hand: → discount_hand, next: → discount_next。"""
    assert build_piece("HAND_DISC", 2, analyzer).discount_hand == 1
    assert build_piece("HAND_DISC", 2, analyzer).discount_next == 0
    assert build_piece("NEXT_DISC", 0, analyzer).discount_next == 2
    assert build_piece("NEXT_DISC", 0, analyzer).discount_hand == 0


def test_build_piece_engine_cast_draw(analyzer):
    """拍卖师: Mechanic(cast_draw) → engine=True; 触发句误标的 Draw(1) 不产生
    任何抽牌位(抽牌只走 engine+known_draws)。"""
    p = build_piece("AUCTION", 5, analyzer)
    assert p.engine is True
    assert not p.is_spell and p.segments == ()


def test_build_piece_choose_one_spellpower_forced_zero(analyzer):
    """文本含"选择一" → 法强增益强制 0(抉择分支不确定, 宁漏勿错)。"""
    assert build_piece("CHOOSE_SP", 1, analyzer).spellpower_gain == 0


def test_build_piece_missing_card_inert(analyzer):
    """卡表缺牌 → inert Piece(全默认, 诚实降级不崩溃)。"""
    p = build_piece("NOT_IN_DB", 4, analyzer)
    assert p == Piece("NOT_IN_DB", 4)
    assert p.segments == () and not p.spell_scaled and not p.is_spell
    assert p.mana_gain == 0 and p.spellpower_gain == 0
    assert p.discount_hand == 0 and p.discount_next == 0
    assert p.engine is False


# ---------------- play: 状态转移 ----------------

def test_initial_state_normalizes_hand():
    """initial_state: 手牌升序排序规范化。"""
    st = initial_state(5, (("B", 2), ("A", 1)), 0)
    assert st.hand == (("A", 1), ("B", 2))
    assert st.face == 0 and st.drawn == 0 and st.known_draws == ()
    assert st.sp == 0 and st.disc_hand == 0 and st.disc_next == 0


def test_play_unpayable_raises_value_error(analyzer):
    """不可支付 → ValueError(契约语义)。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    st = initial_state(0, (("MOON", 1),), 0)
    with pytest.raises(ValueError):
        play(st, 0, pieces)


def test_play_scales_each_segment_with_spellpower(analyzer):
    """法强逐段结算: (base+sp)×hits。月光射线 sp=3 → (1+3)×2=8。"""
    pieces = _mk_pieces(analyzer, [("TWO_HIT", 2)])
    st = initial_state(5, (("TWO_HIT", 2),), 3)
    st2 = play(st, 0, pieces)
    assert st2.face == 8
    assert st2.mana == 3 and st2.hand == ()


def test_play_mana_cap_ten(analyzer):
    """回费上限 10。"""
    pieces = _mk_pieces(analyzer, [("INNERVATE", 0)])
    st = initial_state(10, (("INNERVATE", 0),), 0)
    assert play(st, 0, pieces).mana == 10
    st2 = initial_state(9, (("INNERVATE", 0), ("INNERVATE", 0)), 0)
    assert play(st2, 0, pieces).mana == 10


def test_play_disc_next_consumed_once(analyzer):
    """next: 减费 = 面值一次性(2026-09-14 审计修正): 下一张全额减, 用后清零。
    (initial_state 直给的不带类目余额视为不限类目: 任意有剩余费的牌都
    作用并消耗 —— 带类目余额的作用/保留语义见下方三枚新钉子。)"""
    pieces = _mk_pieces(analyzer, [("VANILLA", 2)])
    st = initial_state(5, (("VANILLA", 2),), 0, disc_next=2)
    st2 = play(st, 0, pieces)           # 下一张 2 费随从: 全额 -2 → 0 费
    assert st2.mana == 5 and st2.disc_next == 0


def test_build_piece_next_disc_category_and_card_cats(analyzer):
    """2026-09-14 实测(g16 T5 最优线把水晶塔-2送给礼物): Piece 记录
    next: 减费的类目词与自身类目 —— 水晶塔 next:星灵 → next_cat=星灵,
    自身是星灵(set=SPACE); 普通法术 card_cats=法术 且 next_cat 空。"""
    tower = build_piece("NEXT_DISC", 0, analyzer)
    assert tower.discount_next == 2 and tower.next_cat == "星灵"
    assert "星灵" in tower.card_cats and "法术" in tower.card_cats
    moon = build_piece("MOON", 1, analyzer)
    assert moon.next_cat == "" and moon.card_cats == frozenset({"法术"})
    minion = build_piece("VANILLA", 2, analyzer)
    assert "随从" in minion.card_cats and "星灵" not in minion.card_cats


def test_play_disc_next_category_mismatch_not_consumed(analyzer):
    """类目不符: 下一张星灵牌-2 不得作用也不得消耗在非星灵牌上
    (2026-09-14 实测: 旧语义白送给礼物/月光射线 → 线内虚减费, 误报可斩)。"""
    pieces = _mk_pieces(analyzer, [("NEXT_DISC", 0), ("HAND_DISC", 2)])
    st = initial_state(5, (("NEXT_DISC", 0), ("HAND_DISC", 2)), 0)
    st2 = play(st, 1, pieces)                    # 打出水晶塔: 余额 2(星灵)
    assert st2.disc_next == 2 and st2.disc_next_cat == "星灵"
    st3 = play(st2, 0, pieces)                   # 礼物(2费, 非星灵): 原价 2
    assert st3.mana == 3                         # 5-2: 一分不减
    assert st3.disc_next == 2 and st3.disc_next_cat == "星灵"   # 余额保留


def test_play_disc_next_zero_cost_not_consumed(analyzer):
    """0 费的牌不需要/不消耗减费(用户裁决 2026-09-14): 原费已 0 的牌既不
    被作用也不吃掉一次性余额(旧语义 0 费牌白吃面值 → 线内少一张可用减费)。"""
    pieces = _mk_pieces(analyzer, [("NEXT_DISC", 0), ("INNERVATE", 0)])
    st = initial_state(5, (("NEXT_DISC", 0), ("INNERVATE", 0)), 0)
    st2 = play(st, 0, pieces)                    # 打出水晶塔: 余额 2
    st3 = play(st2, 0, pieces)                   # 激活(0费): 不消耗
    assert st3.disc_next == 2 and st3.disc_next_cat == "星灵"
    assert st3.mana == 6                         # 5-0费+激活回费1


def test_play_disc_next_applies_to_starcraft_card(analyzer):
    """类目相符: 水晶塔-2 作用于星灵随从(2费→0费)后清零。"""
    pieces = _mk_pieces(analyzer, [("NEXT_DISC", 0), ("PROTOSS", 2)])
    st = initial_state(5, (("NEXT_DISC", 0), ("PROTOSS", 2)), 0)
    st2 = play(st, 0, pieces)
    st3 = play(st2, 0, pieces)
    assert st3.mana == 5 and st3.disc_next == 0


def test_best_line_no_fake_discount_line_for_gift(analyzer):
    """g16 T5 回归钉(标定版): mana=2, 手牌=水晶塔(0)+礼物(2)+3费法术($1)。
    旧语义: 塔→礼物(白吃-2 按0费打出, 还给手牌法术-1)→3费法术(3-1=2费)
    → face 1 —— 虚减费多打了一张; 新语义礼物按原价 2, 线打不出伤害
    → face_det=0(宁漏勿错: 绝不为虚减费多算伤害)。"""
    pieces = _mk_pieces(analyzer, [("NEXT_DISC", 0), ("HAND_DISC", 2),
                                   ("SP3", 3)])
    st = initial_state(2, (("NEXT_DISC", 0), ("HAND_DISC", 2), ("SP3", 3)), 0)
    plan = best_line(st, pieces)
    assert plan.face_det == 0


def test_play_disc_hand_discounts_every_spell(analyzer):
    """hand: 减费只作用于法术且不耗尽; 打出的减费牌追加余额。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1), ("HAND_DISC", 2)])
    st = initial_state(3, (("HAND_DISC", 2), ("MOON", 1)), 0, disc_hand=1)
    st2 = play(st, 1, pieces)           # 月火(法术): 1-1=0 费
    assert st2.mana == 3 and st2.disc_hand == 1
    st3 = play(st2, 0, pieces)          # 礼物(法术): 2-1=1 费, 又 +1 余额
    assert st3.mana == 2 and st3.disc_hand == 2


def test_play_engine_draws_known_head_and_normalizes_hand(analyzer):
    """引擎语义: 打出的牌不是引擎本尊不触发; 引擎在身(打牌前 engines>0)时
    施放法术 → known_draws 队头入手并弹出, 手牌重新排序规范化。"""
    pieces = _mk_pieces(analyzer, [("AUCTION", 5), ("MOON", 1), ("TWO_HIT", 2)])
    st = initial_state(10, (("AUCTION", 5), ("MOON", 1)), 0,
                       known_draws=(("TWO_HIT", 2),))
    st2 = play(st, 0, pieces)           # 打出拍卖师(随从): 不触发抽牌
    assert st2.engines == 1
    assert st2.drawn == 0 and st2.known_draws == (("TWO_HIT", 2),)
    st3 = play(st2, 0, pieces)          # 施放月火: 队头入手
    assert st3.known_draws == ()
    assert st3.hand == (("TWO_HIT", 2),)
    assert st3.drawn == 0


def test_play_engine_unknown_draw_increments_drawn(analyzer):
    """known_draws 为空时: 未知抽牌 drawn+1。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    st = initial_state(5, (("MOON", 1),), 0, engines=1)
    st2 = play(st, 0, pieces)
    assert st2.drawn == 1 and st2.hand == ()


def test_play_spell_without_engine_no_draw(analyzer):
    """engines=0: 施放法术不抽牌。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    st = initial_state(5, (("MOON", 1),), 0)
    st2 = play(st, 0, pieces)
    assert st2.drawn == 0 and st2.engines == 0


def test_play_two_engines_draw_two_known(analyzer):
    """双拍卖师(控制者定版语义): 施放一个法术, 每个在场引擎各自触发一次
    → known_draws 两张都入手(排序规范化), 不计 drawn。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    st = initial_state(5, (("MOON", 1),), 0,
                       known_draws=(("VANILLA", 2), ("TWO_HIT", 2)), engines=2)
    st2 = play(st, 0, pieces)
    assert st2.known_draws == ()
    assert st2.hand == (("TWO_HIT", 2), ("VANILLA", 2))
    assert st2.drawn == 0


def test_play_two_engines_unknown_draws_increment_twice(analyzer):
    """双拍卖师无已知待抽: 一次施法 drawn+2。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    st = initial_state(5, (("MOON", 1),), 0, engines=2)
    st2 = play(st, 0, pieces)
    assert st2.drawn == 2 and st2.hand == ()


def test_play_missing_piece_key_is_inert():
    """pieces 缺键 → inert Piece(仅费), 不崩溃不产伤。"""
    st = initial_state(5, (("UNK", 2),), 0)
    st2 = play(st, 0, {})
    assert st2.mana == 3 and st2.face == 0 and st2.hand == ()


# ---------------- best_line: DFS 正确性/口径/性能 ----------------

def _brute_best(state, pieces):
    """暴力枚举全部出牌序列(所有长度、所有顺序, 含引擎抽牌入手后继续打出),
    返回可达的最大 face_det。递归穷举 = 无记忆化无剪枝的参照实现。"""
    best = state.face                      # 空线(就此停手)
    for i in range(len(state.hand)):
        try:
            nxt = play(state, i, pieces)
        except ValueError:                 # 不可支付: 该分支作废
            continue
        best = max(best, _brute_best(nxt, pieces))
    return best


def test_best_line_matches_bruteforce_on_random_states(tmp_path):
    """随机小状态(手牌≤4、费≤5, 固定种子)×暴力全排列交叉验证 ≥20 组:
    剪枝永不砍最优 —— DFS 最优 face_det == 暴力最优。"""
    rng = random.Random(20260913)
    analyzer = EffectAnalyzer(_db(tmp_path, CARDS))
    pieces = _mk_pieces(analyzer, POOL)
    cases = 25
    for case in range(cases):
        hand = tuple(rng.choice(POOL) for _ in range(rng.randint(1, 4)))
        state = initial_state(
            mana=rng.randint(0, 5), hand_cards=hand, sp=rng.randint(0, 2),
            known_draws=(rng.choice(POOL),) if rng.random() < 0.4 else (),
            disc_hand=rng.choice([0, 0, 1]),
            disc_next=rng.choice([0, 0, 1, 2]),
            engines=rng.choice([0, 0, 1]))
        plan = best_line(replace(state, board_atk=rng.randint(0, 3),
                                 enemy_total=None),
                         pieces, exp_per_draw=1.5)
        assert plan.face_det == _brute_best(state, pieces), \
            f"case {case}: 手牌={hand} state={state}"


def test_best_line_empty_when_nothing_payable(analyzer):
    """无可支付动作 → 叶: 空线, total=face+场攻, lethal 按确定伤判定。"""
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    st = initial_state(0, (("MOON", 1),), 0)
    plan = best_line(replace(st, board_atk=3, enemy_total=3), pieces)
    assert plan.actions == () and plan.face_det == 0
    assert plan.total == 3 and plan.lethal is True
    assert plan.mana_trace == () and plan.face_exp == 0
    assert best_line(replace(st, board_atk=3, enemy_total=4), pieces).lethal is False


def test_lethal_only_by_face_det_and_board_atk(analyzer):
    """致命口径: lethal 只由 face_det+board_atk 判定 —— 期望分量(face_exp)
    再高也不进 lethal, 也不进 total。"""
    analyzer = analyzer
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    # 3 张月火, mana 3, 引擎在身 → 3 确定伤 + 3 张未知抽牌
    st = initial_state(3, (("MOON", 1), ("MOON", 1), ("MOON", 1)), 0, engines=1)
    plan = best_line(replace(st, board_atk=0, enemy_total=4), pieces,
                     exp_per_draw=10.0)
    assert plan.face_det == 3 and plan.face_exp == 30   # 3×10 取整, 仅注记
    assert plan.total == 3                              # 期望不进 total
    assert plan.lethal is False                         # 3 < 4: 期望再高也不斩
    plan2 = best_line(replace(st, board_atk=1, enemy_total=4), pieces)
    assert plan2.lethal is True                         # 3+1 ≥ 4: 确定伤判定


def test_plan_mana_trace_and_totals(analyzer):
    """Plan 注记: mana_trace=每步打出后的剩余 mana; total=face_det+board_atk;
    回费牌只有在"打出才能多打一张伤害"时才进最优线(平手取短)。"""
    pieces = _mk_pieces(analyzer, [("INNERVATE", 0), ("MOON", 1)])
    st = initial_state(1, (("INNERVATE", 0), ("MOON", 1), ("MOON", 1)), 0)
    plan = best_line(replace(st, board_atk=2), pieces)
    assert plan.face_det == 2 and plan.total == 4
    assert len(plan.actions) == 3           # 激活回费才能打满两张月火
    assert plan.mana_trace == (2, 1, 0)     # 激活→回费 2, 月火→1, 月火→0
    st2 = initial_state(3, (("INNERVATE", 0), ("MOON", 1)), 0)
    assert len(best_line(st2, pieces).actions) == 1   # 费够时激活是废线: 平手取短


def test_uncovered_n_counts_distinct_no_damage_spells_played(analyzer):
    """uncovered_n: 线中打出过的 is_spell 且 segments==() 的张数(按 action
    序列去重); 未打出的不计, 有伤害段的法术不计, 非法术不计。"""
    pieces = _mk_pieces(analyzer, [("INNERVATE", 0), ("MOON", 1)])
    # 费只有 1: 必须两张激活都打出才能打满三张月火(最优 face=3, 5 步)
    st = initial_state(1, (("INNERVATE", 0), ("INNERVATE", 0),
                           ("MOON", 1), ("MOON", 1), ("MOON", 1)), 0)
    plan = best_line(st, pieces)
    assert plan.face_det == 3 and len(plan.actions) == 5
    assert plan.uncovered_n == 1        # 两张激活都打出但去重计 1; 月火有段不计
    # 非法术(虚灵改装师, 有法强增益无伤害段)打出也不计
    pieces2 = _mk_pieces(analyzer, [("GADGET", 3), ("MOON", 1)])
    st2 = initial_state(4, (("GADGET", 3), ("MOON", 1)), 0)
    plan2 = best_line(st2, pieces2)     # 改装师+1 法强让月火变 2 伤: 值得打
    assert plan2.face_det == 2 and len(plan2.actions) == 2
    assert plan2.uncovered_n == 0
    # 未打出的不计(无伤害增益的牌不进最优线)
    st3 = initial_state(5, (("INNERVATE", 0),), 0)
    assert best_line(st3, pieces).uncovered_n == 0


def test_best_line_worst_case_under_100ms(tmp_path):
    """性能: 10 张同费同牌 + mana=10 的最坏构造, best_line <100ms。"""
    analyzer = EffectAnalyzer(_db(tmp_path, CARDS))
    pieces = _mk_pieces(analyzer, [("MOON", 1)])
    state = initial_state(10, tuple(("MOON", 1) for _ in range(10)), 2)
    t0 = time.perf_counter()
    plan = best_line(state, pieces)
    dt = time.perf_counter() - t0
    assert dt < 0.1, f"best_line 耗时 {dt * 1000:.1f}ms 超出 100ms"
    assert plan.face_det == 10 * (1 + 2)     # 10 张月火全打: (1+2)×10
    assert len(plan.actions) == 10
    assert plan.mana_trace == (9, 8, 7, 6, 5, 4, 3, 2, 1, 0)


def test_best_line_ten_distinct_cards_worst_case_under_100ms(tmp_path):
    """性能·真实最坏面(控制者补定): 10 张异牌 + mana=10 + 减费/回费混合
    (含在手的引擎牌与法强牌), best_line <100ms。"""
    analyzer = EffectAnalyzer(_db(tmp_path, CARDS))
    hand = (("MOON", 1), ("MOON_B", 1), ("TWO_HIT", 2), ("BIG_SPELL", 4),
            ("INNERVATE", 0), ("HAND_DISC", 2), ("NEXT_DISC", 0),
            ("GADGET", 3), ("AUCTION", 5), ("VANILLA", 2))
    pieces = _mk_pieces(analyzer, hand)
    state = initial_state(10, hand, 1, disc_hand=1, disc_next=1)
    t0 = time.perf_counter()
    plan = best_line(state, pieces)
    dt = time.perf_counter() - t0
    assert dt < 0.1, f"best_line 耗时 {dt * 1000:.1f}ms 超出 100ms"
    # 下限抽查: 混合线至少要打出多数伤害牌(精确最优由交叉验证测试背书)
    assert plan.face_det >= 12 and len(plan.actions) >= 4


def test_plan_defaults():
    """Plan: board_atk/mana_trace 有默认值, frozen。"""
    p = Plan(actions=(), total=0, face_det=0, face_exp=0, lethal=False,
             enemy_total=None, uncovered_n=0)
    assert p.board_atk == 0 and p.mana_trace == ()
    with pytest.raises(Exception):          # frozen: 不可变
        p.total = 1


# ---------------- 审计 2026-09-14 语义修正波 ----------------

def test_disc_next_is_single_use_face_value():
    """审计 中#3: next: 减费 = 一次性面值(伺机待发"下一法术-3"), 全额作用
    下一张、用后清零; 不是"-1×N 张"。旧按张数结算会让线内多打一张法术
    (实测 6 伤局算成可斩, 真实上限 4)。"""
    prep = Piece(card_id="A_PREP", cost=0, discount_next=3, is_spell=True)
    d2 = Piece(card_id="D", cost=2, segments=(2,), spell_scaled=False, is_spell=True)
    pieces = {("A_PREP", 0): prep, ("D", 2): d2}
    st = initial_state(3, (("A_PREP", 0), ("D", 2), ("D", 2), ("D", 2)), 0)
    st2 = play(st, 0, pieces)                    # 打出 -3 减费: 余额 3
    assert st2.disc_next == 3
    st3 = play(st2, 0, pieces)                   # 下一张全额 -3(下限 0)
    assert st3.mana == 3 and st3.face == 2 and st3.disc_next == 0
    st4 = play(st3, 0, pieces)                   # 第二张恢复全价 2 费
    assert st4.mana == 1 and st4.face == 4
    plan = best_line(replace(st, enemy_total=6), pieces)  # 整线只打得出 2 张 → 4 伤
    assert plan.face_det == 4 and plan.lethal is False


def test_piece_excludes_minion_only_aoe(analyzer):
    """审计 中#5: "对所有敌方随从"是随从-only AoE, 打不了脸 —— 不进斩杀
    segments(宁漏勿错; 旧把裸"造成N点伤害"规则排 AoE 前, 截胡成 single)。"""
    p = build_piece("AOE_MIN", 4, analyzer)
    assert p.segments == ()


def test_piece_random_split_not_spell_scaled(analyzer):
    """审计 低#8: random_split 按不放大法强处理(EFFECTS_DESIGN §8 定版):
    段进 segments(沿粗估口径全计打脸)但 spell_scaled=False。"""
    p = build_piece("RAND_SPLIT", 2, analyzer)
    assert p.segments == (4,) and p.spell_scaled is False


def test_uncovered_counts_carddb_missing_cards():
    """审计 低#9: 卡表缺牌 = 效果未知(即便 inert 打出) —— best_line 对线内
    缺键张照计 uncovered(防御; 缺牌通常进不了线, 事实层的缺牌计数在
    analysis.lethal_plan 的 facts 口径, 见 test_lethal_plan)。"""
    d1 = Piece(card_id="VAL", cost=1, segments=(1,), is_spell=True)
    pieces = {("VAL", 1): d1}
    plan = best_line(initial_state(2, (("GHOST_A", 1), ("VAL", 1)), 0,
                                   enemy_total=None),
                     pieces)
    assert plan.face_det == 1
    assert plan.uncovered_n == 0        # 缺牌无增益进不了线, 线内无未覆盖张


# ---------------- 统一斩杀算式(SimSnapshot §3) ----------------

def test_best_line_unified_lethal_absorbs_face_and_dealt():
    """face_det+board_atk ≥ enemy_total−dealt_total−face: 快照已造成的
    本回合 face 与跨回合 dealt_total 都是已扣减量 —— rollout 旧
    replace(face=0) 技巧被吸收。"""
    pieces = {("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                                is_spell=True)}
    st = replace(initial_state(4, (("BIG", 4),), 0, enemy_total=10,
                               board_atk=0, dealt_total=3), face=1)
    plan = best_line(st, pieces)                 # 线伤 6: 6 ≥ 10-3-1=6
    assert plan.lethal is True and plan.total == 6
    st2 = replace(initial_state(4, (("BIG", 4),), 0, enemy_total=11,
                                dealt_total=3), face=1)
    assert best_line(st2, pieces).lethal is False  # 6 < 11-3-1=7


def test_best_line_board_atk_from_snapshot_and_none_enemy():
    pieces = {("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                                is_spell=True)}
    st = initial_state(4, (("BIG", 4),), 0, enemy_total=6, board_atk=2)
    plan = best_line(st, pieces)                 # 6+2 ≥ 6
    assert plan.lethal is True and plan.board_atk == 2 and plan.total == 8
    stn = initial_state(4, (("BIG", 4),), 0)     # enemy_total=None
    pn = best_line(stn, pieces)
    assert pn.lethal is False and pn.enemy_total is None
