"""SimState 不可变状态与 play 转移 —— DFS 的记忆化 key 与出牌语义(T1 切片2)。

全 tuple/frozen: 状态可直接哈希, 与 GameStore 完全解耦。play 是纯函数:
不修改入参, 返回新状态; 不可支付 raise ValueError 由调用方捕获。
"""
from __future__ import annotations

from bisect import insort
from dataclasses import dataclass

from .pieces import Piece


@dataclass(frozen=True)
class SimState:
    """决策点快照(全 tuple/frozen, 可哈希; dfs.memo 以去 face/drawn 的七元组
    为键, 见 planner/dfs.py 模块注释的后缀不变性)。"""
    mana: int
    hand: tuple                  # ((card_id, cost), ...) 升序排序规范化
    sp: int                      # 当前法强
    disc_hand: int               # 在身的手牌法术减费余额(每张法术都减, 不耗尽)
    disc_next: int               # "下一张"减费面值(一次性: 全额作用下一张, 用后清零)
    disc_next_cat: str = ""      # 该余额的类目词(星灵/法术/…; 空=不限类目):
                                 # 类目不符或有剩余费的牌才作用并消耗
                                 # (2026-09-14 用户裁决"0 水晶不需要")
    engines: int = 0             # 在场施法抽牌引擎数
    drawn: int = 0               # 已消耗的未知抽牌数(Plan.face_exp 期望折算用)
    known_draws: tuple = ()      # ((cid, cost), ...) 队头=最先抽到(消耗式)
    face: int = 0                # 已累计确定伤害


def initial_state(mana: int, hand_cards, sp: int, known_draws=(), disc_hand: int = 0,
                  disc_next: int = 0, engines: int = 0,
                  disc_next_cat: str = "") -> SimState:
    """构造初始状态: hand_cards=((cid, cost), ...), 手牌升序排序规范化;
    known_draws 保持调用方给的队列序(队头=最先抽到, 不排序)。"""
    return SimState(mana=mana, hand=tuple(sorted(hand_cards)), sp=sp,
                    disc_hand=disc_hand, disc_next=disc_next,
                    disc_next_cat=disc_next_cat, engines=engines,
                    drawn=0, known_draws=tuple(known_draws), face=0)


def play(state: SimState, index: int, pieces: dict) -> SimState:
    """打出 state.hand[index] 并返回新状态(纯函数)。

    语义(契约口径, 2026-09-14 审计修正 disc_next):
    - 实付费 eff = max(0, 费 − (disc_hand 若法术) − disc_next);
      eff > mana → raise ValueError(不可支付)。
    - mana' = min(10, mana − eff + mana_gain)。
    - face' = face + Σ((b+sp) 若 spell_scaled 否则 b for b in segments) ——
      法强逐段结算 (base+sp)×hits。
    - disc_next 是一次性面值, 且 2026-09-14 用户裁决"0 水晶不需要":
      原费(扣手牌减费后)已 0 的牌不得作用也不得消耗余额; 类目不符
      (如 水晶塔 next:星灵 打在非星灵牌上)同样保留余额等下一张 ——
      唯有两种其一相符才全额作用并清零(溢出浪费), 打出的减费牌自身
      的 next 余额覆盖旧余额; disc_hand 只减法术且不耗尽。
    - 抽牌: p.is_spell 且打牌前 engines>0 → 每个在场引擎各自触发一次(循环
      engines 次): known_draws 非空则队头入手并弹出, 否则 drawn+1;
      hand' 永远排序规范化。
    - 独立抽牌: p.draw_n>0 时逐张抽(同样 known 队头优先); build_piece 恒不填,
      live 路径零影响。
    - engines' = engines + (1 若本牌是引擎) —— 触发判定用打牌前的 engines。
    """
    key = state.hand[index]
    # pieces 缺键 → inert Piece(仅费), 诚实降级
    p = pieces.get(key)
    if p is None:
        p = Piece(card_id=key[0], cost=key[1])
    eff = p.cost
    if p.is_spell and state.disc_hand:
        eff -= state.disc_hand
        if eff < 0:
            eff = 0
    # 下一张减费: 有剩余费且(不限类目 或 类目相符)才作用并全额消耗;
    # 0 费牌/类目不符 → 余额原样保留(不作用不消耗)
    disc_next, disc_next_cat = state.disc_next, state.disc_next_cat
    if disc_next:
        if (eff > 0 and (not disc_next_cat
                         or disc_next_cat in p.card_cats)):
            eff = max(0, eff - disc_next)
            disc_next, disc_next_cat = 0, ""
    if eff > state.mana:
        raise ValueError(f"不可支付: {key[0]} 需 {eff} 费, 仅剩 {state.mana}")

    rest = state.hand[:index] + state.hand[index + 1:]   # 已是升序(子序列)
    known = state.known_draws
    drawn = state.drawn
    hand_list = None                      # 惰性 list 化: 只有入手牌才重排

    def _draw_one() -> None:              # 已知牌队头入手, 未知只计数
        nonlocal known, drawn, hand_list   # (rollout 的抽样 = known 队列喂入)
        if known:
            if hand_list is None:
                hand_list = list(rest)
            insort(hand_list, known[0])   # 队头入手, 二分插入保持升序
            known = known[1:]             # 弹出
        else:
            drawn += 1

    if p.is_spell and state.engines > 0:  # 引擎触发只看打牌前的 engines
        for _ in range(state.engines):    # 每个在场引擎各自触发一次
            _draw_one()                   # (双拍卖师施一法术抽两张)
    for _ in range(p.draw_n):             # 独立抽牌(rollout 专用; 恒 0 即无操作)
        _draw_one()
    hand = tuple(hand_list) if hand_list is not None else rest
    # 热路径(dfs 每条边一次): 绕开 frozen __init__ 的逐字段 object.__setattr__,
    # 一次 C 级填充等价构造; 事后外部赋值仍被 frozen_setattr 拦截, 不可变性不变。
    if p.discount_next:                       # 打出的减费牌覆盖旧余额
        disc_next, disc_next_cat = p.discount_next, p.next_cat
    st = SimState.__new__(SimState)
    st.__dict__.update(mana=min(10, state.mana - eff + p.mana_gain), hand=hand,
                       sp=state.sp + p.spellpower_gain,
                       disc_hand=state.disc_hand + p.discount_hand,
                       disc_next=disc_next, disc_next_cat=disc_next_cat,
                       engines=state.engines + (1 if p.engine else 0),
                       drawn=drawn, known_draws=known,
                       face=state.face + sum((b + state.sp) if p.spell_scaled
                                             else b for b in p.segments))
    return st
