"""留牌模拟器(v3.1)—— 训练/CLI 侧: pieces 构建、CRN 2ⁿ 枚举、组合维度
输出、语料曲线提取与 CLI。live 进程零依赖本模块。

铁律(spec §7): pieces 不完备 → 硬失败清单, 该卡组模拟器禁用, 级联降级
(TabPFN/LR/统计表)。与 live 斩杀线的 inert 诚实降级是显式分歧。
"""
from __future__ import annotations

import dataclasses

from planner.pieces import Piece, build_piece

COIN_CID = "COIN"


def _draw_n_from_ir(carddb, analyzer, cid: str) -> int:
    """非触发的独立抽牌数; 文本含触发/条件标记一律 0(宁漏勿错)。"""
    text = carddb.text(cid) or ""
    if "每当" in text or "如果" in text:
        return 0
    ir = analyzer.cache.get_or_compile(carddb.raw(cid))
    if ir is None:
        return 0
    n = 0
    for e in ir.effects:
        if type(e).__name__ == "Draw" and getattr(e, "scope", "") != "opponent":
            n += getattr(e, "amount", 1)   # 对手抽牌(自然平衡族)不计我方
    return n


def build_sim_pieces(decklist: dict, carddb, analyzer) -> tuple[dict, dict]:
    """decklist{cid:n} → (pieces, cost_of)。

    build_piece 刻意忽略 Draw(触发句误标); 此处对非触发抽牌后填 draw_n,
    并注入幸运币(0 费回费法术 —— 引擎在场施放可触发抽牌, 忠实游戏规则)。
    """
    pieces: dict = {}
    cost_of: dict = {}
    for cid in decklist:
        cost = carddb.cost(cid) or 0
        cost_of[cid] = cost
        piece = build_piece(cid, cost, analyzer)
        draws = _draw_n_from_ir(carddb, analyzer, cid)
        if draws:
            piece = dataclasses.replace(piece, draw_n=draws)
        pieces[(cid, cost)] = piece
    cost_of[COIN_CID] = 0
    pieces[(COIN_CID, 0)] = Piece(card_id=COIN_CID, cost=0, mana_gain=1,
                                  is_spell=True)
    return pieces, cost_of


def pieces_completeness(decklist: dict, carddb, pieces: dict,
                        cost_of: dict) -> list[str]:
    """模拟器硬门: 卡表缺牌/pieces 缺键 → 清单非空即禁用(spec §7)。"""
    bad = []
    for cid in decklist:
        if carddb.raw(cid) is None:
            bad.append(f"{cid}: 卡表缺牌")
        elif (cid, cost_of.get(cid)) not in pieces:
            bad.append(f"{cid}: pieces 缺键")
    return bad
