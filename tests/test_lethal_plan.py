"""analysis.lethal_plan 切片测试(T1 契约 §2): facts dict 字段、降级路径、
确定性可斩不含期望分量。planner 以契约签名的桩模块注入(函数体内延迟
import 正为此设计; planner 本体的搜索行为由其自带测试覆盖, 这里只测
接线与事实采集口径)。store 用 conftest 现成 GameStore 夹具(真状态仓)。"""
import json
import sys
import types
from collections import Counter
from types import SimpleNamespace

import pytest
from hearthstone.enums import CardType, GameTag, Zone

from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB
from hsbot.consts import SPELLPOWER_TYPES
from hsbot.knowledge import DeckKnowledge
from hsbot.store import GameStore

from .conftest import EmptyTree, mk_full, mk_heroes, mk_pm, mk_store, mk_tag


def _write_cards(tmp_path):
    """假卡表: 火球($6/4费)、箭($2/1费)、激活(0伤0费回水晶)、无伤随从。"""
    p = tmp_path / "cards.json"
    p.write_text(json.dumps([
        {"id": "TST_FIRE", "name": "测试火球", "type": "SPELL", "cost": 4,
         "text": "造成$6点伤害。"},
        {"id": "TST_BOLT", "name": "测试箭", "type": "SPELL", "cost": 1,
         "text": "造成$2点伤害。"},
        {"id": "TST_RAMP", "name": "测试激活", "type": "SPELL", "cost": 0,
         "text": "在本回合中，获得一个 法力水晶。"},
        {"id": "TST_MIN", "name": "测试随从", "type": "MINION", "cost": 5},
    ], ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


def _scene(tmp_path, *, mana=5, sp=0, enemy=8):
    """可斩基准局面: 5费, 手牌 火球(4费)+箭(1费), 敌方总血甲=enemy。"""
    db = _write_cards(tmp_path)
    st, _log = mk_store()
    mk_heroes(st)
    st.apply(mk_tag(5, GameTag.DAMAGE, 33 - enemy))  # 敌 30-伤害+3甲 = enemy
    st.apply(mk_tag(5, GameTag.ARMOR, 3))
    st.apply(mk_tag(2, GameTag.RESOURCES, mana))
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, 0))
    if sp:
        st.apply(mk_tag(2, GameTag.CURRENT_SPELLPOWER_BASE, sp))
    st.apply(mk_full(10, "TST_FIRE", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, ZONE_POSITION=1))
    st.apply(mk_full(11, "TST_BOLT", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, ZONE_POSITION=2))
    st.current = st.friendly_key                     # 我方回合
    return st, db, EffectAnalyzer(db)


# ================= planner 桩(契约 §1 签名; 贪心打出可支付牌) =================

class _StubPiece:
    def __init__(self, card_id, cost, analyzer):
        self.card_id, self.cost = card_id, cost
        self.base = analyzer.burst_damage(card_id, 0) or 0
        self.spell_scaled = (self.base > 0 and
                             analyzer.carddb.cardtype(card_id) in SPELLPOWER_TYPES)
        self.engine = analyzer.has_mechanic(card_id, "cast_draw")

    def damage_at(self, sp):
        return (self.base + sp) if self.spell_scaled else self.base


