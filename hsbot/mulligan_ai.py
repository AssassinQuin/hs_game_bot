"""留牌推理层 —— 模型产物的加载与同步推理(训练器与实时军师共用)。

纯 stdlib: 不依赖 sklearn/hearthstone(架构铁律: 只有 adapter 可 import 实体类;
幸运币判定/职业名/英雄兜底表复用 consts)。三层分工:
  训练器 python -m trainer mulligan  产出模型产物(依赖 sklearn);
  本层 MulliganAdvisor              读 LATEST 模型 + 专家先验, 同步出事实;
  render(输出层)                    事实 → 中文结论/建议行(阈值也在这层)。

本层只产机读事实: 增益/双侧胜率/样本量/出处键(src: coin|class|all|prior|none)/
匹配对照。不产任何中文结论词 —— "建议留/样本不足"等属输出层政策(render)。

结论三层来源(设计见 docs/MULLIGAN_AI.md):
  平滑统计表(级联: 同先手→本职业→全体) > 专家先验(data/mulligan_prior.yaml)
  > 费用启发; 逻辑回归过 AUC 门控后才对建议集合有拍板权。
建议 = 在 2^n 个候选留牌集合上取最优(协同一等公民), 而非逐卡独立判断。
"""
from __future__ import annotations

import json
import math
import os
import random
from itertools import combinations
from pathlib import Path

from .consts import HERO_ID_CLASS, is_coin

# ── 统计机制参数(数据的读法, 非展示; 结论阈值在 render 输出层) ──
BETA_PRIOR_N = 2.0     # Beta 平滑虚拟样本数(以全局胜率/先验为均值)
SHRINK_N = 4.0         # 数据向先验收缩: 权重 n/(n+SHRINK_N)
N_COIN_CELL = 3        # (职业×先手) 格子最少样本, 否则级联回本职业行
N_CLASS_CELL = 5       # 本职业格最少样本, 否则级联回全体
LR_AUC_GATE = 0.55     # LR 时序留出 AUC 低于此值时, 集合评分权归统计表
ENUM_MAX_CARDS = 10    # 起手去重卡数超过此值退回贪心(理论最多 4)
MATCHED_MIN = 3        # 同情境匹配证据最少局数

UNKNOWN = "UNKNOWN"    # 提取不到职业时的哨兵(本层专用)


def tabpfn_env(data_dir) -> None:
    """TabPFN 权重/HF 缓存固定落项目数据目录, 绝不写 C 盘用户缓存。
    必须在首次 import tabpfn 之前调用。"""
    d = Path(data_dir)
    os.environ.setdefault("TABPFN_MODEL_CACHE_DIR", str(d / "cache" / "tabpfn"))
    os.environ.setdefault("HF_HOME", str(d / "cache" / "hf"))


def hero_class(cid: str | None, carddb=None) -> str | None:
    """英雄卡 card_id → CLASS 英文标签。权威 = 卡表 cardClass(含英雄皮肤);
    卡表缓存过旧无此字段时降级 consts.HERO_ID_CLASS 前缀表。"""
    if not cid:
        return None
    cls = carddb.card_class(cid) if carddb else None
    if cls:
        return cls
    for prefix, cls in HERO_ID_CLASS.items():
        if str(cid).startswith(prefix):
            return cls
    return None


# ════════════════════ 专家先验(人工维护文件) ════════════════════

