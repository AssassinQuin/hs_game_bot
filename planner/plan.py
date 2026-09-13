"""Plan 输出结构 —— 推理层只回结构, 措辞归 render(T1 切片3)。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    """一条出牌线的全部机读事实。"""
    actions: tuple               # ((card_id, cost), ...) 出牌顺序
    total: int                   # face_det + board_atk(期望分量不进)
    face_det: int                # 确定伤(不含期望)
    face_exp: int                # 期望注记 = drawn_used * exp_per_draw 取整; 不进 lethal/total
    lethal: bool                 # enemy_total 不为 None 且 face_det + board_atk >= enemy_total
    enemy_total: int | None
    uncovered_n: int             # 线中打出过的 is_spell 且 segments==() 的张数(去重计)
    board_atk: int = 0
    mana_trace: tuple = ()       # 每步打出后的剩余 mana
