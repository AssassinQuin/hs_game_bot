"""planner/mc.py(T3 MC 抽牌采样外壳)—— 固定 seed 采样线 × 线内 T1 DFS。

TDD 留痕(每 cycle 一测一实现, 纵向切片):
  c1  采样外壳 tracer: McPlan 分布字段 + P(斩杀)∈[0,1]
  c2  固定 seed 两次跑逐位一致
  c3  known_draws(已知顶牌)不进抽样池(台账口径: remaining 含顶牌, mc 侧扣减)
  c4  全伤害牌库闭式解: 超几何 P 对得上; 池抽干→P 退化 {0,1}
  c5  k≥池/空池/全零 remaining 边界 + 调用方 remaining 不被改写
  c6  换 seed 分布变动(防假随机)
  c7  hand_draws 通道(回合抽牌直接入手) + costs 预映射是唯一费源
  c8  k 缺省推导 = hand_draws + engines×手牌数, 且按池截断
  c9  budget_ms 诚实降级(提前停, n=实际线数)
  c10 性能 sanity(最坏构造局面 n=2000 实测)
"""
import json
import time

import pytest

from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB

from planner.mc import McPlan, mc_plan
from planner.pieces import build_piece
from planner.simstate import initial_state


# ---------------- 假卡表(无网络, 同 tests/test_planner.py 模式) ----------------

CARDS = [
    # MC 闭式解夹具: 0 费伤害法术 / 0 费无害法术(喂引擎链)
    {"id": "ZAP", "name": "电震", "type": "SPELL", "cost": 0,
     "text": "造成$3点伤害。"},
    {"id": "FILLER", "name": "回费 filler", "type": "SPELL", "cost": 0,
     "text": "在本回合中，获得一个 法力水晶。"},
    # 引擎(施法抽牌)
    {"id": "AUCTION", "name": "黑市拍卖师", "type": "MINION", "cost": 5,
     "text": "每当你施放一个法术后，抽一张牌。"},
    # 费闸夹具(c7: 预映射费决定可否支付)
    {"id": "BIG", "name": "大伤害法术", "type": "SPELL", "cost": 4,
     "text": "造成$5点伤害。"},
    # 最坏局面夹具(借 test_planner 混合牌型)
    {"id": "MOON", "name": "月火术", "type": "SPELL", "cost": 1,
     "text": "造成$1点伤害。"},
    {"id": "TWO_HIT", "name": "月光射线", "type": "SPELL", "cost": 2,
     "text": "对一个敌人造成$1点伤害两次。"},
    {"id": "INNERVATE", "name": "激活", "type": "SPELL", "cost": 0,
     "text": "在本回合中，获得一个 法力水晶。"},
    {"id": "HAND_DISC", "name": "生命缚誓者的礼物", "type": "SPELL", "cost": 2,
     "text": "使你手牌中法术牌的法力值消耗减少（1）点。"},
    {"id": "VANILLA", "name": "白板嘲讽", "type": "MINION", "cost": 2,
     "text": "<b>嘲讽</b>", "mechanics": ["TAUNT"]},
]


def _db(tmp_path):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(CARDS, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


@pytest.fixture
def analyzer(tmp_path):
    return EffectAnalyzer(_db(tmp_path))


def _mk_pieces(analyzer, entries):
    return {(cid, cost): build_piece(cid, cost, analyzer)
            for cid, cost in entries}


def _chain_pieces(analyzer):
    """闭式解夹具 pieces: 手牌 filler + 池中 zap/filler + 引擎(引擎本体不在
    pieces 里也行 —— engines 是 initial_state 的计数, 不需要 AUCTION Piece)。"""
    return _mk_pieces(analyzer, [("ZAP", 0), ("FILLER", 0)])


# ---------------- c1: tracer —— 分布外壳基本形状 ----------------

def test_mc_plan_returns_distribution(analyzer):
    """最小闭环: 引擎在场 + 池中混合伤害/填充牌, 采样线跑 DFS 出分布;
    P(斩杀) ∈ [0,1], 线数与字段诚实。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    plan = mc_plan(st, pieces, {"ZAP": 2, "FILLER": 2}, enemy_total=5,
                   n=50, seed=7, k=2)
    assert isinstance(plan, McPlan)
    assert plan.n == 50 and plan.seed == 7
    assert len(plan.totals) == plan.n
    assert 0.0 <= plan.p_lethal <= 1.0
    assert plan.p90 <= plan.max
    assert plan.mean <= plan.max
    assert plan.best_plan is not None and plan.best_plan.total == plan.max


# ---------------- c2: 固定 seed 逐位一致 ----------------

def test_same_seed_bit_identical(analyzer):
    """同 seed 两次跑(无 budget_ms): McPlan 全字段相等(逐位一致)。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    a = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                n=200, seed=11, k=4)
    b = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                n=200, seed=11, k=4)
    assert a == b