@pytest.fixture
def planner_stub(monkeypatch):
    calls = []
    pkg = types.ModuleType("planner")
    pkg.__path__ = []
    m_dfs = types.ModuleType("planner.dfs")
    m_pieces = types.ModuleType("planner.pieces")
    m_sim = types.ModuleType("planner.simstate")

    m_pieces.build_piece = lambda cid, cost, analyzer: _StubPiece(cid, cost, analyzer)
    m_sim.initial_state = (lambda mana, hand_cards, sp, known_draws=(), disc_hand=0,
                           disc_next=0, engines=0: SimpleNamespace(
                               mana=mana, hand=tuple(sorted(hand_cards)), sp=sp,
                               known_draws=tuple(known_draws), engines=engines,
                               disc_hand=disc_hand, disc_next=disc_next))

    def best_line(state, pieces, *, board_atk=0, enemy_total=None,
                  exp_per_draw=0.0):
        calls.append({"state": state, "pieces": pieces, "board_atk": board_atk,
                      "enemy_total": enemy_total, "exp_per_draw": exp_per_draw,
                      "engines": getattr(state, "engines", None)})
        mana, sp, face, actions = state.mana, state.sp, 0, []
        for cid, cost in state.hand:
            piece = pieces.get((cid, cost))
            if piece is None or cost > mana:         # 缺键=惰性牌(仅费), 不可支付跳过
                continue
            mana -= cost
            actions.append((cid, cost))
            face += piece.damage_at(sp)
        total = face + board_atk
        return SimpleNamespace(
            actions=tuple(actions), total=total, face_det=face,
            face_exp=int(round(exp_per_draw)),
            lethal=enemy_total is not None and total >= enemy_total,
            enemy_total=enemy_total, board_atk=board_atk, uncovered_n=0,
            mana_trace=())

    m_dfs.best_line = best_line
    for name, mod in (("planner", pkg), ("planner.dfs", m_dfs),
                      ("planner.pieces", m_pieces), ("planner.simstate", m_sim)):
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def _lethal_plan(st, k, a, **kw):
    from hsbot.analysis import lethal_plan
    return lethal_plan(st, k, a, **kw)


# ================= 测试 =================

def test_lethal_scene_facts_and_actions(tmp_path, planner_stub):
    """可斩局面: lethal=True, 可支付牌全部成线(顺序=手牌规范化序),
    mana 取 store 实时水晶, 敌血甲取 hero_total_hp。"""
    st, db, a = _scene(tmp_path, mana=5)
    plan = _lethal_plan(st, None, a)
    assert plan["lethal"] is True
    assert plan["face_det"] == 8 and plan["total"] == 8      # 火球6+箭2, 场攻0
    assert plan["actions"] == [("TST_BOLT", 1), ("TST_FIRE", 4)]
    assert plan["enemy_total"] == 8 and plan["board_atk"] == 0
    call = planner_stub[0]
    assert call["state"].mana == 5 and call["state"].sp == 0
    assert call["enemy_total"] == 8 and call["board_atk"] == 0


def test_hand_cost_prefers_cost_tag_and_missing_carddb(tmp_path, planner_stub):
    """手牌费口径: COST 标签(减费在身)优先于卡表基础费; 卡表缺牌 → 0。"""
    st, db, a = _scene(tmp_path, mana=3)
    st.apply(mk_tag(10, GameTag.COST, 2))            # 火球 4→2, 3费可双开
    st.apply(mk_full(12, "TST_GHOST", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, ZONE_POSITION=3))
    plan = _lethal_plan(st, None, a)
    hand = dict(planner_stub[0]["state"].hand)
    assert hand == {"TST_FIRE": 2, "TST_BOLT": 1, "TST_GHOST": 0}
    # 桩按手牌规范化序贪心: 3费内 箭(1)+火球(2)全打出, 0费空伤牌免费也成线
    assert plan["actions"] == [("TST_BOLT", 1), ("TST_FIRE", 2),
                               ("TST_GHOST", 0)]
    assert plan["face_det"] == 8 and plan["lethal"] is True