def load_prior(path: Path | str, deck: str, carddb, known_ids: set) -> dict:
    """解析先验文件 → {"cards": {cid: entry}, "pairs": {(a,b): bonus}, "engine": [cid]}。
    known_ids 用于把中文名反查成 card_id(统计表/卡表词汇)。"""
    out = {"cards": {}, "pairs": {}, "engine": []}
    path = Path(path)
    if not path.exists():
        return out
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001  坏文件: 先验静默降级为空
        return out
    sec = data.get(deck) or data.get("通用") or {}
    name2id = {}
    for cid in known_ids:
        nm = carddb.name(cid)
        if nm and nm != cid:
            name2id[nm] = cid

    def resolve(tok):
        tok = str(tok).strip()
        return tok if tok in known_ids else name2id.get(tok)

    for name, ent in (sec.get("cards") or {}).items():
        cid = resolve(name)
        if cid and isinstance(ent, dict):
            out["cards"][cid] = ent
    for ent in sec.get("pairs") or []:
        ids = [resolve(t) for t in ent.get("cards") or []]
        bonus = ent.get("bonus")
        if all(ids) and isinstance(bonus, (int, float)):
            out["pairs"][tuple(sorted(ids))] = float(bonus)
    out["engine"] = [c for c in (resolve(t) for t in sec.get("engine") or []) if c]
    return out


def prior_gain(prior: dict, cid: str, coin: int | None) -> float | None:
    ent = prior.get("cards", {}).get(cid)
    if not ent:
        return None
    base = ent.get("keep")
    if coin == 1 and ent.get("keep_coin") is not None:
        base = ent["keep_coin"]
    elif coin == 0 and ent.get("keep_first") is not None:
        base = ent["keep_first"]
    return None if base is None else float(base)


# ════════════════════ 平滑统计表 ════════════════════
# cell = {"keep": {"w": n, "l": n}, "drop": {...}}; 格键 "职业|先手"(先手: 1/0/*)

def new_cell() -> dict:
    return {"keep": {"w": 0, "l": 0}, "drop": {"w": 0, "l": 0}}


def cell_n(cell: dict) -> int:
    return sum(cell[side][k] for side in ("keep", "drop") for k in "wl")


def table_update(stats: dict, opp_class: str, coin: bool, result: int,
                 cards: list, kept: list) -> None:
    """一局的留牌事实计入统计表(训练器调用; 推理层只读)。"""
    kept_c: dict = {}
    for c in kept:
        kept_c[c] = kept_c.get(c, 0) + 1
    cnt: dict = {}
    for c in cards:
        cnt[c] = cnt.get(c, 0) + 1
    for cid, n in cnt.items():
        k = kept_c.get(cid, 0)
        d = n - k
        if k == 0 and d == 0:
            continue
        for key in (f"{opp_class}|{int(coin)}", f"{opp_class}|*", "*|*"):
            cell = stats.setdefault(cid, {}).setdefault(key, new_cell())
            cell["keep"]["w" if result else "l"] += k
            cell["drop"]["w" if result else "l"] += d


def pick_cell(stats: dict, cid: str, opp_class: str,
              coin: int | None) -> tuple[dict | None, str | None]:
    """级联取样本最足的格子: 同先手(n≥3) → 本职业(n≥5) → 全体。
    返回 (cell, 出处键 coin|class|all)。"""
    cascade = [("all", "*|*", 0)]
    if opp_class != "*":
        cascade.insert(0, ("class", f"{opp_class}|*", N_CLASS_CELL))
        if coin is not None:
            cascade.insert(0, ("coin", f"{opp_class}|{coin}", N_COIN_CELL))
    ent = stats.get(cid) or {}
    for src, key, min_n in cascade:
        c = ent.get(key)
        if c and cell_n(c) >= max(min_n, 1):
            return c, src
    return None, None


def cost_prior(cid: str, carddb) -> float:
    """费用启发先验(最弱档): 低费倾向留、高费倾向换。有专家先验时不启用。"""
    cost = carddb.cost(cid)
    if cost is None:
        return 0.0
    if cost <= 2:
        return 0.03
    if cost == 3:
        return 0.0
    return max(-0.15, -(cost - 3) * 0.04)


def _wr(side: dict, mean: float) -> float:
    n = side["w"] + side["l"]
    return (side["w"] + BETA_PRIOR_N * mean) / (n + BETA_PRIOR_N)


