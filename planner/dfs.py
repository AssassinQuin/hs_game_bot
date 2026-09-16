"""best_line —— 记忆化 + 剪枝的 DFS 最优出牌线(T1 切片3; 控制者修订版)。

最优定义(契约): face_det 最大; 平手取 actions 短者; 不存在可支付动作即叶。

记忆化(对契约 memo 规格的落地修正, 控制者 2026-09-13 裁定口径):
- 后缀不变性: 从某状态出发的"最优追加伤害"只取决于 SimSnapshot 的
  投影(memo_key, 含 disc_next_cat) —— 已累计的 face/未知抽牌计数 drawn
  不影响任何后续转移, 只在根上回加/取值。
- 因此 memo 键用该投影(而非完整 SimSnapshot): 同一决策点经不同顺序到达、
  face/drawn 不同的状态坍缩为一个键, 状态数按数量级缩减(10 异牌实测
  28703 → 5631)。每个键只算一次, 结果恒为该决策点真实局部最优 ——
  剪枝永不砍最优由暴力交叉验证背书(1200 组, 含 face 与平手最短长度, 零失配)。
- 契约原文的 incumbent 型上界剪枝与记忆化组合会"投毒"(低 incumbent 下被砍
  子树的结果被当局部最优复用)或退化为反复重算(实测 700ms+), 故不采用;
  上界公式的结论性作用(毫秒级出线)由键坍缩达成。

斩杀统一算式: lethal ⇔ face_det + board_atk ≥ enemy_total − dealt_total − face
—— 快照的 face/dealt_total 是本回合/跨回合的已扣减量, rollout 旧的
replace(face=0) 清零技巧被算式吸收(调用方无需再清零)。

纯函数: 无 IO、无全局态。
"""
from __future__ import annotations

from .plan import Plan
from .simstate import memo_key, play


def best_line(state, pieces: dict, *, exp_per_draw: float = 0.0) -> Plan:
    """从 state 出发搜索最优线, 返回 Plan(机读事实, 措辞零)。

    board_atk/enemy_total/dealt_total 从快照读(SimSnapshot 统一); exp_per_draw
    只进期望注记(face_exp), 不参与分支裁剪(见模块注释)。斩杀统一算式:
    lethal ⇔ face_det + board_atk ≥ enemy_total − dealt_total − face。"""
    memo: dict = {}                  # memo_key 决策点 -> (后缀伤害, 后续动作)

    def search(st):
        key = memo_key(st)
        hit = memo.get(key)
        if hit is not None:
            return hit
        best = (0, ())               # 叶: 就此停手(平手取更短后缀)
        for i in range(len(st.hand)):
            try:
                nxt = play(st, i, pieces)
            except ValueError:       # 不可支付: 跳过
                continue
            cf, ca = search(nxt)
            # 后缀伤害 = 本打出的伤害 + 子决策点最优后缀
            cand = (nxt.face - st.face + cf, (st.hand[i],) + ca)
            if cand[0] > best[0] or (cand[0] == best[0]
                                     and len(cand[1]) < len(best[1])):
                best = cand
        memo[key] = best
        return best

    face, actions = search(state)

    # 回放动作序列: 生成 mana_trace 与终态(drawn → face_exp)
    st = state
    trace = []
    for key in actions:
        st = play(st, st.hand.index(key), pieces)
        trace.append(st.mana)

    # 期望注记 = 消耗的未知抽牌数 × 单抽期望, 四舍五入取整(绝不进 lethal/total)
    face_exp = int(st.drawn * exp_per_draw + 0.5)

    # uncovered_n: 线中打出过的"效果未覆盖/未知"张数(按 (cid,cost) 去重)。
    # 2026-09-14 审计修正: 卡表缺牌(pieces 无键, 走 inert)也计 —— 缺牌=效果
    # 未知, 诚实计数; 有表但无伤害段的法术照旧计
    seen = set()
    uncovered = 0
    for key in actions:
        if key in seen:
            continue
        seen.add(key)
        p = pieces.get(key)
        if p is None or (p.is_spell and not p.segments):
            uncovered += 1

    total = face + state.board_atk
    need = None if state.enemy_total is None else (
        state.enemy_total - state.dealt_total - state.face)
    return Plan(actions=actions, total=total, face_det=face,
                face_exp=face_exp,
                lethal=(need is not None and total >= need),
                enemy_total=state.enemy_total, uncovered_n=uncovered,
                board_atk=state.board_atk, mana_trace=tuple(trace))
