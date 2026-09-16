"""无损性对拍(一次性探针): HEAD 旧 rollout vs 新实现(等效不截断) 在当前
语料上逐推演比对 launch_turn —— 全等则证明前置过滤对旧语义严格无损,
4/425 与昨日 ~10% 的差异纯属语料增长(93 局/血甲曲线变化), 非代码回归。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import planner.rollout as R          # 新实现
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

_launches: list = []
_orig_new = simcal.rollout


def _recording(fn):
    def wrap(*a, **kw):
        r = fn(*a, **kw)
        _launches.append(r.launch_turn)
        return r
    return wrap


# 新实现, cap 放到等效不截断
R.LAUNCH_DRAWS_CAP = 10 ** 9
simcal.rollout = _recording(_orig_new)
t0 = time.perf_counter()
simcal.calibrate(corpus, rows, battletag=cfg.battletag, carddb=carddb,
                 analyzer=analyzer, orders=5, k_max=12, seed=0)
t_new = time.perf_counter() - t0
new = list(_launches)
simcal.rollout = _orig_new

# HEAD 旧实现(无过滤/无截断)
sys.path.insert(0, ".")
import importlib
old_mod = importlib.import_module("tmp_old.rollout")
_launches.clear()
simcal.rollout = _recording(old_mod.rollout)
t0 = time.perf_counter()
simcal.calibrate(corpus, rows, battletag=cfg.battletag, carddb=carddb,
                 analyzer=analyzer, orders=5, k_max=12, seed=0)
t_old = time.perf_counter() - t0
old = list(_launches)
simcal.rollout = _orig_new

print(f"new(uncapped): {t_new:.1f}s, launches={sum(x is not None for x in new)}/{len(new)}")
print(f"HEAD old     : {t_old:.1f}s, launches={sum(x is not None for x in old)}/{len(old)}")
diff = [(i, x, y) for i, (x, y) in enumerate(zip(new, old)) if x != y]
print(f"逐推演差异: {len(diff)}")
for i, x, y in diff[:20]:
    print(f"  rollout#{i}: new={x} old={y}")
print("判定: " + ("严格无损 ✓" if not diff and len(new) == len(old) else "存在差异 ✗"))
