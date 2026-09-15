"""留牌模拟器(v3.1)—— 训练/CLI 侧: pieces 构建、CRN 2ⁿ 枚举、组合维度
输出、语料曲线提取与 CLI。live 进程零依赖本模块。

铁律(spec §7): pieces 不完备 → 硬失败清单, 该卡组模拟器禁用, 级联降级
(TabPFN/LR/统计表)。与 live 斩杀线的 inert 诚实降级是显式分歧。
"""
from __future__ import annotations

import dataclasses
import random
from itertools import combinations

from planner.pieces import Piece, build_piece
from planner.rollout import rollout

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


# ---------------- CRN 2ⁿ 枚举 + 组合维度输出 ----------------

ANTI_SYNERGY_THRESHOLD = -0.02      # spec §5.3


def all_keep_sets(offered) -> list[frozenset]:
    """2ⁿ 候选留集(n≤5, ≤32), 含空集与全集。"""
    out: list[frozenset] = []
    for r in range(len(offered) + 1):
        out.extend(frozenset(c) for c in combinations(offered, r))
    return out


def sim_mulligan(decklist: dict, offered, *, pieces: dict, cost_of: dict,
                 coin: bool = False, orders: int = 200, k_max: int = 8,
                 enemy_totals, survive, seed: int = 0) -> dict:
    """CRN 蒙特卡洛: 每个抽样牌序枚举全部 keep 集(同随机源, 差值低方差)。

    score(S) = Σ_t P(启动=t│S) · survive(t) · P(胜│启动=1.0 常数 v1)
    (结果层校准验证 ≈1 假设, 见 simcal)。返回见模块 Interfaces。
    """
    full = [cid for cid, n in decklist.items() for _ in range(n)]
    missing = [c for c in offered if c not in decklist]
    if missing:
        raise ValueError(f"offered 不在牌表中: {missing}")
    sets = all_keep_sets(offered)
    hist = {s: [0] * (k_max + 1) for s in sets}    # [0] 弃用; 1..k_max 计启动回合
    rng = random.Random(seed)
    for _ in range(orders):
        order = tuple(rng.sample(full, len(full)))
        for s in sets:
            r = rollout(order, offered=offered, keep=tuple(sorted(s)),
                        coin=coin, pieces=pieces, cost_of=cost_of,
                        k_max=k_max, enemy_totals=enemy_totals)
            if r.launch_turn is not None:
                hist[s][r.launch_turn] += 1
    scores = {s: sum(hist[s][t] / orders * survive[t - 1]
                     for t in range(1, k_max + 1) if hist[s][t])
              for s in sets}
    return {"scores": scores, "orders": orders, "k_max": k_max,
            "launch_hist": hist, **combo_outputs(scores, offered)}


def combo_outputs(scores: dict, offered) -> dict:
    """组合维度输出契约(spec §5.3, 全部由 2ⁿ 打分表组合而来)。"""
    best = max(scores, key=lambda s: scores[s])
    base = scores[frozenset()]
    per_card_marginal = {c: scores[best] - scores[best - frozenset((c,))]
                         for c in sorted(best)}
    pair_synergy: dict = {}
    anti_synergy: list = []
    for a, b in combinations(sorted(offered), 2):
        syn = (scores.get(frozenset((a, b)), 0.0)
               - scores.get(frozenset((a,)), 0.0)
               - scores.get(frozenset((b,)), 0.0) + base)
        pair_synergy[(a, b)] = syn
        if syn < ANTI_SYNERGY_THRESHOLD:
            anti_synergy.append((a, b))
    reject = [c for c in sorted(offered)
              if c not in best and scores[frozenset((c,))] - base < 0]
    return {"keep": tuple(sorted(best)),
            "per_card_marginal": per_card_marginal,
            "pair_synergy": pair_synergy,
            "anti_synergy": anti_synergy, "reject": reject}


# ---------------- 语料曲线提取 + 血甲档位表 ----------------

ENEMY_LADDER = (26, 30, 40, 48, 56)   # 血甲档位表(2026-09-15 用户增补):
                                      # 产品侧(advise)逐档判定, 校准侧用真实血甲


def ladder_totals(rung: int, k_max: int = 8) -> list:
    """全回合恒定档位 —— rollout 的 enemy_totals 直填。"""
    return [rung] * k_max


def enemy_hp_curve(rows: list, k_max: int = 8,
                   quantile: float = 0.5) -> list:
    """回合 → 敌方有效血甲(hp+armor)经验分位; 无样本回合 None。"""
    by_turn: dict = {}
    for r in rows:
        snap = r.get("snap") or {}
        if snap.get("my_turn"):
            t = snap["turn"]
            by_turn.setdefault(t, []).append(
                snap["opp"]["hp"] + snap["opp"]["armor"])
    out = []
    for t in range(1, k_max + 1):
        v = by_turn.get(t)
        out.append(None if not v
                   else sorted(v)[min(len(v) - 1, int(quantile * len(v)))])
    return out


def survive_curve(rows: list, k_max: int = 8) -> list:
    """P(存活至我的第 K 回合) ≈ 有该回合行的局占比(游戏时长代理)。"""
    max_turn: dict = {}
    for r in rows:
        snap = r.get("snap") or {}
        if snap.get("my_turn"):
            g = r["src"].split("#")[0]
            max_turn[g] = max(max_turn.get(g, 0), snap["turn"])
    n = len(max_turn) or 1
    return [sum(1 for mx in max_turn.values() if mx >= t) / n
            for t in range(1, k_max + 1)]