# ---------------- c3: known_draws(已知顶牌)不进抽样池 ----------------

def test_known_draws_excluded_from_sampling_pool(analyzer):
    """台账口径(analysis.lethal_plan): remaining 含已知顶牌实体(它仍在 DECK
    区, remaining=decklist−hand−used−lost)。mc_plan 必须把 known_draws 每个
    (cid,_) 从抽样池扣 1 张, 否则与确定通道双计。
    夹具: 顶牌=ZAP(确定抽, 每线必打 3 伤); remaining={ZAP:2,FILLER:2}, k=3。
    扣减后池={ZAP:1,FILLER:2} 恰 3 张 → 每线抽干池: 样里恰 1 张 ZAP →
    每线 total=3+3=6; 若未扣减(池 4 张抽 3)会出现 2 ZAP 样 → total=9。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0,
                       known_draws=(("ZAP", 0),), engines=1)
    plan = mc_plan(st, pieces, {"ZAP": 2, "FILLER": 2}, enemy_total=9,
                   n=40, seed=3, k=3)
    assert plan.k == 3
    assert set(plan.totals) == {6}
    assert plan.p_lethal == 0.0          # 6 < 9: 全线同质 → 退化但诚实


# ---------------- c4: 全伤害牌库闭式解(超几何) ----------------

def test_closed_form_hypergeometric(analyzer):
    """池全为已知效果牌(0 费伤害/填充法术), 引擎链保证 k 张全抽全打 →
    线内 total = 3×X, X = 样中 ZAP 数 ~ 超几何(N=6, K=3, n=4)。
    enemy=4 → 斩杀 iff X≥2; P = 1 − C(3,1)C(3,3)/C(6,4) = 1 − 3/15 = 0.8。
    E[X] = 2.0 → mean = 6.0; P(total≤6)=0.8 → 最近秩 p90=9(与 seed 无关)。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    plan = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                   n=2000, seed=0, k=4)
    assert 0.0 <= plan.p_lethal <= 1.0
    assert abs(plan.p_lethal - 0.8) <= 0.03
    assert abs(plan.mean - 6.0) <= 0.2
    assert plan.p90 == 9
    assert plan.max == 9                  # 池里有 3 张 ZAP, 4 抽可全中


def test_closed_form_pool_exhausted_degenerates(analyzer):
    """k 抽干池(6=6): 每线组成恒同 → X=3 恒成立 → P(斩杀)=1.0 精确。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    plan = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=9,
                   n=200, seed=5, k=6)
    assert set(plan.totals) == {9}
    assert plan.p_lethal == 1.0


# ---------------- c5: k/池边界 + 调用方 remaining 不被改写 ----------------

def test_k_clamped_to_pool_and_remaining_not_mutated(analyzer):
    """k>池 → 按池截断; 抽样只读 remaining(调用方 Counter/dict 原样)。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    remaining = {"ZAP": 1, "FILLER": 1}
    plan = mc_plan(st, pieces, remaining, enemy_total=9, n=30, seed=1, k=99)
    assert plan.k == 2
    assert set(plan.totals) == {3}       # 手牌 filler + 样里唯一 ZAP
    assert plan.p_lethal == 0.0
    assert remaining == {"ZAP": 1, "FILLER": 1}


def test_empty_and_zero_remaining_degenerate(analyzer):
    """空池(缺 remaining/全零计数): 无未知抽牌位 → 退化为单条基准线,
    P∈{0,1}, 不崩。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    for remaining in ({}, {"ZAP": 0, "FILLER": 0}):
        plan = mc_plan(st, pieces, remaining, enemy_total=1,
                       n=50, seed=2, k=3)
        assert plan.k == 0 and plan.n == 1 and plan.totals == (0,)
        assert plan.p_lethal == 0.0          # 手牌 filler 0 伤 < 1
    plan = mc_plan(st, pieces, {}, enemy_total=None, n=5, seed=2, k=3)
    assert plan.p_lethal is None


def test_n_below_one_rejected(analyzer):
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    with pytest.raises(ValueError):
        mc_plan(st, pieces, {"ZAP": 1}, n=0)


# ---------------- c6: 换 seed 分布变动(防假随机) ----------------

def test_seed_change_changes_distribution(analyzer):
    """同夹具换 seed: 每线样本不同 → totals 序列不同(防"固定值假随机")。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    a = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                n=200, seed=0, k=4)
    b = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                n=200, seed=1, k=4)
    assert a.totals != b.totals
    assert a.seed == 0 and b.seed == 1


