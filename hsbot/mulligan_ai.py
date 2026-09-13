"""留牌推理层 —— 模型产物的加载与同步推理(训练器与实时军师共用)。

纯 stdlib: 不依赖 sklearn/hearthstone(架构铁律: 只有 adapter 可 import 实体类;
幸运币判定为此本地实现, 与 store.is_coin 同语义)。三层分工:
  训练器 scripts/train_mulligan.py  产出模型产物(依赖 sklearn);
  本层 MulliganAdvisor              读 LATEST 模型 + 专家先验, 同步出建议;
  render                            只格式化本层给的结论字段。

结论三层来源(设计见 docs/MULLIGAN_AI.md):
  平滑统计表(级联: 同先手→本职业→全体) > 专家先验(data/mulligan_prior.yaml)
  > 费用启发; 逻辑回归过 AUC 门控后才对建议集合有拍板权。
建议 = 在 2^n 个候选留牌集合上取最优(协同一等公民), 而非逐卡独立判断。
"""
from __future__ import annotations

import json
import math
import random
from itertools import combinations
from pathlib import Path

# ── 职业名 ──
CLASS_ZH = {"WARRIOR": "战士", "SHAMAN": "萨满", "ROGUE": "潜行者",
            "PALADIN": "圣骑士", "HUNTER": "猎人", "DRUID": "德鲁伊",
            "WARLOCK": "术士", "MAGE": "法师", "PRIEST": "牧师",
            "DEMONHUNTER": "恶魔猎手", "DEATHKNIGHT": "死亡骑士"}
HERO_ID_CLASS = {"HERO_01": "WARRIOR", "HERO_02": "SHAMAN", "HERO_03": "ROGUE",
                 "HERO_04": "PALADIN", "HERO_05": "HUNTER", "HERO_06": "DRUID",
                 "HERO_07": "WARLOCK", "HERO_08": "MAGE", "HERO_09": "PRIEST",
                 "HERO_10": "DEMONHUNTER", "HERO_11": "DEATHKNIGHT"}
UNKNOWN = "UNKNOWN"

# ── 平滑/阈值参数(集中一处, 调参改这里) ──
BETA_PRIOR_N = 2.0     # Beta 平滑虚拟样本数(以全局胜率/先验为均值)
SHRINK_N = 4.0         # 数据向先验收缩: 权重 n/(n+SHRINK_N)
N_COIN_CELL = 3        # (职业×先手) 格子最少样本, 否则级联回本职业行
N_CLASS_CELL = 5       # 本职业格最少样本, 否则级联回全体
N_ADVICE_MIN = 6       # 低于此样本只报"样本不足"(有专家先验的卡除外)
GAIN_KEEP = 0.03       # 增益 ≥ +3% → 建议留
GAIN_DROP = -0.03      # 增益 ≤ -3% → 建议换
LR_AUC_GATE = 0.55     # LR 时序留出 AUC 低于此值时, 建议权归统计表
ENUM_MAX_CARDS = 10    # 起手去重卡数超过此值退回贪心(理论最多 4)
MATCHED_MIN = 3        # 同情境匹配证据最少局数
PRIOR_TXT = "专家先验"


def class_zh(en: str) -> str:
    if en == UNKNOWN:
        return "未知职业"
    return CLASS_ZH.get(en, "不限职业" if en == "*" else en)


def is_coin(cid: str | None) -> bool:
    return bool(cid) and ("COIN" in cid.upper() or cid.upper() == "GAME_005")


