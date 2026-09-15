"""三层一致性校准 —— 模拟器 vs 真实语料(v3.1 设计 §6)。

合成→合成的自洽只证明模拟器无 bug; 合成→真实的三层一致才证明无偏:
轨迹层(手牌规模曲线)/启动层(P(启动≤K) 曲线)/结果层(启动→胜转化)。
任一层失败 → pass=False, CLI 退出码非零, 模拟器对该卡组禁用。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from planner.dfs import best_line
from planner.pieces import Piece
from planner.rollout import rollout
from planner.simstate import initial_state

from .sim import (build_sim_pieces, enemy_hp_curve, pieces_completeness,
                  survive_curve)

TRAJ_MAX_MEDIAN_HAND_DIFF = 1.5    # 轨迹层: 手牌规模中位差上限(张)
LAUNCH_MAX_ABS_DIFF = 0.20         # 启动层: P(启动≤K) 逐点绝对差上限
RESULT_MIN_WIN_RATE = 0.60         # 结果层: 启动局胜率下限(≈1 预期, 宽容小样本)


def my_mulligan(meta: dict, battletag: str) -> tuple[tuple, tuple, bool]:
    """corpus _meta → (offered, kept, coin)。coin = offered 4 张(v1 口径,
    MULLIGAN_AI"幸运币不参与留牌"的推断; 校准容差吸收误差)。"""
    for pid, p in meta.get("players", {}).items():
        if p.get("name") == battletag:
            m = meta.get("mulligan", {}).get(pid) or {}
            offered = tuple(m.get("offered") or [])
            return offered, tuple(m.get("kept") or []), len(offered) == 4
    return (), (), False


def _game_key(row: dict) -> str:
    """行 → 局键: src 的 '#' 前缀去掉 '.power.log' —— 与 meta 的
    f"{session}_g{idx:02d}" 同域(两侧必须共用本函数, 否则键失配)。"""
    return row["src"].split("#")[0].removesuffix(".power.log")


def _piece_of(pieces: dict, cid: str, cost_of: dict) -> Piece:
    cost = cost_of.get(cid, 0)
    return pieces.get((cid, cost)) or Piece(card_id=cid, cost=cost)


def first_lethal_turns(rows: list, pieces: dict, cost_of: dict) -> dict:
    """每局首个 best_line 可斩的我方回合(启动层真实侧)。

    快照缺费的手牌宁漏勿错跳过; 返回 {局键: 回合}, 未启动局缺省由调用方
    以 None 补全。局键 = _game_key(row)。"""
    out: dict = {}
    for r in rows:
        snap = r.get("snap") or {}
        if not snap.get("my_turn"):
            continue
        g = _game_key(r)
        if g in out:
            continue
        me, opp = snap["me"], snap["opp"]
        hand = tuple((c["cid"], c["cost"]) for c in me["hand"]
                     if c["cost"] is not None)
        engines = sum(1 for b in me["board"]
                      if _piece_of(pieces, b["cid"], cost_of).engine)
        st = initial_state(me["mana"], hand, sp=me.get("spellpower", 0),
                           engines=engines)
        if best_line(st, pieces,
                     enemy_total=opp["hp"] + opp["armor"]).lethal:
            out[g] = snap["turn"]
    return out


def calibrate(deck_dir, rows: list, *, battletag: str, carddb, analyzer,
              orders: int = 200, k_max: int = 8, seed: int = 0) -> dict:
    metas = []
    for p in sorted(Path(deck_dir).glob("*.jsonl")):
        meta = json.loads(
            p.read_text(encoding="utf-8").splitlines()[0])
        if meta.get("_meta"):
            metas.append(meta)
    decklist = next((m.get("decklist") for m in metas if m.get("decklist")),
                    None)
    if not decklist:
        return {"pass": False, "reason": "语料无 decklist, 无法构建牌表",
                "layers": {}}
    pieces, cost_of = build_sim_pieces(decklist, carddb, analyzer)
    bad = pieces_completeness(decklist, carddb, pieces, cost_of)
    if bad:
        return {"pass": False, "reason": "pieces 不完备(硬门, spec §7)",
                "layers": {}, "detail": bad}

    ehp = enemy_hp_curve(rows, k_max=k_max)
    surv = survive_curve(rows, k_max=k_max)
    full = [cid for cid, n in decklist.items() for _ in range(n)]

    # 真实侧: 每局手牌规模曲线 / 首可斩回合 / 胜负
    real_hs: dict = {}
    result_of: dict = {}
    for r in rows:
        g = _game_key(r)
        snap = r.get("snap") or {}
        if snap.get("my_turn"):
            real_hs.setdefault(g, {})[snap["turn"]] = len(snap["me"]["hand"])
        if "result" in r and g not in result_of:
            result_of[g] = r["result"]
    real_launch = first_lethal_turns(rows, pieces, cost_of)
    games = sorted(real_hs)

    # 模拟侧: 每局用真实 keep + 逐局种子推演一次
    diffs: list[float] = []
    sim_launch: list[int | None] = []
    n_sim = 0
    for i, m in enumerate(metas):
        offered, kept, coin = my_mulligan(m, battletag)
        if not offered:
            continue
        rng = random.Random(seed * 1000003 + i)
        res = rollout(tuple(rng.sample(full, len(full))), offered=offered,
                      keep=kept, coin=coin, pieces=pieces, cost_of=cost_of,
                      k_max=k_max, enemy_totals=ehp)
        n_sim += 1
        sim_launch.append(res.launch_turn)
        g = f"{m['session']}_g{m['game_index']:02d}"
        for t, hs in enumerate(res.hand_sizes, 1):
            if t in real_hs.get(g, {}):
                diffs.append(abs(hs - real_hs[g][t]))

    def _median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0.0

    # 轨迹层
    traj_med = _median(diffs)
    traj_ok = traj_med <= TRAJ_MAX_MEDIAN_HAND_DIFF
    # 启动层: P(启动≤K) 双侧逐点差
    launch_diffs = []
    for K in range(1, k_max + 1):
        real_p = (sum(1 for g in games if real_launch.get(g, k_max + 1) <= K)
                  / len(games)) if games else 0.0
        sim_p = (sum(1 for x in sim_launch if x is not None and x <= K)
                 / n_sim) if n_sim else 0.0
        launch_diffs.append(abs(real_p - sim_p))
    launch_max = max(launch_diffs) if launch_diffs else 0.0
    launch_ok = launch_max <= LAUNCH_MAX_ABS_DIFF
    # 结果层: 真实启动局的胜率
    launched_games = [g for g in real_launch if g in result_of]
    win_rate = (sum(result_of[g] for g in launched_games) / len(launched_games)
                ) if launched_games else 1.0
    result_ok = win_rate >= RESULT_MIN_WIN_RATE

    layers = {
        "trajectory": {"ok": traj_ok, "median_hand_diff": traj_med,
                       "n_points": len(diffs)},
        "launch": {"ok": launch_ok, "max_abs_diff": launch_max,
                   "curve_diffs": launch_diffs},
        "result": {"ok": result_ok, "win_rate": win_rate,
                   "n_launched": len(launched_games)},
    }
    return {"pass": all(v["ok"] for v in layers.values()),
            "games": len(games), "sim_games": n_sim, "layers": layers}


def print_calibrate_report(rep: dict) -> None:
    if "reason" in rep:
        print(f"✗ 校准未运行: {rep['reason']}")
        if rep.get("detail"):
            print("  " + " │ ".join(rep["detail"][:10]))
        return
    lj = {k: ("通过" if v["ok"] else "未过") for k, v in rep["layers"].items()}
    tr = rep["layers"]["trajectory"]
    la = rep["layers"]["launch"]
    re = rep["layers"]["result"]
    print(f"── 三层校准: {'PASS' if rep['pass'] else 'FAIL'} "
          f"({rep['games']} 真实局 / {rep['sim_games']} 模拟局) ──")
    print(f"  轨迹层[{lj['trajectory']}] 手牌规模中位差 "
          f"{tr['median_hand_diff']:.2f} (≤{TRAJ_MAX_MEDIAN_HAND_DIFF}, "
          f"{tr['n_points']} 点)")
    print(f"  启动层[{lj['launch']}] P(启动≤K) 最大绝对差 "
          f"{la['max_abs_diff']:.3f} (≤{LAUNCH_MAX_ABS_DIFF})")
    print(f"  结果层[{lj['result']}] 启动局胜率 {re['win_rate']:.2f} "
          f"(≥{RESULT_MIN_WIN_RATE}, {re['n_launched']} 局)")
