"""Piece 编译层 —— (card_id, 实际费) → Piece 效果切片(T1 切片2)。

Piece 是 DFS 可模拟效果的极小投影: 只保留伤害段/回费/法强增益/两类减费/
施法抽牌引擎/法术旗标; 其余 IR(Draw/CostUp/Heal/Armor/Buff/Summon/Mechanic)
一律不进 Piece —— 拍卖师的 Draw(1) 是触发句误标, 忽略之(R6), 抽牌只走
engine + known_draws 通道。

依赖方向约束: planner → hsbot 仅允许 hsbot.consts。IR 效果种类判定用
type 名鸭子判别(不 import hsbot.effects), analyzer 实例经参数传入。
纯函数: 无 IO、无全局态。
"""
from __future__ import annotations

from dataclasses import dataclass

from hsbot.consts import SPELLPOWER_TYPES


@dataclass(frozen=True)
class Piece:
    """单卡可模拟效果切片(frozen, 纯数据)。"""
    card_id: str
    cost: int                    # 调用方给定的实际费(COST 标签优先)
    segments: tuple[int, ...] = ()   # 首个 Damage(scaled=True) 效果: (base,)*hits; 无则 ()
    spell_scaled: bool = False   # cardtype in SPELLPOWER_TYPES(法术/英雄技能吃法强)
    mana_gain: int = 0           # ManaGain.amount 合计
    spellpower_gain: int = 0     # SpellPower.amount 合计; 文本含"选择一"→强制 0(宁漏勿错)
    discount_hand: int = 0       # CostDown scope 以 "hand:" 开头的 amount 合计
    discount_next: int = 0       # CostDown scope 以 "next:" 开头的 amount 合计
    engine: bool = False         # IR 含 Mechanic("cast_draw"): 每施放一法术抽一张
    is_spell: bool = False       # cardtype == "SPELL"


def _inert(card_id: str, cost: int) -> Piece:
    """诚实降级: 缺牌/编译异常 → 全默认 Piece(仅卡号与费)。"""
    return Piece(card_id=card_id, cost=cost)


def build_piece(card_id: str, cost: int, analyzer) -> Piece:
    """编译单卡。IR 取用走 analyzer 的编译缓存(增量, 进程内); carddb.raw
    缺牌/异常 → inert Piece。抉择条件分支(文本含"选择一")的法强增益强制 0:
    分支未定是否生效, 宁漏勿错(可斩只可能漏报不可能误报)。"""
    try:
        ir = analyzer.cache.get_or_compile(analyzer.carddb.raw(card_id))
    except Exception:  # noqa: BLE001  任何编译异常等价于无卡表条目
        return _inert(card_id, cost)
    if ir is None:
        return _inert(card_id, cost)

    segments: tuple[int, ...] = ()
    mana_gain = 0
    spellpower_gain = 0
    discount_hand = 0
    discount_next = 0
    engine = False
    for e in ir.effects:
        # 依赖方向约束: 不 import hsbot.effects, 按 IR 节点类型名判别
        kind = type(e).__name__
        if kind == "Damage":
            # 只认首个 scaled=True 的伤害段(裸数字固定伤目标受限, 不进斩杀口径)
            if not segments and getattr(e, "scaled", False):
                segments = (getattr(e, "base"),) * getattr(e, "hits", 1)
        elif kind == "ManaGain":
            mana_gain += e.amount
        elif kind == "CostDown":
            scope = e.scope or ""
            if scope.startswith("hand:"):
                discount_hand += e.amount
            elif scope.startswith("next:"):
                discount_next += e.amount
        elif kind == "SpellPower":
            spellpower_gain += e.amount
        elif kind == "Mechanic" and e.kind == "cast_draw":
            engine = True
        # 其余 IR(Heal/CostUp/Draw/Armor/Buff/Summon/Mechanic 其它/Unknown)不入 Piece
    if "选择一" in (analyzer.carddb.text(card_id) or ""):
        spellpower_gain = 0          # 抉择分支未定 → 宁漏勿错
    cardtype = ir.cardtype or ""
    return Piece(card_id=card_id, cost=cost, segments=segments,
                 spell_scaled=cardtype in SPELLPOWER_TYPES,
                 mana_gain=mana_gain, spellpower_gain=spellpower_gain,
                 discount_hand=discount_hand, discount_next=discount_next,
                 engine=engine, is_spell=cardtype == "SPELL")