def test_ledger_known_draws_and_deck_bounds(tmp_path, planner_stub):
    """台账侧: 已知顶恒为确定抽; 底牌仅在顶底之间无未知牌(unknown_middle==0,
    全库已知)时才进确定队列——否则该抽位实际抽到的是未知牌, 底牌降级走期望
    通道(remaining 仍含底牌; 宁漏勿错, 2026-09-14 审计修正)。牌库上界=sp+1
    单卡段伤最大值; 期望注记=Σ(剩余×当前法强伤)/剩余数。"""
    st, db, a = _scene(tmp_path, mana=5, enemy=30)
    k = DeckKnowledge({"TST_FIRE": 2, "TST_BOLT": 3}, db, "测试")
    k.rebuild(st)                                    # 与 watcher 批尾同序
    k.ledger.remaining = Counter({"TST_FIRE": 2, "TST_BOLT": 3})
    k.ledger.known_top = "TST_BOLT"
    k.ledger.known_bottom = [("TST_FIRE", 9), ("TST_BOLT", 7)]   # 牌位降序(深→浅)
    # 中段有未知牌(常态): 底牌不进确定队列, 只剩顶牌是确定抽
    k.ledger.unknown_middle = 5
    plan = _lethal_plan(st, k, a)
    call = planner_stub[0]
    assert call["state"].known_draws == (("TST_BOLT", 1),)
    assert call["exp_per_draw"] == pytest.approx((2 * 6 + 3 * 2) / 5)
    assert plan["enemy_total"] == 30 and plan["lethal"] is False
    # 全库已知(middle==0): 顶→浅底→深底 才是全确定队列
    k.ledger.unknown_middle = 0
    _lethal_plan(st, k, a)
    assert planner_stub[-1]["state"].known_draws == (
        ("TST_BOLT", 1), ("TST_BOLT", 1), ("TST_FIRE", 4))


def test_deterministic_lethal_excludes_expectation(tmp_path, planner_stub):
    """确定性可斩只看 face_det+场攻; 期望分量只作注记(不为可斩添头)。"""
    st, db, a = _scene(tmp_path, mana=5, enemy=8)    # 面上 8 伤恰好斩 8
    k = DeckKnowledge({"TST_FIRE": 2}, db, "测试")
    k.rebuild(st)
    k.ledger.remaining = Counter({"TST_FIRE": 2})    # 库内期望 > 0
    plan = _lethal_plan(st, k, a)
    assert plan["face_exp"] > 0                      # 期望注记存在
    assert plan["lethal"] is True and plan["total"] == 8   # 确定伤恰 8: 可斩但
    # total 仍 8 —— 期望分量(face_exp>0)没被加进 total/lethal(旧桩曾把
    # 桩自己的公式当被测语义断言, 审计 2026-09-14 弱测试#改写)


def test_no_damage_hand_empty_actions(tmp_path, planner_stub):
    """无伤害且不可支付的手牌: 仍返回 facts dict(手牌非空),
    actions 为空、lethal=False(无伤害路径可走)。"""
    db = _write_cards(tmp_path)
    st, _log = mk_store()
    mk_heroes(st)
    st.apply(mk_full(10, "TST_MIN", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1))
    st.current = st.friendly_key
    plan = _lethal_plan(st, None, EffectAnalyzer(db))
    assert plan is not None
    assert plan["actions"] == [] and plan["face_det"] == 0
    assert plan["lethal"] is False


def test_disabled_and_degraded_paths(tmp_path, planner_stub):
    """开关关 / 我方未解析 / 无手牌 → None(信息区保持原样)。"""
    st, db, a = _scene(tmp_path)
    assert _lethal_plan(st, None, a, enabled=False) is None
    assert planner_stub == []                        # 关闭时不触 planner
    bare = GameStore(carddb=db, battletag="湫然#51704",
                     tree=EmptyTree(), player_manager=mk_pm())
    assert _lethal_plan(bare, None, a) is None       # 我方未解析
    empty, _log = mk_store()
    mk_heroes(empty)
    empty.current = empty.friendly_key
    assert _lethal_plan(empty, None, a) is None      # 无手牌


def test_facts_dict_matches_contract(tmp_path, planner_stub):
    """返回 dict 字段与 T1 契约 §2 完全一致(键集合与取值类型)。"""
    st, db, a = _scene(tmp_path)
    plan = _lethal_plan(st, None, a)
    assert set(plan) == {"actions", "total", "face_det", "face_exp",
                         "lethal", "enemy_total", "board_atk", "uncovered_n"}
    assert isinstance(plan["actions"], list)
    assert all(isinstance(a, tuple) and len(a) == 2 for a in plan["actions"])
    for key in ("total", "face_det", "face_exp", "board_atk", "uncovered_n"):
        assert isinstance(plan[key], int), key
    assert isinstance(plan["lethal"], bool)
    assert plan["enemy_total"] is None or isinstance(plan["enemy_total"], int)


# ---------------- 审计 2026-09-14: 台账队列去重 / engines 接线 / 场攻合法性 ----------------

