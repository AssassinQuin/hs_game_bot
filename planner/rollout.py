"""多回合 rollout —— 自闭卡组留牌模拟(v3.1 设计 §3/§4)。

与 dfs 的语义分歧(设计 §3 单列条款): dfs 中未知抽牌只计数, 经 exp_per_draw
进期望注记; 本模块把抽样牌序作为 known_draws 队列喂给 play —— 抽到的牌
真实入手, 期望折算通道在本模块永不出现。

脚本策略(设计 §3, 2026-09-15 v2 迭代): 引擎/回费牌尽快打; 伤害段牌仅
引擎在场时打(转抽牌循环燃料); 其余无段非引擎非回费牌**囤手不打** ——
v1 对其一律打, 0 费法术密集卡组 T1 即倾泻至 2 张, 轨迹层实测手牌
系统性偏小(中位差 3.0, 4020 点), 真实对局是囤组件的。
启动判定复用 best_line(board_atk=0 保守口径, 宁漏勿错)。

启动判定性能(P1, FOLLOWUPS Issue #1): 逐回合 best_line DFS 在手牌 8-10 张时
组合爆炸(实测单推演 ~4.7-10s)。两道前置:
- **封闭回合乐观上界短路(严格无损)**: 手牌本回合不可能再进新牌(无引擎在场
  且手中无引擎牌/独立抽牌牌/可触发引擎的法术)时, 手牌伤害上界 < 敌方血甲
  的回合直接跳过 DFS —— 上界恒 ≥ DFS 可达伤害, 只可能少算"不可能启动"的
  回合, 启动语义零变化;
- **known_draws 截断(保守方向)**: 可抽回合喂给启动 DFS 的已知抽牌截断到
  LAUNCH_DRAWS_CAP(上界口径同步把可抽前缀计入, 对截断语义封闭)。截断只
  可能漏报"深抽后才够伤"的启动线, 偏差方向保守, 量化实测入档 spec §5。
  策略轨迹(手牌演化)不受截断影响 —— 只动 launch 检查的输入。

简化口径(v1, 轨迹层校准兜底): 被换牌视为洗回统一抽牌流; 忽略手牌上限、
疲劳与费用锁定; T1 不自然抽、T≥2 每回合抽 1; coin 注入为 0 费回费法术。

快照序列推进(SimSnapshot 统一, spec §4): 旧实现的 5 个散装循环变量
(hand/stream/engines/sp/disc_hand)全部寄居 SimSnapshot —— 回合间用
advance_turn 推进(自然抽/face 结转/disc_next 过期都在转移里), 策略出牌
用 play 纯函数迭代, 各回合策略收尾后的快照入 RolloutResult.snapshots。
纯函数: 无 IO/全局态; 随机性全在调用方(trainer.sim 的 CRN 层)。
"""
from __future__ import annotations

import dataclasses

from .dfs import best_line
from .simstate import advance_turn, initial_state, play

LAUNCH_DRAWS_CAP = 12   # 启动 DFS 单回合可消费的已知抽牌上限(超参数:
                        # 依据 = 2026-09-15 真实语料偏差量化, 量化口径与
                        # 结果入档 spec §5 性能门; 偏差方向恒为漏报/保守)


@dataclasses.dataclass(frozen=True)
class RolloutResult:
    launch_turn: int | None            # 首个启动回合; 未启动 None
    turns: int                         # 推演到的回合数
    hand_sizes: tuple[int, ...] = ()   # 各回合策略收尾后手牌数(轨迹层校准用)
    engines: int = 0                   # 终态在场引擎数
    snapshots: tuple = ()              # 各回合策略收尾后的 SimSnapshot(spec §4)


def _policy_playable(key, pieces: dict, engines: int) -> bool:
    """脚本策略单卡裁定 v2: 引擎/回费 → 打; 伤害段 → 仅引擎在场打;
    其余(无段非引擎非回费) → 囤(v1 打了, 轨迹层校准实测偏小)。"""
    p = pieces.get(key)
    if p is None or p.engine or p.mana_gain:
        return True
    if p.segments:
        return engines > 0
    return False