# ---------------- c7: hand_draws 直入手通道 + costs 预映射唯一费源 ----------------

def test_hand_draws_enters_hand_without_engine(analyzer):
    """hand_draws=回合确定抽牌(DESIGN §6.3): 直接并入手牌, 无需引擎即可打。
    engines=0 + hand_draws=1, 池 {ZAP:1,FILLER:1} 抽 1: 抽中 ZAP → total 3。
    对照: hand_draws=0 时样牌进 known_draws 队尾, 无引擎永不下手 → 全 0。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(3, (("FILLER", 0),), 0)
    plan = mc_plan(st, pieces, {"ZAP": 1, "FILLER": 1}, enemy_total=3,
                   n=200, seed=9, k=1, hand_draws=1)
    assert plan.hand_draws == 1
    assert set(plan.totals) == {0, 3}
    assert 0.0 < plan.p_lethal < 1.0
    que = mc_plan(st, pieces, {"ZAP": 1, "FILLER": 1}, enemy_total=3,
                  n=50, seed=9, k=1, hand_draws=0)
    assert set(que.totals) == {0}


def test_costs_premap_is_sole_cost_source(analyzer):
    """样牌费 = costs 预映射(卡表面值), 预映射缺失按 0 诚实降级。
    mana=3: 预映射 BIG→9 不可支付(全 0 伤); BIG→3 可支付(5 伤)。"""
    analyzer.cache.get_or_compile(analyzer.carddb.raw("BIG"))
    pieces = {("BIG", 9): build_piece("BIG", 9, analyzer),
              ("BIG", 3): build_piece("BIG", 3, analyzer)}
    st = initial_state(3, (), 0)
    poor = mc_plan(st, pieces, {"BIG": 1}, enemy_total=5, n=10, seed=4,
                   k=1, hand_draws=1, costs={"BIG": 9})
    rich = mc_plan(st, pieces, {"BIG": 1}, enemy_total=5, n=10, seed=4,
                   k=1, hand_draws=1, costs={"BIG": 3})
    assert set(poor.totals) == {0}
    assert set(rich.totals) == {5}


# ---------------- c8: k 缺省推导(保守上限)并按池截断 ----------------

def test_k_default_engines_times_hand_capped_by_pool(analyzer):
    """k 缺省 = engines×手牌数(DESIGN §6.2"按法术数估, 保守给上限"; 多抽的
    样牌滞留队尾不影响线, 只浪费可忽略的抽样成本) → 池小按池截断;
    engines=0 且 hand_draws=0 → 无未知抽牌位 → k=0(诚实退化, 不造随机性)。"""
    pieces = _chain_pieces(analyzer)
    hand = (("FILLER", 0), ("FILLER", 0), ("ZAP", 0), ("ZAP", 0))
    st = initial_state(0, hand, 0, engines=2)
    plan = mc_plan(st, pieces, {"ZAP": 4, "FILLER": 4}, enemy_total=5, n=20)
    assert plan.k == 8 and plan.hand_draws == 0
    small = mc_plan(st, pieces, {"ZAP": 1}, enemy_total=5, n=20)
    assert small.k == 1
    st0 = initial_state(0, hand, 0)
    no_engine = mc_plan(st0, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=5,
                        n=20)
    assert no_engine.k == 0 and no_engine.n == 1
    assert no_engine.totals == (6,)      # 只剩手牌基准线: 两张 ZAP = 6 伤


# ---------------- c9: budget_ms 诚实降级(提前停) ----------------

def test_budget_ms_stops_early_and_stays_consistent(analyzer):
    """budget_ms 极小 → 提前停: n=实际线数(<请求数, ≥1), 字段自洽。
    (时延相关 → n 跨跑会变; 确定性只在 budget_ms=None 承诺, docstring 写明。)"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    plan = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                   n=100000, seed=0, k=4, budget_ms=1.0)
    assert 1 <= plan.n < 100000
    assert len(plan.totals) == plan.n
    assert 0.0 <= plan.p_lethal <= 1.0
    assert plan.best_plan.total == plan.max


# ---------------- c10: 性能 sanity ----------------