def card_advice(stats: dict, cid: str, opp_class: str, coin: int | None,
                deck_wr: float, carddb, prior: dict | None = None) -> dict:
    """逐卡留牌增益事实 = P(胜|留) − P(胜|换); 级联取样本最足的格子。
    只回机读事实(src 键: coin|class|all|prior|none), 结论词在 render 输出层。"""
    prior = prior or {"cards": {}, "pairs": {}, "engine": []}
    pg = prior_gain(prior, cid, coin)
    fill = pg if pg is not None else cost_prior(cid, carddb)
    cell, src = pick_cell(stats, cid, opp_class, coin)
    if cell is None:
        return {"n": 0, "gain": pg if pg is not None else fill,
                "keep_wr": None, "drop_wr": None, "keep_n": 0, "drop_n": 0,
                "src": "prior" if pg is not None else "none", "prior": pg is not None}
    nk, nd = sum(cell["keep"].values()), sum(cell["drop"].values())
    keep_wr = _wr(cell["keep"], deck_wr + (pg or 0) / 2)
    drop_wr = _wr(cell["drop"], deck_wr - (pg or 0) / 2)
    w = min(nk, nd) / (min(nk, nd) + SHRINK_N)
    return {"n": nk + nd, "gain": w * (keep_wr - drop_wr) + (1 - w) * fill,
            "keep_wr": keep_wr, "drop_wr": drop_wr, "keep_n": nk, "drop_n": nd,
            "src": src, "prior": pg is not None}


# ════════════════════ 集合枚举 + Thompson 探索 ════════════════════

def best_keep_set(cards: list, gains: dict, pair_bonus: dict) -> list:
    """在全部候选留牌集合上取分最高者(升序枚举+严格比较 → 平分时取最小集)。
    score(set) = Σ 单卡增益 + Σ 专家 pair 协同。卡数超限退回贪心。"""
    uniq = sorted(set(cards), key=cards.index)
    if len(uniq) > ENUM_MAX_CARDS:
        return [c for c in uniq if gains.get(c, 0) > 0]
    best, best_s = [], 0.0                     # 空集基线 0 分
    for size in range(1, len(uniq) + 1):
        for combo in combinations(uniq, size):
            cs = set(combo)
            s = sum(gains.get(c, 0) for c in combo)
            s += sum(b for p, b in pair_bonus.items() if set(p) <= cs)
            if s > best_s + 1e-12:
                best, best_s = list(combo), s
    return best


def thompson_gain(stats: dict, cid: str, opp_class: str, coin: int | None,
                  deck_wr: float, carddb, prior: dict,
                  rng: random.Random) -> float:
    """从留牌增益的后验抽一次样(Beta): 数据窄→贴近均值(利用), 数据缺→宽→探索。"""
    pg = prior_gain(prior, cid, coin) or 0.0
    cell, _ = pick_cell(stats, cid, opp_class, coin)
    ps = []
    for side, mean in (("keep", deck_wr + pg / 2), ("drop", deck_wr - pg / 2)):
        w = cell[side]["w"] if cell else 0
        l = cell[side]["l"] if cell else 0
        ps.append(rng.betavariate(max(w + BETA_PRIOR_N * mean, 1e-6),
                                  max(l + BETA_PRIOR_N * (1 - mean), 1e-6)))
    return ps[0] - ps[1]


# ════════════════════ 逻辑回归推理(纯 JSON, 零 sklearn) ════════════════════

