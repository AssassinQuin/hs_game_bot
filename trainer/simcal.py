"""三层一致性校准 —— 模拟器 vs 真实语料(v3.1 设计 §6)。

合成→合成的自洽只证明模拟器无 bug; 合成→真实的三层一致才证明无偏:
轨迹层(手牌规模曲线)/启动层(P(启动≤K) 曲线)/结果层(启动→胜转化)。
任一层失败 → pass=False, CLI 退出码非零, 模拟器对该卡组禁用。
"""
from __future__ import annotations

import json
import random
import time
from collections import Counter
from pathlib import Path

from planner.dfs import best_line
from planner.pieces import Piece
from planner.rollout import rollout
from planner.simstate import initial_state

from .sim import build_sim_pieces, enemy_hp_curve, pieces_completeness

TRAJ_MAX_MEDIAN_HAND_DIFF = 1.5    # 轨迹层: 手牌规模中位差上限(张)
LAUNCH_MAX_ABS_DIFF = 0.20         # 启动层: P(启动≤K) 逐点绝对差上限
RESULT_MIN_WIN_RATE = 0.60         # 结果层: 启动局胜率下限(≈1 预期, 宽容小样本)

# 硬币 card_id 容错集合: 2026-09-15 真实语料实测币混在 offered 里,
# cid 为 MUDAN_COIN1(本季皮肤币); offered 长度实测 {3:先手, 5:后手},
# 从无 4 —— v1 的 len==4 判币是死代码(审计 2026-09-15 修复波 D1)。
_COIN_CIDS = ("MUDAN_COIN1", "COIN", "GAME_005")


def my_mulligan(meta: dict, battletag: str) -> tuple[tuple, tuple, bool]:
    """corpus _meta → (offered, kept, coin), 币已剔出 offered/kept。

    空串洞(cid 解析失败的洞, 实测 1/83 局)一并过滤。"""
    for pid, p in meta.get("players", {}).items():
        if p.get("name") == battletag:
            m = meta.get("mulligan", {}).get(pid) or {}
            offered = tuple(c for c in (m.get("offered") or []) if c)
            kept = tuple(c for c in (m.get("kept") or []) if c)
            coin = any(c in _COIN_CIDS for c in offered)
            return (tuple(c for c in offered if c not in _COIN_CIDS),
                    tuple(c for c in kept if c not in _COIN_CIDS), coin)
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
                           engines=engines, enemy_total=opp["hp"] + opp["armor"])
        if best_line(st, pieces).lethal:
            out[g] = snap["turn"]
    return out