def test_perf_n2000_small_state_and_budgeted_worst_state(analyzer):
    """性能 sanity(CI 安全档, 实测数字见 planner/mc.py docstring):
    a) 小局面(1 手牌 + 0 费链) n=2000 直跑 —— 实测 ~0.1s, 宽松上限 10s;
    b) 恶意最坏局面(10 手牌 0 费链+减费+引擎, 池 20)不裸跑 n=2000
       (实测 ~1s/线), 用 budget_ms 提前停 —— 必须及时返回且 n 诚实。"""
    pieces = _chain_pieces(analyzer)
    st = initial_state(0, (("FILLER", 0),), 0, engines=1)
    t0 = time.perf_counter()
    plan = mc_plan(st, pieces, {"ZAP": 3, "FILLER": 3}, enemy_total=4,
                   n=2000, seed=0, k=4)
    assert (time.perf_counter() - t0) < 10.0
    assert plan.n == 2000

    hand = [("MOON", 1), ("TWO_HIT", 2), ("INNERVATE", 0), ("HAND_DISC", 2),
            ("ZAP", 0), ("ZAP", 0), ("BIG", 4), ("VANILLA", 2),
            ("FILLER", 0), ("SPGUY", 3)]
    costs = {"MOON": 1, "TWO_HIT": 2, "INNERVATE": 0, "HAND_DISC": 2,
             "ZAP": 0, "BIG": 4, "VANILLA": 2, "FILLER": 0, "SPGUY": 3,
             "AUCTION": 5}
    full = _mk_pieces(analyzer, [("MOON", 1), ("TWO_HIT", 2), ("INNERVATE", 0),
                                 ("HAND_DISC", 2), ("ZAP", 0), ("BIG", 4),
                                 ("VANILLA", 2), ("FILLER", 0), ("SPGUY", 3),
                                 ("AUCTION", 5)])
    worst = initial_state(10, tuple(sorted(hand)), 0, engines=1)
    remaining = {"MOON": 2, "TWO_HIT": 2, "INNERVATE": 2, "HAND_DISC": 2,
                 "ZAP": 2, "BIG": 2, "VANILLA": 2, "FILLER": 2,
                 "SPGUY": 2, "AUCTION": 2}
    t0 = time.perf_counter()
    bad = mc_plan(worst, full, remaining, enemy_total=30, n=2000, seed=0,
                  costs=costs, budget_ms=250.0)
    wall = time.perf_counter() - t0
    assert 1 <= bad.n < 2000
    assert len(bad.totals) == bad.n
    assert wall < 30.0                  # 实测 ~0.4s(1 条线超预算即停)


# ---------------- 审计 2026-09-17: mc_plan 两口径统一(统一斩杀算式) ----------------

def test_mc_plan_p_lethal_matches_best_plan_formula_with_face(analyzer):
    """face>0 快照: p_lethal 与 best_plan.lethal 必须同口径(统一算式
    total ≥ enemy_total−dealt_total−face)——修复前一个 True 一个 False。"""
    from dataclasses import replace

    pieces = _mk_pieces(analyzer, [("BIG", 4)])
    st = initial_state(4, (("BIG", 4),), 0)
    mp = mc_plan(replace(st, face=3), pieces, {}, enemy_total=7, n=5, seed=0)
    assert mp.best_plan.lethal is True              # 5 ≥ 7−0−3
    assert mp.p_lethal == 1.0                       # 同口径: 全线 ≥ need
    mp2 = mc_plan(st, pieces, {}, enemy_total=7, n=5, seed=0)
    assert mp2.p_lethal == 0.0 and mp2.best_plan.lethal is False


def test_mc_plan_p_lethal_main_path_face_and_dealt(analyzer):
    """主路径契约(池非空非退化, 二轮审计中#2): face>0/dealt_total>0 时
    killed 与 best_plan.lethal 同用统一算式折减 need——主路径被突变回裸
    enemy_total 比较时本用例必挂(修复前主路径零覆盖)。"""
    from dataclasses import replace

    pieces = _mk_pieces(analyzer, [("BIG", 4)])
    st = initial_state(4, (("BIG", 4),), 0)
    # face=3: total=5, need=7−0−3=4 → 5≥4 全线可斩(突变裸比较 5<7 → 0.0)
    mp = mc_plan(replace(st, face=3), pieces, {"BIG": 2}, enemy_total=7,
                 n=6, seed=0, k=2)
    assert mp.k == 2 and mp.n == 6                  # 主路径(非退化)实证
    assert mp.p_lethal == 1.0 and mp.best_plan.lethal is True
    # dealt_total=2: need=7−2−0=5 → 5≥5 仍可斩(突变版 0.0)
    mp2 = mc_plan(replace(st, dealt_total=2), pieces, {"BIG": 2},
                  enemy_total=7, n=6, seed=0, k=2)
    assert mp2.p_lethal == 1.0 and mp2.best_plan.lethal is True
    # 无折减量: need=7 → 5<7 不可斩
    mp3 = mc_plan(st, pieces, {"BIG": 2}, enemy_total=7, n=6, seed=0, k=2)
    assert mp3.p_lethal == 0.0 and mp3.best_plan.lethal is False