def lr_features(vocab: list, classes: list, pairs: list, cards: list, kept: list,
                coin: bool, opp_class: str) -> list:
    """特征布局: 手牌onehot + 留牌onehot + 先手 + 职业onehot + 留牌组合(协同)。
    vocab/classes/pairs 与 model.json 完全一致(训练侧也用本函数, 布局单点维护)。"""
    nc = len(vocab)
    x = [0.0] * (2 * nc + 1 + len(classes) + len(pairs))
    vidx = {c: i for i, c in enumerate(vocab)}
    for cid in cards:
        if cid in vidx:
            x[vidx[cid]] = 1.0
    kept_set = set(kept)
    for cid in kept:
        if cid in vidx:
            x[nc + vidx[cid]] = 1.0
    x[2 * nc] = 1.0 if coin else 0.0
    if opp_class in classes:
        x[2 * nc + 1 + classes.index(opp_class)] = 1.0
    for j, (a, b) in enumerate(pairs):         # 留牌组合(协同)特征
        if a in kept_set and b in kept_set:
            x[2 * nc + 1 + len(classes) + j] = 1.0
    return x


def lr_p(model: dict, cards: list, kept: list, coin: bool, opp_class: str) -> float:
    x = lr_features(model["vocab"], model["classes"], model.get("pairs", []),
                    cards, kept, coin, opp_class) + list(model.get("deck_vec") or [])
    z = model["intercept"] + sum(w * v for w, v in zip(model["coef"], x))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def lr_best_set(model: dict, cards: list, coin: bool, opp_class: str) -> list:
    """在 2^n 个候选集合上取 P(胜) 最高者(空集为基线; 平分取最小集)。"""
    uniq = sorted(set(cards), key=cards.index)
    if len(uniq) > ENUM_MAX_CARDS:
        base = lr_p(model, cards, [], coin, opp_class)
        return [c for c in uniq if lr_p(model, cards, [c], coin, opp_class) > base]
    best, best_s = [], lr_p(model, cards, [], coin, opp_class)
    for size in range(1, len(uniq) + 1):
        for combo in combinations(uniq, size):
            p = lr_p(model, cards, list(combo), coin, opp_class)
            if p > best_s + 1e-12:
                best, best_s = list(combo), p
    return best


# ════════════════════ 同情境匹配证据 ════════════════════

def matched_evidence(digest: list, cid: str, hand: list, opp_class: str,
                     coin: int | None) -> dict:
    """在近似相同起手里找"留/换都发生过"的对局(机读事实, 中文在 render):
    level: near=近似手牌(共享≥2张) / same=同职业同手 / none=样本不足。"""
    def split(pool):
        keep = [g for g in pool if cid in g["k"]]
        drop = [g for g in pool if cid not in g["k"]]
        wr = lambda gs: (sum(g["r"] for g in gs), len(gs) - sum(g["r"] for g in gs))
        return {"keep": wr(keep), "drop": wr(drop)}

    pool = [g for g in digest if cid in g["f"]
            and (opp_class == "*" or g["c"] == opp_class)
            and (coin is None or g["o"] == coin)]
    hand_set = set(hand)
    near = [g for g in pool if len(set(g["f"]) & hand_set) >= 2]
    if len(near) >= MATCHED_MIN:
        out = split(near)
        out.update({"level": "near", "n": len(near)})
    elif len(pool) >= MATCHED_MIN:
        out = split(pool)
        out.update({"level": "same", "n": len(pool)})
    else:
        out = {"level": "none", "n": max(len(near), len(pool)),
               "keep": (0, 0), "drop": (0, 0)}
    return out


# ════════════════════ 模型产物读取 + 实时建议器 ════════════════════

def models_root_for(data_dir, deck: str) -> Path:
    safe = "".join("_" if c in '\\/:*?"<>|' else c for c in deck).strip() or "未知卡组"
    return Path(data_dir) / "models" / "mulligan" / safe