def calibrate(deck_dir, rows: list, *, battletag: str, carddb, analyzer,
              orders: int = 200, k_max: int = 12, seed: int = 0) -> dict:
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

    # D1 数据契约: 先过 meta —— 剔币/空串后, offered/kept 任一 cid 不在
    # decklist(实测 8/83 局换牌时段玩了别的卡组, 导出器按目录盖章了
    # decklist)→ 该局跳过且**双侧**排除(真实侧 rows 按局键同步过滤)。
    skip: Counter = Counter()
    offdeck: set = set()
    sims: list = []            # (meta 序, offered, kept, coin, 局键)
    for i, m in enumerate(metas):
        offered, kept, coin = my_mulligan(m, battletag)
        if not offered:
            continue
        g = f"{m['session']}_g{m['game_index']:02d}"
        if any(c not in decklist for c in (*offered, *kept)):
            skip["异局(deck外卡)"] += 1
            offdeck.add(g)
            continue
        sims.append((i, offered, kept, coin, g))
    rows = [r for r in rows if _game_key(r) not in offdeck]
    print(f"[cal] 配置: {len(metas)} meta, 可模拟 {len(sims)} 局, "
          f"orders={orders}, k_max={k_max}, seed={seed}, "
          f"跳过={dict(skip) or '无'}", flush=True)

    ehp = enemy_hp_curve(rows, k_max=k_max)
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
    if real_launch:
        heads = ", ".join(f"T{t}" for _g, t in
                          sorted(real_launch.items(), key=lambda kv: kv[1]))
        print(f"[cal] 真实侧: {len(games)} 局, 首可斩 {len(real_launch)} 局 "
              f"@ {heads}", flush=True)
    else:
        print(f"[cal] 真实侧: {len(games)} 局, 首可斩 0 局"
              "(启动/结果层将空洞)", flush=True)

    # 模拟侧: 每局用真实 keep 推演 orders 次(orders 真旋钮: 每局启动率的
    # 样本量), 轨迹层聚合全部推演在公共回合的手牌规模差值
    diffs: list[float] = []
    sim_launch: list[int | None] = []
    n_sim = 0
    t_all = time.perf_counter()
    step = max(1, orders // 10)           # 推演进度上报间隔(orders=5 → 每次都报)
    for i, offered, kept, coin, g in sims:
        n_sim += 1
        t_g = time.perf_counter()
        rng = random.Random(seed * 1000003 + i)
        launched = 0
        for j in range(orders):
            res = rollout(tuple(rng.sample(full, len(full))), offered=offered,
                          keep=kept, coin=coin, pieces=pieces, cost_of=cost_of,
                          k_max=k_max, enemy_totals=ehp)
            sim_launch.append(res.launch_turn)
            launched += res.launch_turn is not None
            for t, hs in enumerate(res.hand_sizes, 1):
                if t in real_hs.get(g, {}):
                    diffs.append(abs(hs - real_hs[g][t]))
            if (j + 1) % step == 0 or j + 1 == orders:
                print(f"[cal {n_sim}/{len(sims)}] {g} keep="
                      f"{'+'.join(sorted(kept)) or '全换'} "
                      f"推演 {j + 1}/{orders} 启动 {launched} "
                      f"({time.perf_counter() - t_g:.1f}s)", flush=True)
    elapsed = time.perf_counter() - t_all

    if not games or not n_sim or not diffs:
        return {"pass": False, "reason": "无配对样本(语料/键域/回合交集为空)",
                "layers": {}, "games": len(games), "sim_games": n_sim,
                "skip": dict(skip)}

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
                 / len(sim_launch)) if sim_launch else 0.0
        launch_diffs.append(abs(real_p - sim_p))
    launch_max = max(launch_diffs) if launch_diffs else 0.0
    launch_ok = launch_max <= LAUNCH_MAX_ABS_DIFF
    # 结果层: 真实启动局的胜率
    launched_games = [g for g in real_launch if g in result_of]
    win_rate = (sum(result_of[g] for g in launched_games) / len(launched_games)
                ) if launched_games else 1.0
    result_ok = win_rate >= RESULT_MIN_WIN_RATE

    # D3 空洞显性化: 双侧 0 启动 → 该层无检验力, 不提供正向证据也不否决
    launch_vacuous = (not real_launch
                      and not any(x is not None for x in sim_launch))
    result_vacuous = not launched_games

    layers = {
        "trajectory": {"ok": traj_ok, "median_hand_diff": traj_med,
                       "n_points": len(diffs)},
        "launch": {"ok": launch_ok, "vacuous": launch_vacuous,
                   "max_abs_diff": launch_max, "curve_diffs": launch_diffs},
        "result": {"ok": result_ok, "vacuous": result_vacuous,
                   "win_rate": win_rate, "n_launched": len(launched_games)},
    }
    return {"pass": traj_ok and (launch_ok or launch_vacuous)
            and (result_ok or result_vacuous),
            "games": len(games), "sim_games": n_sim, "layers": layers,
            "skip": dict(skip), "elapsed_s": round(elapsed, 1)}


def print_calibrate_report(rep: dict) -> None:
    if "reason" in rep:
        print(f"✗ 校准未运行: {rep['reason']}")
        if rep.get("detail"):
            print("  " + " │ ".join(rep["detail"][:10]))
        if rep.get("skip"):
            print("跳过: " + " │ ".join(f"{k} {v}" for k, v in
                                         rep["skip"].items()))
        return
    lj = {k: ("空洞" if v.get("vacuous") else
              ("通过" if v["ok"] else "未过"))
          for k, v in rep["layers"].items()}
    eff = 1 + sum(1 for k in ("launch", "result")
                  if not rep["layers"][k].get("vacuous"))
    tr = rep["layers"]["trajectory"]
    la = rep["layers"]["launch"]
    re = rep["layers"]["result"]
    print(f"── 三层校准: {'PASS' if rep['pass'] else 'FAIL'} "
          f"({rep['games']} 真实局 / {rep['sim_games']} 模拟局, "
          f"{eff}/3 层有效, 模拟侧 {rep.get('elapsed_s', '?')}s) ──")
    print(f"  轨迹层[{lj['trajectory']}] 手牌规模中位差 "
          f"{tr['median_hand_diff']:.2f} (≤{TRAJ_MAX_MEDIAN_HAND_DIFF}, "
          f"{tr['n_points']} 点)")
    vac = ", 双侧0启动, 无检验力" if la.get("vacuous") else ""
    print(f"  启动层[{lj['launch']}] P(启动≤K) 最大绝对差 "
          f"{la['max_abs_diff']:.3f} (≤{LAUNCH_MAX_ABS_DIFF}{vac})")
    vac_r = ", 双侧0启动, 无检验力" if re.get("vacuous") else ""
    wr = f"{re['win_rate']:.2f}" if re["n_launched"] else "n/a"
    print(f"  结果层[{lj['result']}] 启动局胜率 {wr} "
          f"(≥{RESULT_MIN_WIN_RATE}, {re['n_launched']} 局{vac_r})")
    if rep.get("skip"):
        print("跳过: " + " │ ".join(f"{k} {v}" for k, v in rep["skip"].items()))
