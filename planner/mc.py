"""mc_plan —— MC 抽牌采样外壳(T3; PLAY_ADVICE §1-T3/§6.2/§6.3/§6.7, DESIGN §6.2/§6.3)。

一句话: N 条采样线 —— 牌库剩余组成(remaining 多重集)不放回抽 k 张 → 每条线
内跑 T1 DFS(planner.dfs.best_line, 线内纯确定) → 总伤分布;
P(斩杀) = P(total ≥ 敌血+甲)。纯推理层: 无 IO、无全局态、机读措辞零
(结论词/阈值归 render); planner 不碰 knowledge, remaining/costs 由调用方传入。

== 抽样口径(定版, 全部有出处) ==

1. 样牌进 known_draws 队尾, 不直接进手牌。DESIGN §6.1"engine_on: 每打一张
   法术 → 抽牌来自 MC 预抽样"、任务口径"抽样只补未知抽牌位": simstate.play
   里未知抽牌位 = 施法×引擎时从 known_draws 弹队头(空则 drawn+1 只计数)。
   直接并手会让线凭空多 k 张免引擎可打牌 → P(斩杀)系统性虚高, 违背
   "宁漏勿错"。已有 known_draws(台账顶/底牌)在前, 样牌按抽样序在后
   (物理序: 顶牌先抽)。
2. remaining 口径对齐 analysis.lethal_plan 既有台账: knowledge.rebuild 的
   remaining = decklist − hand − used − lost, 已知顶/底牌实体仍在 DECK 区
   → 仍计入 remaining。mc_plan 据 DESIGN §6.2"已知牌从抽样池剔除"把
   state.known_draws 每个 (cid,_) 从抽样池扣 1 张 —— 它们走确定通道,
   不扣会与确定抽双计; 扣减只作用于本函数的池副本, 不改写调用方 dict。
   unknown_middle>0 时底牌不在 known_draws(lethal_plan 口径) → 留在池中按
   均匀未知位参与抽样(底牌被提前抽到的轻微偏差, 既定均匀近似, 挂 M5 回测
   校准); unknown_middle==0 时顶+底都在 known_draws → 全部剔除, 池=纯未知。
3. cost 获取用 (cid,cost) 预映射(costs: cid→卡表面值费), 二选一定死, 不收
   回调 —— 纯函数层不收可藏 IO/活对象的 callable; 预映射缺失按 0 诚实降级
   (同 lethal_plan"卡表缺牌→0"口径)。注意 pieces 必须含 (cid, 预映射费)
   键, 缺键该牌按 inert Piece(仅费)参与, 降级口径与 T1 一致。
4. k 缺省 = hand_draws + engines×手牌数: DESIGN §6.2"k←本线预计消耗的抽牌
   数(拍卖师线按法术数估, 保守给上限)"的可计算化 —— 每个手牌都可能是法术,
   每张法术触发 engines 次抽牌。多抽的样牌滞留队尾不进线, 过量无代价、
   只多付可忽略的抽样成本。局限: 抽来的法术再施法再抽的链式抽牌可超出
   此上界(欠采样), 需要时调用方显式传 k。
5. hand_draws = 回合确定抽牌通道(DESIGN §6.3 预备模式"固定抽 1 张(未知 →
   并入 MC 抽样)"): 直接并入手牌(回合抽牌不经引擎), 与线内引擎抽位共用
   同一次不放回抽样, 不双计。

== P(斩杀)与确定性 ==

- p_lethal = 斩杀线数/实际线数, 仅 enemy_total 不为 None 时有意义(None →
  p_lethal=None, 分布照出)。
- 随机源只用 random.Random(seed) 顺序派生: 同 seed + budget_ms=None →
  两次调用逐位一致(测试钉死)。budget_ms 生效时提前停, n=随机器的实际
  线数(诚实回报), 跨跑不保证一致 —— 确定性与时延墙二选一, 由调用方定。

== 性能实测(Win10 x64 / Python 3.11, 2026-09-14) ==

- 小局面(手牌 1 + 0 费链, k=4): n=2000 ≈ 0.1s。
- 典型中局(手牌 7, 引擎 1, k 缺省=7, 池 15): ≈27.6 ms/线 → n=2000 ≈ 55s。
- 恶意最坏(手牌 10 混 0 费链/减费, 引擎 1, k 缺省=10, 池 20): ≈1.0 s/线
  → n=2000 ≈ 35min。爆炸主因 = 引擎把样牌抽进手牌(10→20 张): 静态手牌
  DFS 实测 10 张 26ms / 15 张 375ms(每 +5 张 ≈ ×14)。
- 同恶意局面 budget_ms=1000: 1.25s 完成 2 线(n=2 诚实回报)。

超预算降级定版(按任务口径"诚实降级参数而非复杂剪枝"): budget_ms 硬墙钟
停 + 调用方显式小 k(2~4)/小 n(100~300); 交互接线建议 budget_ms 500~1000
(牺牲跨跑一致性换吞吐)或固定小 n 保确定性。结构性提速(线间 memo 共享、
手牌 10 张上限建模)在 planner/dfs.py、simstate.py —— 本切片不动既有文件,
留 T2 后接线方拍板(见 tests/test_lethal_mc.py 头注)。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace

from .dfs import best_line
from .plan import Plan
from .simstate import SimState


@dataclass(frozen=True)
class McPlan:
    """MC 采样分布的机读事实集(措辞零; 每字段用途如下)。

    n           实际完成线数 = len(totals); budget_ms 提前停时 < 请求数,
                消费方应据 n 评估置信度(±1/√n 量级)。
    seed        传入种子原样回传(复现锚: 同 seed 无 budget_ms 可重放同分布)。
    k           每线实际抽样张数(已按抽样池截断; 口径见模块注释 4)。
    hand_draws  实际直接入手的张数(回合抽牌通道, 口径见模块注释 5)。
    p_lethal    P(total ≥ enemy_total) = 斩杀线数/n; enemy_total=None 时为
                None(无判定基准, 分布照出)。
    mean        总伤分布均值(每线等权)。
    p90         总伤 90 分位, 最近秩法: 升序 totals[ceil(0.9n)-1]。
    max         分布最大总伤(抽样意义下的线上限, 非全局上界)。
    best_plan   max 总伤那条线的完整 Plan(actions/mana_trace 供展示;
                平手取先采样到, 保证同 seed 可重放)。
    totals      每线 total 原样 —— 分布的原始事实, 消费方可自行重算任意
                分位数/方差, 也是确定性断言锚。
    enemy_total 判定基准(血+甲)原样回传。
    """
    n: int
    seed: int
    k: int
    hand_draws: int
    p_lethal: float | None
    mean: float
    p90: int
    max: int
    best_plan: Plan
    totals: tuple
    enemy_total: int | None


def mc_plan(state: SimState, pieces: dict, remaining, enemy_total=None, *,
            costs=None, n: int = 2000, seed: int = 0, k: int | None = None,
            hand_draws: int = 0, board_atk: int = 0,
            budget_ms: float | None = None) -> McPlan:
    """采样 n 条线: 每线不放回抽 k 张样牌接在 state.known_draws 队尾
    (前 hand_draws 张直接并入手牌) → best_line → total; 汇总成 McPlan。

    state     T1 决策点快照(engines/known_draws/disc_* 均按 lethal_plan 口径
              构造; known_draws 成员会被从抽样池剔除, 见模块注释 2)。
    pieces    {(cid, cost): Piece}; 必须覆盖样牌 (cid, costs[cid]) 键,
              缺键按 inert Piece 诚实降级。
    remaining {cid: 张数} 牌库剩余组成(knowledge 台账口径, 含已知顶/底牌,
              本函数自行剔除 known_draws 成员; 只读不改写)。
    costs     {cid: 卡表面值费} 预映射(口径见模块注释 3); 缺失按 0。
    k         每线抽样张数; None = hand_draws + engines×手牌数(保守上限)。
    n/seed    线数与随机种子; budget_ms=None 时同 seed 逐位可复现。
    board_atk 可直击脸的场攻(原样传给 best_line, 计入 total/lethal)。
    budget_ms 墙钟预算, 到点停(诚实 n; 生效时放弃跨跑确定性)。
    """
    if n < 1:
        raise ValueError(f"n 必须 ≥1, 收到 {n}")
    cost_of = (costs or {}).get

    # 抽样池: remaining 多重集展开, 剔除 known_draws 成员(口径见模块注释 2)
    pool = {cid: cnt for cid, cnt in remaining.items() if cnt > 0}
    for cid, _c in state.known_draws:
        if pool.get(cid):
            pool[cid] -= 1
    pool_list = [cid for cid, cnt in pool.items() for _ in range(cnt)]

    k_eff = min(k if k is not None else _default_k(state, hand_draws),
                len(pool_list))
    hd = min(hand_draws, k_eff)
    if k_eff <= 0:                      # 无未知抽牌位: 分布退化为基准线
        return _degenerate(state, pieces, enemy_total, board_atk, seed,
                            k_eff, hd)

    rng = random.Random(seed)
    totals: list[int] = []
    best: Plan | None = None
    best_total = -1
    t0 = time.perf_counter()
    for _ in range(n):
        sample = rng.sample(pool_list, k_eff)
        hand = list(state.hand) + [(cid, cost_of(cid, 0) or 0)
                                   for cid in sample[:hd]]
        queue = state.known_draws + tuple((cid, cost_of(cid, 0) or 0)
                                          for cid in sample[hd:])
        plan = best_line(replace(state, hand=tuple(sorted(hand)),
                                 known_draws=queue),
                         pieces, board_atk=board_atk, enemy_total=enemy_total)
        totals.append(plan.total)
        if plan.total > best_total:     # 平手取先采样到(同 seed 可重放)
            best_total, best = plan.total, plan
        if budget_ms is not None and (time.perf_counter() - t0) * 1000.0 >= budget_ms:
            break

    m = len(totals)
    killed = (sum(1 for t in totals if t >= enemy_total) / m
              if enemy_total is not None else None)
    asc = sorted(totals)
    return McPlan(n=m, seed=seed, k=k_eff, hand_draws=hd, p_lethal=killed,
                  mean=sum(totals) / m,
                  p90=asc[max(0, -(-9 * m // 10) - 1)], max=asc[-1],
                  best_plan=best, totals=tuple(totals),
                  enemy_total=enemy_total)


def _default_k(state: SimState, hand_draws: int) -> int:
    """k 缺省 = hand_draws + engines×手牌数(模块注释 4; 过量样牌滞留队尾
    无代价)。"""
    return hand_draws + state.engines * len(state.hand)


def _degenerate(state, pieces, enemy_total, board_atk, seed, k, hd):
    """无可抽样位(k=0 或空池): 所有线同质 → 单条基准线, 分布退化。
    P(斩杀)∈{0,1} 精确; enemy_total=None → p_lethal=None。"""
    plan = best_line(state, pieces, board_atk=board_atk, enemy_total=enemy_total)
    if enemy_total is None:
        killed = None
    else:
        killed = 1.0 if plan.total >= enemy_total else 0.0
    return McPlan(n=1, seed=seed, k=k, hand_draws=hd, p_lethal=killed,
                  mean=float(plan.total), p90=plan.total, max=plan.total,
                  best_plan=plan, totals=(plan.total,),
                  enemy_total=enemy_total)