def _add_card(db_cards, cid, name, ctype, text):
    db_cards.append({"id": cid, "name": name, "type": ctype, "text": text})


def test_known_draws_queue_no_top_duplication(tmp_path, planner_stub):
    """审计 中#4: rebuild 的 bottom_map 收录 pos=1 顶牌 → 全库已知时
    queue=[top]+reversed(bottom) 把顶牌计两遍, 引擎抽牌时同一张牌两次入手
    (实测 3 张库双引擎算出 12 伤, 真实上限 6)。修正: 底牌队列剔除顶牌,
    牌位升序=抽牌序。"""
    st, db, a = _scene(tmp_path, mana=5, enemy=30)
    for eid, cid, pos in ((20, "TST_BOLT", 1), (21, "TST_BOLT", 2),
                          (22, "TST_FIRE", 3)):
        st.apply(mk_full(eid, cid, ZONE=Zone.DECK.value, CONTROLLER=1,
                         ZONE_POSITION=pos))
    k = DeckKnowledge({"TST_FIRE": 3, "TST_BOLT": 5}, db, "测试")
    k.rebuild(st)
    assert k.ledger.unknown_middle == 0            # 前提: 全库已知
    _lethal_plan(st, k, a)
    assert planner_stub[-1]["state"].known_draws == (
        ("TST_BOLT", 1), ("TST_BOLT", 1), ("TST_FIRE", 4))   # 3 张库 3 个抽位


def test_engines_wired_from_board_cast_draw(tmp_path, planner_stub):
    """审计 高#1: engines 从未接线(恒 0)——拍卖师在场施法不抽牌, 台账顶牌
    队列永不消耗, 奇迹德引擎线整条死。修正: 我方场上 cast_draw 随从数传入
    initial_state(engines=...)。"""
    cards = [
        {"id": "TST_FIRE", "name": "测试火球", "type": "SPELL", "cost": 4,
         "text": "造成$6点伤害。"},
        {"id": "TST_AUC", "name": "测试拍卖师", "type": "MINION", "cost": 5,
         "text": "每当你施放一个法术后，抽一张牌。"},
    ]
    p = tmp_path / "cards2.json"
    p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    db2 = CardDB(p)
    st, db, a = _scene(tmp_path, mana=5)
    st.apply(mk_full(30, "TST_AUC", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1))
    a2 = EffectAnalyzer(db2)
    _lethal_plan(st, None, a2)
    assert planner_stub[0]["state"].engines == 1
    # 双拍卖师 → 2
    st.apply(mk_full(31, "TST_AUC", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=2))
    _lethal_plan(st, None, a2)
    assert planner_stub[-1]["state"].engines == 2


def test_board_atk_conservative_when_enemy_taunt(tmp_path, planner_stub):
    """审计 高#2: 敌方嘲讽在场时随从打不了脸 —— 斩杀线场攻必须为 0
    (旧: 场攻全额计入, "直伤8+场攻8≥12"式误报)。"""
    st, db, a = _scene(tmp_path, mana=1, enemy=30)
    st.apply(mk_full(32, "TST_MIN", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1, ATK=8))
    _lethal_plan(st, None, a)
    assert planner_stub[0]["board_atk"] == 8        # 无嘲讽: 场攻计入
    st.apply(mk_full(33, "TST_MIN", ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1,
                     ATK=2, TAUNT=1))
    _lethal_plan(st, None, a)
    assert planner_stub[-1]["board_atk"] == 0       # 敌方嘲讽: 必须先解嘲


def test_uncovered_counts_missing_carddb_cards(tmp_path, planner_stub):
    """审计 低#9: 卡表缺牌 = 效果未知, 计入 facts 的 uncovered_n
    (缺牌 inert 进不了出牌线, 但属于斩杀线可信度的诚实计数)。"""
    st, db, a = _scene(tmp_path, mana=5)
    st.apply(mk_full(12, "TST_GHOST", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, ZONE_POSITION=3))
    plan = _lethal_plan(st, None, a)
    assert plan["uncovered_n"] == 1                    # 桩报 0, 缺牌 +1