def _face_ceiling(sp: int, keys, pieces: dict) -> int:
    """乐观脸伤上界: keys 全部打出且法强增益全部先生效。

    scaled 段按可达最大法强(sp + 手中全部法强增益)逐段计, 裸段按面值计;
    pieces 缺键计 0(inert 无段)。只用于"不可能启动"短路, 恒 ≥ DFS 可达值。"""
    sp_max, scaled_sum, scaled_hits, flat = sp, 0, 0, 0
    for key in keys:
        p = pieces.get(key)
        if p is None:
            continue
        sp_max += p.spellpower_gain
        if p.segments:
            if p.spell_scaled:
                scaled_sum += sum(p.segments)
                scaled_hits += len(p.segments)
            else:
                flat += sum(p.segments)
    return scaled_sum + sp_max * scaled_hits + flat


def _drawable(st, pieces: dict) -> bool:
    """launch DFS 期间手牌是否可能进新牌(决定上界是否封闭)。

    开 = 手中有引擎牌(打出后后续法术触发抽牌)/独立抽牌牌/在场引擎×手中有
    法术; 过近似安全 —— 判"开"只是少过滤, 不影响正确性。"""
    for key in st.hand:
        p = pieces.get(key)
        if p is None:
            continue
        if p.engine or p.draw_n:
            return True
        if p.is_spell and st.engines:
            return True
    return False


def rollout(full_order, *, offered, keep, coin, pieces, cost_of, k_max,
            enemy_totals, launch_draws_cap: int | None = None) -> RolloutResult:
    """一副完整牌序 + 留牌决策 → 启动回合。

    full_order: 整副牌(含重复)的一个随机序 —— CRN 层对全部 keep 集共用;
    keep 中的卡按首次出现从 full_order 移除, 剩余序列即抽牌流。
    enemy_totals: 按回合(1-based)的敌方有效血甲; None 回合跳过启动判定。
    launch_draws_cap: 启动 DFS 可消费的已知抽牌上限(None → 模块默认)。
    """
    if len(enemy_totals) < k_max:
        raise ValueError(
            f"enemy_totals 长度 {len(enemy_totals)} < k_max {k_max}(调用方契约违反)")
    cap = LAUNCH_DRAWS_CAP if launch_draws_cap is None else launch_draws_cap
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

    snap = initial_state(
        1, tuple((c, cost_of[c]) for c in hand), 0,
        known_draws=tuple((c, cost_of[c]) for c in stream),
        enemy_total=enemy_totals[0])
    hand_sizes: list[int] = []
    snapshots: list = []
    for t in range(1, k_max + 1):
        if t >= 2:
            # v1 口径(spec §9 不改): 血甲曲线是逐回合经验血甲(分布已含真实
            # 伤害), 模拟侧跨回合已打伤害不得再扣(双计) —— dealt_total 逐回
            # 合清零, 等价旧实现"face 不跨回合携带"; 本回合 face 仍进统一算式
            snap = dataclasses.replace(
                advance_turn(snap, enemy_total=enemy_totals[t - 1]),
                dealt_total=0)
        while True:                          # 策略出牌: 打到打不动为止
            played = False
            for i, key in enumerate(snap.hand):
                if _policy_playable(key, pieces, snap.engines):
                    try:
                        snap = play(snap, i, pieces)
                        played = True
                        break
                    except ValueError:       # 不可支付: 试下一张
                        continue
            if not played:
                break
        hand_sizes.append(len(snap.hand))
        snapshots.append(snap)
        if snap.enemy_total is not None:
            # P1 前置(原样保留): 封闭回合手牌上界(可抽回合连 cap 内前缀一起
            # 计)够不到血甲 → 免 DFS; 可抽回合 known_draws 截断到 cap
            if _drawable(snap, pieces):
                known = snap.known_draws[:cap]
                ceiling = _face_ceiling(snap.sp, snap.hand + known, pieces)
            else:
                known = ()
                ceiling = _face_ceiling(snap.sp, snap.hand, pieces)
            if ceiling >= snap.enemy_total - snap.face:   # need(dealt 恒 0)
                # 统一斩杀算式吸收旧 replace(face=0) 技巧: best_line 从快照
                # 读 enemy_total/face, 只算手牌剩余爆发
                plan = best_line(dataclasses.replace(snap, known_draws=known),
                                 pieces)
                if plan.lethal:
                    return RolloutResult(t, t, tuple(hand_sizes), snap.engines,
                                         tuple(snapshots))
    return RolloutResult(None, k_max, tuple(hand_sizes), snap.engines,
                         tuple(snapshots))