def read_latest(root: Path | str) -> tuple[str | None, dict]:
    """读 LATEST 版本产物 → (版本, {meta/seen/stats/lr/digest})。"""
    latest = Path(root) / "LATEST.json"
    if not latest.exists():
        return None, {}
    try:
        ver = json.loads(latest.read_text(encoding="utf-8"))["version"]
        vdir = Path(root) / ver
        art = {"meta": json.loads((vdir / "meta.json").read_text(encoding="utf-8")),
               "seen": json.loads((vdir / "games_seen.json").read_text(encoding="utf-8")),
               "stats": json.loads((vdir / "stats.json").read_text(encoding="utf-8"))}
        for name, key in (("model.json", "lr"), ("games_digest.json", "digest"),
                          ("tabpfn.json", "tabpfn")):
            p = vdir / name
            art[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        return ver, art
    except (ValueError, KeyError, OSError):
        return None, {}


class TabPFNWrap:
    """TabPFN 开源表格基座(上下文学习)评分器 —— 可选依赖, 缺包即不可用。

    fit = 存上下文(我们的全局对局行); 打分 = 对候选留牌集合批量出 P(胜)。
    权重文件由 tabpfn_env 钉在项目数据目录, 不落 C 盘。"""

    def __init__(self, payload: dict, data_dir) -> None:
        self.payload = payload
        self.data_dir = data_dir
        self._learner = None

    @classmethod
    def usable(cls, payload: dict | None, data_dir) -> bool:
        """产物存在 + 时序留出过 AUC 门控 + tabpfn 包可导入。"""
        if not payload or not payload.get("X"):
            return False
        auc = (payload.get("metrics") or {}).get("test_auc")
        if auc is None or auc < LR_AUC_GATE:
            return False
        tabpfn_env(data_dir)
        try:
            import tabpfn  # noqa: F401
        except ImportError:
            return False
        return True

    def _fit_learner(self):
        if self._learner is None:
            import numpy as np
            tabpfn_env(self.data_dir)
            from tabpfn import TabPFNClassifier
            clf = TabPFNClassifier(device="cpu", ignore_pretraining_limits=True)
            clf.fit(np.array(self.payload["X"], dtype=np.float32),
                    np.array(self.payload["y"], dtype=int))
            self._learner = clf
        return self._learner

    def _p(self, kept: list, cards: list, coin: bool, opp_class: str) -> float:
        import numpy as np
        x = np.array([lr_features(self.payload["vocab"], self.payload["classes"],
                                  self.payload.get("pairs", []), cards, kept,
                                  coin, opp_class)], dtype=np.float32)
        return float(self._fit_learner().predict_proba(x)[0, 1])

    def best_set(self, cards: list, coin: bool, opp_class: str) -> list:
        """在 2^n 候选集合上取 P(胜) 最高者(空集为基线; 平分取最小集)。"""
        import numpy as np
        payload = self.payload
        vocab, classes = payload["vocab"], payload["classes"]
        pairs = payload.get("pairs", [])
        uniq = sorted(set(cards), key=cards.index)
        cand = [list(combo) for size in range(len(uniq) + 1)
                for combo in combinations(uniq, size)]
        tail = list(payload.get("deck_vec") or [])
        X = np.array([lr_features(vocab, classes, pairs, cards, kept,
                                  coin, opp_class) + tail for kept in cand],
                     dtype=np.float32)
        probs = self._fit_learner().predict_proba(X)[:, 1]
        best, best_p = [], float(probs[0])       # cand[0] = 空集基线
        for kept, p in zip(cand[1:], probs[1:]):
            if p > best_p + 1e-12:
                best, best_p = kept, float(p)
        return best


class MulliganAdvisor:
    """实时留牌建议: 读 LATEST 模型 + 专家先验(改动即生效), 同步出事实。

    用法: advisor.advise(offered_cids, opp_class, coin) → 事实 dict | None(无模型)。
    每次调用检查 LATEST/先验文件的 mtime, 训练器产出新版本后自动切换。"""

    def __init__(self, root: Path | str, deck: str, carddb,
                 prior_path: Path | str | None = None) -> None:
        self.root = Path(root)
        self.deck = deck
        self.carddb = carddb
        self.prior_path = Path(prior_path) if prior_path else \
            self.root.parents[2] / "mulligan_prior.yaml"   # data/models/mulligan/<卡组> → data/
        self._stamp = None                  # (LATEST mtime, prior mtime) 失配即重读
        self._ver, self._art = None, {}
        self._prior = {"cards": {}, "pairs": {}, "engine": []}
        self._tab = None                    # TabPFNWrap(懒加载; 缺包/未过门控=不可用)

    def _refresh(self) -> None:
        latest = self.root / "LATEST.json"
        prior_path = self.prior_path
        try:
            stamp = (latest.stat().st_mtime if latest.exists() else None,
                     prior_path.stat().st_mtime if prior_path.exists() else None)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp = stamp
        self._ver, self._art = read_latest(self.root)
        known = set(self._art.get("stats", {}).get("cards", {})) | \
            set((self._art.get("lr") or {}).get("vocab", []))
        self._prior = load_prior(prior_path, self.deck, self.carddb, known)

    def advise(self, offered: list, opp_class: str, coin: bool,
               explore: bool = False, seed: int | None = None) -> dict | None:
        """起手 card_id 列表 + 对手 CLASS → 事实 dict; 无模型返回 None。
        explore=True 时用 Thompson 采样出探索局(偏离均值的卡列入 deviations)。"""
        self._refresh()
        if self._ver is None or not offered:
            return None
        stats = self._art["stats"].get("cards", {})
        deck_wr = self._art["stats"].get("deck_wr", 0.5)
        lr = self._art.get("lr")
        digest = self._art.get("digest") or []
        cls = opp_class or UNKNOWN
        uniq = sorted(set(offered), key=offered.index)
        coin_i = 1 if coin else 0
        advs = {c: card_advice(stats, c, cls, coin_i, deck_wr,
                               self.carddb, self._prior) for c in uniq}

        auc = ((lr or {}).get("metrics") or {}).get("test_auc")
        lr_ok = lr is not None and auc is not None and auc >= LR_AUC_GATE
        mean_gains = {c: a["gain"] for c, a in advs.items()}
        pair_bonus = {p: b for p, b in self._prior["pairs"].items()
                      if set(p) <= set(uniq)}
        tab_payload = self._art.get("tabpfn")
        if TabPFNWrap.usable(tab_payload, self.prior_path.parent):
            if self._tab is None:
                self._tab = TabPFNWrap(tab_payload, self.prior_path.parent)
            mean_keep = self._tab.best_set(uniq, bool(coin_i), cls)
            scorer = "tabpfn"
        elif lr_ok:
            mean_keep = lr_best_set(lr, uniq, bool(coin_i), cls)
            scorer = "lr"
        else:
            mean_keep = best_keep_set(uniq, mean_gains, pair_bonus)
            scorer = "table"
        keep, deviations = mean_keep, []
        if explore:                                # Thompson 探索局
            rng = random.Random(seed) if seed is not None else random.Random()
            sampled = {c: thompson_gain(stats, c, cls, coin_i, deck_wr,
                                        self.carddb, self._prior, rng)
                       for c in uniq}
            keep = best_keep_set(uniq, sampled, pair_bonus)
            deviations = [{"card_id": c, "mean": mean_gains[c],
                           "sample": sampled[c]}
                          for c in uniq if (c in keep) != (c in mean_keep)]

        per_card = {}
        for c in uniq:
            a = advs[c]
            per_card[c] = {"name": self.carddb.name(c) or c, "gain": a["gain"],
                           "keep_wr": a["keep_wr"], "drop_wr": a["drop_wr"],
                           "src": a["src"], "n": a["n"], "prior": a["prior"],
                           "matched": matched_evidence(digest, c, uniq, cls, coin_i)
                           if digest else None}
        return {"version": self._ver, "opp_class": cls, "coin": coin,
                "deck_wr": deck_wr, "keep": keep,
                "drop": [c for c in uniq if c not in keep],
                "per_card": per_card, "scorer": scorer, "auc": auc,
                "deviations": deviations}
