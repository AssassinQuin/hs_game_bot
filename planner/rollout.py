"""多回合 rollout —— 自闭卡组留牌模拟(v3.1 设计 §3/§4)。

与 dfs 的语义分歧(设计 §3 单列条款): dfs 中未知抽牌只计数, 经 exp_per_draw
进期望注记; 本模块把抽样牌序作为 known_draws 队列喂给 play —— 抽到的牌
真实入手, 期望折算通道在本模块永不出现。

脚本策略(设计 §3, 2026-09-15 v2 迭代): 引擎/回费牌尽快打; 伤害段牌仅
引擎在场时打(转抽牌循环燃料); 其余无段非引擎非回费牌**囤手不打** ——
v1 对其一律打, 0 费法术密集卡组 T1 即倾泻至 2 张, 轨迹层实测手牌
系统性偏小(中位差 3.0, 4020 点), 真实对局是囤组件的。
启动判定复用 best_line(board_atk=0 保守口径, 宁漏勿错)。

简化口径(v1, 轨迹层校准兜底): 被换牌视为洗回统一抽牌流; 忽略手牌上限、
疲劳与费用锁定; T1 不自然抽、T≥2 每回合抽 1; coin 注入为 0 费回费法术。
纯函数: 无 IO/全局态; 随机性全在调用方(trainer.sim 的 CRN 层)。
"""
from __future__ import annotations

import dataclasses

from .dfs import best_line
from .simstate import initial_state, play


@dataclasses.dataclass(frozen=True)
class RolloutResult:
    launch_turn: int | None            # 首个启动回合; 未启动 None
    turns: int                         # 推演到的回合数
    hand_sizes: tuple[int, ...] = ()   # 各回合策略收尾后手牌数(轨迹层校准用)
    engines: int = 0                   # 终态在场引擎数


def _policy_playable(key, pieces: dict, engines: int) -> bool:
    """脚本策略单卡裁定 v2: 引擎/回费 → 打; 伤害段 → 仅引擎在场打;
    其余(无段非引擎非回费) → 囤(v1 打了, 轨迹层校准实测偏小)。"""
    p = pieces.get(key)
    if p is None or p.engine or p.mana_gain:
        return True
    if p.segments:
        return engines > 0
    return False


def rollout(full_order, *, offered, keep, coin, pieces, cost_of, k_max,
            enemy_totals) -> RolloutResult:
    """一副完整牌序 + 留牌决策 → 启动回合。

    full_order: 整副牌(含重复)的一个随机序 —— CRN 层对全部 keep 集共用;
    keep 中的卡按首次出现从 full_order 移除, 剩余序列即抽牌流。
    enemy_totals: 按回合(1-based)的敌方有效血甲; None 回合跳过启动判定。
    """
    stream = list(full_order)
    for cid in keep:
        if cid not in stream:
            raise ValueError(f"keep 卡 {cid} 不在牌序中(调用方数据错)")
        stream.remove(cid)
    hand = list(keep)
    if coin:
        hand.append("COIN")
    fill = len(offered) - len(keep)          # 换牌补抽(v1 简化: 同一抽牌流)
    hand += stream[:fill]
    del stream[:fill]

    engines = sp = disc_hand = 0
    hand_sizes: list[int] = []
    for t in range(1, k_max + 1):
        if t >= 2 and stream:
            hand.append(stream.pop(0))       # 回合开始自然抽
        st = initial_state(
            min(10, t), tuple((c, cost_of[c]) for c in hand),
            sp=sp, disc_hand=disc_hand, engines=engines,
            known_draws=tuple((c, cost_of[c]) for c in stream))
        while True:                          # 策略出牌: 打到打不动为止
            played = False
            for i, key in enumerate(st.hand):
                if _policy_playable(key, pieces, st.engines):
                    try:
                        st = play(st, i, pieces)
                        played = True
                        break
                    except ValueError:       # 不可支付: 试下一张
                        continue
            if not played:
                break
        hand_sizes.append(len(st.hand))
        total = enemy_totals[t - 1]
        if total is not None:
            # 已打出的策略伤害先扣血, best_line 只算手牌剩余爆发(face 清零)
            plan = best_line(dataclasses.replace(st, face=0), pieces,
                             enemy_total=total - st.face)
            if plan.lethal:
                return RolloutResult(t, t, tuple(hand_sizes), st.engines)
        engines, sp, disc_hand = st.engines, st.sp, st.disc_hand
        hand = [k[0] for k in st.hand]
        stream = [k[0] for k in st.known_draws]   # 引擎循环抽走的已入手
    return RolloutResult(None, k_max, tuple(hand_sizes), engines)
