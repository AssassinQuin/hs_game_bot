"""留牌模拟器(v3.1)—— 训练/CLI 侧: pieces 构建、CRN 2ⁿ 枚举、组合维度
输出、语料曲线提取与 CLI。live 进程零依赖本模块。

铁律(spec §7): pieces 不完备 → 硬失败清单, 该卡组模拟器禁用, 级联降级
(TabPFN/LR/统计表)。与 live 斩杀线的 inert 诚实降级是显式分歧。
"""
from __future__ import annotations

import dataclasses
import json
import random
from itertools import combinations
from pathlib import Path

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


# ---------------- CLI(advise / calibrate) ----------------

def _latest_decklist(corpus: Path, deck: str) -> dict | None:
    """语料该卡组目录最新一样本的 decklist(牌表 0 变化, 任取即可)。"""
    deck_dir = Path(corpus) / deck
    for p in sorted(deck_dir.glob("*.jsonl"), reverse=True):
        meta = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        if meta.get("_meta") and meta.get("decklist"):
            return meta["decklist"]
    return None


def main(argv=None) -> int:
    import argparse
    import sys
    from pathlib import Path

    from hsbot.analysis import EffectAnalyzer
    from hsbot.carddb import CardDB
    from hsbot.config import Config

    argv = list(sys.argv[1:]) if argv is None else list(argv)
    ap = argparse.ArgumentParser(prog="trainer sim",
                                 description="留牌模拟器(v3.1, 0 变化自闭卡组)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--deck", default=None)
    ap.add_argument("--battletag", default=None)
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--orders", type=int, default=200)
    ap.add_argument("--k-max", dest="k_max", type=int, default=12)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_adv = sub.add_parser("advise", help="给定起手 → CRN 模拟出组合维度建议")
    p_adv.add_argument("--hand", required=True, help="逗号分隔的卡 ID")
    p_adv.add_argument("--coin", action="store_true", help="后手")
    p_adv.add_argument("--enemy", type=int, default=30,
                       help="敌方血甲档位(默认 30; 档位表 26/30/40/48/56)")
    sub.add_parser("calibrate", help="三层一致性校准(vs 真实语料)")
    args = ap.parse_args(argv)

    cfg = Config.load({"config": args.config})
    deck = args.deck or cfg.deck_name
    battletag = args.battletag or cfg.battletag
    corpus = Path(args.corpus or cfg.training_dir)
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    analyzer = EffectAnalyzer(carddb)
    out = Path(__file__).resolve().parent / "data" / deck

    decklist = _latest_decklist(corpus, deck)
    if not decklist:
        raise SystemExit(f"语料 {corpus / deck} 无 decklist, 模拟器不适用")
    pieces, cost_of = build_sim_pieces(decklist, carddb, analyzer)
    bad = pieces_completeness(decklist, carddb, pieces, cost_of)
    if bad:
        raise SystemExit("模拟器硬门未过(spec §7, 该卡组禁用):\n  "
                         + "\n  ".join(bad))

    if args.cmd == "advise":
        from .material import load_material
        rows = load_material(out)
        offered = tuple(c.strip() for c in args.hand.split(",") if c.strip())
        rep = sim_mulligan(decklist, offered, pieces=pieces, cost_of=cost_of,
                           coin=args.coin, orders=args.orders,
                           k_max=args.k_max,
                           enemy_totals=ladder_totals(args.enemy,
                                                      k_max=args.k_max),
                           survive=survive_curve(rows, k_max=args.k_max))
        _zh = lambda cid: carddb.name(cid) or cid  # noqa: E731
        print(f"── 模拟留牌建议 ({args.orders} 牌序, CRN {2 ** len(offered)} 集) ──")
        print("留: " + "、".join(_zh(c) for c in rep["keep"]))
        for c, v in sorted(rep["per_card_marginal"].items(),
                           key=lambda kv: -kv[1]):
            print(f"  {_zh(c)} 边际 {v:+.1%}")
        for (a, b), v in rep["pair_synergy"].items():
            if abs(v) >= 0.02:
                tag = "同留" if v > 0 else "不宜同留"
                print(f"  {_zh(a)}+{_zh(b)} {tag} {v:+.1%}")
        if rep["reject"]:
            print("换: " + "、".join(_zh(c) for c in rep["reject"]))
        return 0

    from .simcal import calibrate, print_calibrate_report
    from .material import load_material
    rep = calibrate(corpus / deck, load_material(out), battletag=battletag,
                    carddb=carddb, analyzer=analyzer, orders=args.orders,
                    k_max=args.k_max)
    print_calibrate_report(rep)
    return 0 if rep["pass"] else 1