def hero_class(cid: str | None) -> str | None:
    """英雄卡 card_id → CLASS 英文标签(HERO_09av → PRIEST)。"""
    if not cid:
        return None
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
    """级联取样本最足的格子: 同先手(n≥3) → 本职业(n≥5) → 全体。"""
    cascade = [("*|*", 0, "全体")]
    if opp_class != "*":
        cascade.insert(0, (f"{opp_class}|*", N_CLASS_CELL, "本职业"))
        if coin is not None:
            cascade.insert(0, (f"{opp_class}|{coin}", N_COIN_CELL, "同先手"))
    ent = stats.get(cid) or {}
    for key, min_n, label in cascade:
        c = ent.get(key)
        if c and cell_n(c) >= max(min_n, 1):
            return c, label
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
    """逐卡留牌增益 = P(胜|留) − P(胜|换); 级联取样本最足的格子。
    无数据的卡: 专家先验兜底(出处"专家先验"), 否则费用启发("样本不足")。"""
    prior = prior or {"cards": {}, "pairs": {}, "engine": []}
    pg = prior_gain(prior, cid, coin)
    fill = pg if pg is not None else cost_prior(cid, carddb)
    cell, src = pick_cell(stats, cid, opp_class, coin)
    if cell is None:
        if pg is not None:
            label = "建议留" if pg >= GAIN_KEEP else \
                "建议换" if pg <= GAIN_DROP else "先验中性"
            return {"n": 0, "gain": pg, "prior": True, "src": PRIOR_TXT,
                    "label": label, "keep_wr": None, "drop_wr": None,
                    "keep_n": 0, "drop_n": 0}
        return {"n": 0, "gain": fill, "prior": True, "src": "无数据",
                "label": "样本不足", "keep_wr": None, "drop_wr": None,
                "keep_n": 0, "drop_n": 0}
    nk, nd = sum(cell["keep"].values()), sum(cell["drop"].values())
    keep_wr = _wr(cell["keep"], deck_wr + (pg or 0) / 2)
    drop_wr = _wr(cell["drop"], deck_wr - (pg or 0) / 2)
    w = min(nk, nd) / (min(nk, nd) + SHRINK_N)
    gain = w * (keep_wr - drop_wr) + (1 - w) * fill
    if nk + nd < N_ADVICE_MIN and pg is None:   # 有专家先验的卡不受样本门槛限制
        label = "样本不足"
    elif gain >= GAIN_KEEP:
        label = "建议留"
    elif gain <= GAIN_DROP:
        label = "建议换"
    else:
        label = "中性"
    return {"n": nk + nd, "gain": gain, "prior": False, "src": src,
            "label": label, "keep_wr": keep_wr, "drop_wr": drop_wr,
            "keep_n": nk, "drop_n": nd}


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
                    cards, kept, coin, opp_class)
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
    """在近似相同起手里找"留/换都发生过"的对局: 近似手牌(共享≥2张) → 同职业同手。
    这是唯一能让"该不该换一种留法"获得数据对照的途径。"""
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
        out.update({"level": "近似手牌", "n": len(near)})
    elif len(pool) >= MATCHED_MIN:
        out = split(pool)
        out.update({"level": "同职业同手", "n": len(pool)})
    else:
        out = {"level": "无可比对局", "n": max(len(near), len(pool)),
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
        for name, key in (("model.json", "lr"), ("games_digest.json", "digest")):
            p = vdir / name
            art[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        return ver, art
    except (ValueError, KeyError, OSError):
        return None, {}


class MulliganAdvisor:
    """实时留牌建议: 读 LATEST 模型 + 专家先验(改动即生效), 同步出结论。

    用法: advisor.advise(offered_cids, opp_hero_cid, coin) → 结论 dict | None(无模型)。
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

    # ---------- 建议 ----------
    def advise(self, offered: list, opp_class: str, coin: bool,
               explore: bool = False, seed: int | None = None) -> dict | None:
        """起手 card_id 列表 + 对手 CLASS → 结论 dict; 无模型返回 None。
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
        if lr_ok:
            mean_keep = lr_best_set(lr, uniq, bool(coin_i), cls)
            basis = f"LR 集合枚举(AUC {auc:.2f}) + 专家先验"
        else:
            mean_keep = best_keep_set(uniq, mean_gains, pair_bonus)
            basis = "统计表+专家先验, 集合枚举" + \
                (f"(LR AUC {auc:.2f} 过低, 仅参考)"
                 if lr is not None and auc is not None else "")
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
            ev = matched_evidence(digest, c, uniq, cls, coin_i) if digest else None
            ev_txt = ""
            if ev and ev["level"] != "无可比对局":
                kw, kl = ev["keep"]
                dw, dl = ev["drop"]
                ev_txt = (f"{ev['level']}{ev['n']}局: "
                          f"留{kw + kl}({kw}胜{kl}负) 换{dw + dl}({dw}胜{dl}负)")
            per_card[c] = {"name": self.carddb.name(c) or c, "label": a["label"],
                           "gain": a["gain"], "keep_wr": a["keep_wr"],
                           "drop_wr": a["drop_wr"], "src": a["src"], "ev": ev_txt}
        return {"version": self._ver, "opp_class": cls, "coin": coin,
                "deck_wr": deck_wr, "keep": keep,
                "drop": [c for c in uniq if c not in keep],
                "per_card": per_card, "basis": basis, "deviations": deviations}
