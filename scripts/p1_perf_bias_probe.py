"""P1 性能测量 + cap 偏差量化(一次性探针, 不入测试)。

跑两遍 calibrate(orders/k_max/seed 与 2026-09-15 实测口径一致):
  A) 默认 LAUNCH_DRAWS_CAP=12 —— 5 档耗时(P1 验收: <10 分钟)
  B) cap 放大到 10**9(等效不截断) —— 与 A 逐推演比对 launch_turn,
     差异数 = cap 截断的漏报启动数(偏差量化, 方向恒保守)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import planner.rollout as R
import trainer.simcal as simcal
from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB
from hsbot.config import Config
from trainer.material import load_material

cfg = Config.load({"config": "config.yaml"})
deck = cfg.deck_name
carddb = CardDB(cfg.cache_dir / "cards.zh.json")
analyzer = EffectAnalyzer(carddb)
rows = load_material(Path("trainer/data") / deck)
corpus = Path(cfg.training_dir) / deck

launches: list = []
_orig = simcal.rollout


def recording(*a, **kw):
    r = _orig(*a, **kw)
    launches.append(r.launch_turn)
    return r


def run(tag: str) -> dict:
    simcal.rollout = recording
    launches.clear()
    t0 = time.perf_counter()
    rep = simcal.calibrate(corpus, rows, battletag=cfg.battletag,
                           carddb=carddb, analyzer=analyzer,
                           orders=5, k_max=12, seed=0)
    simcal.rollout = _orig
    el = round(time.perf_counter() - t0, 1)
    out = {"tag": tag, "elapsed_s": el, "n_rollouts": len(launches),
           "n_launch": sum(x is not None for x in launches),
           "pass": rep.get("pass"), "layers": {
               k: {kk: vv for kk, vv in v.items() if kk != "curve_diffs"}
               for k, v in (rep.get("layers") or {}).items()},
           "sim_games": rep.get("sim_games"), "games": rep.get("games")}
    out["_launches"] = list(launches)
    return out


a = run("capped=12")
print("\n== A(默认 cap=12) ==")
print(json.dumps({k: v for k, v in a.items() if k != "_launches"},
                 ensure_ascii=False), flush=True)

R.LAUNCH_DRAWS_CAP = 10 ** 9
b = run("uncapped")
print("\n== B(cap=∞) ==")
print(json.dumps({k: v for k, v in b.items() if k != "_launches"},
                 ensure_ascii=False), flush=True)

diff = [(i, x, y) for i, (x, y) in
        enumerate(zip(a["_launches"], b["_launches"])) if x != y]
print(f"\n== 偏差量化: {len(diff)}/{len(a['_launches'])} 推演 launch_turn 不等 ==")
for i, x, y in diff[:20]:
    print(f"  rollout#{i}: cap={x} uncapped={y}")
