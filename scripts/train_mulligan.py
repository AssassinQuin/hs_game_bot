"""留牌 AI 训练器 —— 从训练语料学习「按对手职业的起手留牌」。

数据: data/training/<卡组>/*.jsonl(首行 _meta + 归一化事件流, corpus.py 产出)。
我方判定用 config.battletag; 对手职业统一从事件流的英雄实体 CLASS 标签提取
(旧样本 _meta.heroes 大面积缺失, 事件流永远可靠); 幸运币不参与留牌决策。

结论的三层来源与一个闭环(设计与偏差讨论见 docs/MULLIGAN_AI.md):
  1) 平滑统计表 —— (卡牌 × 对手职业 × 先/后手) 的 留/换 胜负计数, 三级级联
     (同先手行 → 本职业行 → 全体) + Beta 平滑。零依赖、可解释。
  2) 专家先验 —— data/mulligan_prior.yaml(人工维护): 无数据卡由先验兜底,
     有数据卡作为收缩目标; 话语权随样本自动衰减。含 pairs 协同与引擎卡。
  3) 逻辑回归(可选, sklearn) —— 整手牌条件化 + 留牌组合特征, 时序留出验证
     (AUC ≥ LR_AUC_GATE 才有拍板权); 系数落 JSON, 推理不依赖 sklearn。
建议 = 在 2^n 个候选留牌集合上取最优(集合枚举, 协同一等公民), 而非逐卡独立;
--explore 用 Thompson 采样从后验探索最缺证据的方向; 逐卡附"同情境匹配"证据。

增量: 每次 train 全量重扫语料(每局只读 _meta+英雄段, 成本恒小, 无状态漂移),
对比上一版 games_seen 报告新增局数; 产物落版本目录 vNNN + LATEST.json。

用法:
  python scripts/train_mulligan.py                     # 训练+报告(config 默认卡组)
  python scripts/train_mulligan.py report              # 查看已保存模型
  python scripts/train_mulligan.py advise --vs 圣骑士 --coin \
      --hand 交易馆长,危机,JAIL_718                     # 给一个起手出建议
  python scripts/train_mulligan.py advise --explore --seed 7 ...   # Thompson 探索局
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.store import is_coin

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
LR_MIN_GAMES = 20      # 语料低于此局数不训逻辑回归
LR_AUC_GATE = 0.55     # LR 时序留出 AUC 低于此值时, 建议权归统计表
LR_TEST_FRAC = 0.2     # 时序留出比例
LR_PAIR_SUPPORT = 4    # 留牌组合特征最少共现次数
ENUM_MAX_CARDS = 10    # 起手去重卡数超过此值退回贪心(理论最多 4)
MATCHED_MIN = 3        # 同情境匹配证据最少局数
ENUM_PRIOR_TXT = "专家先验"


def class_zh(en: str) -> str:
    if en == UNKNOWN:
        return "未知职业"
    return CLASS_ZH.get(en, "不限职业" if en == "*" else en)


# ════════════════════ 1. 语料 → 样本 ════════════════════

@dataclass
class Game:
    path: str          # 文件名(会话_gNN, 字典序即时序)
    result: int        # 1=胜 0=负
    coin: bool         # 后手(起手有幸运币)
    opp_class: str     # 对手职业(CLASS 英文标签)
    cards: list        # 起手 offered(去幸运币, 保序, 可能重复)
    kept: list         # 其中留下的(逐张)
    mtime: float = 0.0
    size: int = 0


def _scan_hero_classes(path: Path, need_pids: set) -> dict:
    """从事件流提取 {player_id(str): CLASS}。英雄实体总在文件前段, 提到即止。"""
    found: dict = {}
    with path.open(encoding="utf-8") as fp:
        fp.readline()                      # 跳过 _meta
        for line in fp:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("t") != "FullEntity":
                continue
            tags = {k: v for k, v in ev.get("tags", [])}
            if tags.get("CARDTYPE") == "HERO" and ev.get("card_id"):
                ctrl = tags.get("CONTROLLER")
                if ctrl is not None:
                    found.setdefault(str(ctrl), tags.get("CLASS") or UNKNOWN)
                    if need_pids <= set(found):
                        break
    return found


def _class_from_hero_id(cid) -> str | None:
    if not cid:
        return None
    for prefix, cls in HERO_ID_CLASS.items():
        if str(cid).startswith(prefix):
            return cls
    return None


def load_game(path: Path, battletag: str) -> tuple[Game | None, str]:
    """单局 → Game; (None, 原因) 表示该局不可用于训练(不是错误)。"""
    try:
        meta = json.loads(path.read_text(encoding="utf-8").split("\n", 1)[0])
    except (ValueError, OSError) as exc:
        return None, f"meta 不可读({exc})"
    players = meta.get("players") or {}
    self_pid = next((pid for pid, p in players.items()
                     if p.get("name") == battletag), None)
    if self_pid is None:               # 兜底: 唯一广播了留牌决定的玩家
        decided = [pid for pid, m in (meta.get("mulligan") or {}).items()
                   if isinstance(m, dict) and m.get("decided")]
        if len(decided) == 1:
            self_pid = decided[0]
    if self_pid is None:
        return None, "无法确定我方玩家"
    result = (meta.get("results") or {}).get(self_pid)
    if result == "WON":
        win = 1
    elif result == "LOST":
        win = 0
    else:
        return None, f"未出结果({result})"
    m = (meta.get("mulligan") or {}).get(self_pid) or {}
    if not m.get("decided"):
        return None, "留牌未广播决定"
    offered_all = [c for c in m.get("offered") or [] if c]
    offered = [c for c in offered_all if not is_coin(c)]
    kept = [c for c in m.get("kept") or [] if c and not is_coin(c)]
    if not offered:
        return None, "起手牌为空"
    opp_pid = next((pid for pid in players if pid != self_pid), None)
    opp_class = None
    if opp_pid is not None:
        heroes = _scan_hero_classes(path, {opp_pid})
        opp_class = heroes.get(opp_pid) or \
            _class_from_hero_id((meta.get("heroes") or {}).get(opp_pid))
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        mtime, size = 0.0, 0
    return Game(path=path.name, result=win,
                coin=any(is_coin(c) for c in offered_all),
                opp_class=opp_class or UNKNOWN, cards=offered, kept=kept,
                mtime=mtime, size=size), ""


def build_dataset(training_dir: Path, deck: str,
                  battletag: str) -> tuple[list[Game], Counter]:
    deck_dir = training_dir / deck
    if not deck_dir.is_dir():
        avail = [d.name for d in training_dir.iterdir() if d.is_dir()] \
            if training_dir.is_dir() else []
        raise SystemExit(f"语料目录不存在: {deck_dir}(现有: {', '.join(avail) or '无'})")
    games, skip = [], Counter()
    for path in sorted(deck_dir.glob("*.jsonl")):
        g, why = load_game(path, battletag)
        if g is None:
            skip[why] += 1
        else:
            games.append(g)
    return games, skip


# ════════════════════ 2. 专家先验(人工维护文件) ════════════════════
# data/mulligan_prior.yaml, 按卡组分节; 卡名/卡ID 均可。话语权随样本自动衰减。

def load_prior(path: Path, deck: str, carddb: CardDB, known_ids: set) -> dict:
    """解析先验文件 → {"cards": {cid: gain}, "pairs": {(a,b): bonus}, "engine": [cid]}。
    known_ids 用于把中文名反查成 card_id(统计表/卡表词汇)。"""
    out = {"cards": {}, "pairs": {}, "engine": []}
    if not path.exists():
        return out
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"(先验文件解析失败, 忽略: {exc})")
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
        if not cid or not isinstance(ent, dict):
            continue
        out["cards"][cid] = ent
    for ent in sec.get("pairs") or []:
        ids = [resolve(t) for t in ent.get("cards") or []]
        bonus = ent.get("bonus")
        if all(ids) and isinstance(bonus, (int, float)):
            out["pairs"][tuple(sorted(ids))] = float(bonus)
    out["engine"] = [c for c in (resolve(t) for t in sec.get("engine") or []) if c]
    return out


def prior_gain(prior: dict, cid: str, coin: int | None) -> float | None:
    ent = prior["cards"].get(cid)
    if not ent:
        return None
    base = ent.get("keep")
    if coin == 1 and ent.get("keep_coin") is not None:
        base = ent["keep_coin"]
    elif coin == 0 and ent.get("keep_first") is not None:
        base = ent["keep_first"]
    return None if base is None else float(base)


# ════════════════════ 3. 平滑统计表 ════════════════════
# cell = {"keep": {"w": n, "l": n}, "drop": {...}}; 格键 "职业|先手"(先手: 1/0/*)

def _new_cell() -> dict:
    return {"keep": {"w": 0, "l": 0}, "drop": {"w": 0, "l": 0}}


def _cell_n(cell: dict) -> int:
    return sum(cell[side][k] for side in ("keep", "drop") for k in "wl")


def table_update(stats: dict, g: Game) -> None:
    kept_c = Counter(g.kept)
    for cid, n in Counter(g.cards).items():
        k, d = kept_c.get(cid, 0), n - kept_c.get(cid, 0)
        if k == 0 and d == 0:
            continue
        for key in (f"{g.opp_class}|{int(g.coin)}", f"{g.opp_class}|*", "*|*"):
            cell = stats.setdefault(cid, {}).setdefault(key, _new_cell())
            cell["keep"]["w" if g.result else "l"] += k
            cell["drop"]["w" if g.result else "l"] += d


def _pick_cell(stats: dict, cid: str, opp_class: str,
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
        if c and _cell_n(c) >= max(min_n, 1):
            return c, label
    return None, None


def cost_prior(cid: str, carddb: CardDB) -> float:
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
                deck_wr: float, carddb: CardDB, prior: dict | None = None) -> dict:
    """逐卡留牌增益 = P(胜|留) − P(胜|换); 级联取样本最足的格子。
    无数据的卡: 专家先验兜底(标"专家先验"), 否则费用启发(标"样本不足")。"""
    prior = prior or {"cards": {}, "pairs": {}, "engine": []}
    pg = prior_gain(prior, cid, coin)
    fill = pg if pg is not None else cost_prior(cid, carddb)
    cell, src = _pick_cell(stats, cid, opp_class, coin)
    if cell is None:
        if pg is not None:
            label = "建议留" if pg >= GAIN_KEEP else \
                "建议换" if pg <= GAIN_DROP else "先验中性"
            return {"n": 0, "gain": pg, "prior": True, "src": ENUM_PRIOR_TXT,
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
    n_total = nk + nd
    if n_total < N_ADVICE_MIN:
        label = "样本不足"
    elif gain >= GAIN_KEEP:
        label = "建议留"
    elif gain <= GAIN_DROP:
        label = "建议换"
    else:
        label = "中性"
    return {"n": n_total, "gain": gain, "prior": False, "src": src,
            "label": label, "keep_wr": keep_wr, "drop_wr": drop_wr,
            "keep_n": nk, "drop_n": nd}


# ════════════════════ 4. 集合枚举 + Thompson 探索 ════════════════════

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
                  deck_wr: float, carddb: CardDB, prior: dict,
                  rng: random.Random) -> float:
    """从留牌增益的后验抽一次样(Beta): 数据窄→贴近均值(利用), 数据缺→宽→探索。"""
    pg = prior_gain(prior, cid, coin) or 0.0
    cell, _ = _pick_cell(stats, cid, opp_class, coin)
    sides = (("keep", deck_wr + pg / 2), ("drop", deck_wr - pg / 2))
    ps = []
    for side, mean in sides:
        w = cell[side]["w"] if cell else 0
        l = cell[side]["l"] if cell else 0
        ps.append(rng.betavariate(max(w + BETA_PRIOR_N * mean, 1e-6),
                                  max(l + BETA_PRIOR_N * (1 - mean), 1e-6)))
    return ps[0] - ps[1]


# ════════════════════ 5. 逻辑回归(sklearn 训练 / 纯 JSON 推理) ════════════════════

def _lr_features(model_or_vocab, classes, pairs, cards, kept, coin,
                 opp_class) -> list:
    if isinstance(model_or_vocab, dict):       # 便于推理侧直传模型
        m = model_or_vocab
        return _lr_features(m["vocab"], m["classes"], m["pairs"], cards,
                            kept, coin, opp_class)
    vocab = model_or_vocab
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


def _auc(y_true: list, scores: list) -> float | None:
    n_pos = sum(y_true)
    if not n_pos or n_pos == len(y_true):
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    s = sum(r for r, y in zip(ranks, y_true) if y)
    return (s - n_pos * (n_pos + 1) / 2) / ((len(y_true) - n_pos) * n_pos)


def _vocab_pairs(games: list) -> tuple[list, list]:
    vocab = sorted({c for g in games for c in g.cards})
    pair_n = Counter()
    for g in games:
        ks = sorted(set(g.kept))
        for a, b in combinations(ks, 2):
            pair_n[(a, b)] += 1
    pairs = sorted(p for p, n in pair_n.items() if n >= LR_PAIR_SUPPORT)
    return vocab, pairs


def _fit_eval(games: list, vocab: list, pairs: list) -> dict | None:
    """时序留出评估(前 80% 拟合 → 后 20% 打分), 返回测试段指标。"""
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    n_test = max(1, int(len(games) * LR_TEST_FRAC))
    tr, te = games[:-n_test], games[-n_test:]
    classes = sorted({g.opp_class for g in games if g.opp_class != UNKNOWN})
    xof = lambda g: _lr_features(vocab, classes, pairs, g.cards, g.kept,
                                 g.coin, g.opp_class)
    ytr = [g.result for g in tr]
    if len(set(ytr)) < 2:
        return None
    try:
        m = LogisticRegression(C=0.3, max_iter=2000).fit([xof(g) for g in tr], ytr)
        pte = m.predict_proba([xof(g) for g in te])[:, 1]
        yte = [g.result for g in te]
    except ValueError:
        return None
    out = {"n_train": len(tr), "n_test": len(te), "test_auc": _auc(yte, list(pte))}
    if 0 < sum(yte) < len(yte):
        ll = -sum(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9))
                  for y, p in zip(yte, pte)) / len(yte)
        out["test_logloss"] = round(ll, 3)
    if out["test_auc"] is not None:
        out["test_auc"] = round(out["test_auc"], 3)
    return out


def train_lr(games: list) -> dict | None:
    """全量拟合出服务模型(含留牌组合特征); 指标来自时序留出(不泄漏)。"""
    if len(games) < LR_MIN_GAMES:
        return None
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    vocab, pairs = _vocab_pairs(games)
    metrics = _fit_eval(games, vocab, pairs)
    classes = sorted({g.opp_class for g in games if g.opp_class != UNKNOWN})
    xof = lambda g: _lr_features(vocab, classes, pairs, g.cards, g.kept,
                                 g.coin, g.opp_class)
    try:
        model = LogisticRegression(C=0.3, max_iter=2000).fit(
            [xof(g) for g in games], [g.result for g in games])
    except ValueError:
        return None
    return {"vocab": vocab, "classes": classes, "pairs": pairs,
            "coef": model.coef_[0].tolist(),
            "intercept": float(model.intercept_[0]),
            "metrics": metrics or {}}


def _lr_p(model: dict, cards: list, kept: list, coin: bool,
          opp_class: str) -> float:
    x = _lr_features(model, model["classes"], model["pairs"], cards, kept,
                     coin, opp_class)
    z = model["intercept"] + sum(w * v for w, v in zip(model["coef"], x))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def lr_best_set(model: dict, cards: list, coin: bool, opp_class: str) -> list:
    """在 2^n 个候选集合上取 P(胜) 最高者(空集为基线; 平分取最小集)。"""
    uniq = sorted(set(cards), key=cards.index)
    if len(uniq) > ENUM_MAX_CARDS:
        order = sorted(uniq, key=lambda c: -_lr_p(model, cards, [c], coin, opp_class))
        return [c for c in order
                if _lr_p(model, cards, [c], coin, opp_class)
                > _lr_p(model, cards, [], coin, opp_class)]
    best, best_s = [], _lr_p(model, cards, [], coin, opp_class)
    for size in range(1, len(uniq) + 1):
        for combo in combinations(uniq, size):
            p = _lr_p(model, cards, list(combo), coin, opp_class)
            if p > best_s + 1e-12:
                best, best_s = list(combo), p
    return best


# ════════════════════ 6. 同情境匹配证据 ════════════════════

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


# ════════════════════ 7. 模型库(版本目录 + LATEST) ════════════════════

def models_root(cfg: Config, deck: str) -> Path:
    safe = "".join("_" if c in '\\/:*?"<>|' else c for c in deck).strip() or "未知卡组"
    return Path(cfg.data_dir) / "models" / "mulligan" / safe


def prior_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "mulligan_prior.yaml"


def _load_latest(root: Path) -> tuple[str | None, dict]:
    latest = root / "LATEST.json"
    if not latest.exists():
        return None, {}
    try:
        ver = json.loads(latest.read_text(encoding="utf-8"))["version"]
        vdir = root / ver
        art = {"meta": json.loads((vdir / "meta.json").read_text(encoding="utf-8")),
               "seen": json.loads((vdir / "games_seen.json").read_text(encoding="utf-8")),
               "stats": json.loads((vdir / "stats.json").read_text(encoding="utf-8"))}
        for name, key in (("model.json", "lr"), ("games_digest.json", "digest")):
            p = vdir / name
            art[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        return ver, art
    except (ValueError, KeyError, OSError):
        return None, {}


def _save_version(root: Path, deck: str, deck_dir: Path, stats: dict,
                  lr: dict | None, games: list, n_new: int,
                  skip: Counter) -> str:
    versions = [d.name for d in root.glob("v*") if d.is_dir()]
    ver = f"v{max((int(v[1:]) for v in versions), default=0) + 1:03d}"
    out = root / ver
    out.mkdir(parents=True, exist_ok=True)
    wins = sum(g.result for g in games)
    lr_out = None
    if lr is not None:
        lr_out = dict(lr)
        lr_out.pop("metrics", None)
        (out / "model.json").write_text(
            json.dumps(lr_out, ensure_ascii=False), encoding="utf-8")
    (out / "stats.json").write_text(
        json.dumps({"deck_wr": wins / len(games), "cards": stats},
                   ensure_ascii=False), encoding="utf-8")
    (out / "games_seen.json").write_text(
        json.dumps({g.path: [g.mtime, g.size] for g in games},
                   ensure_ascii=False), encoding="utf-8")
    (out / "games_digest.json").write_text(
        json.dumps([{"c": g.opp_class, "o": int(g.coin),
                     "f": sorted(set(g.cards)), "k": sorted(set(g.kept)),
                     "r": g.result} for g in games],
                   ensure_ascii=False), encoding="utf-8")
    metrics = (lr or {}).get("metrics") or {}
    (out / "meta.json").write_text(
        json.dumps({"version": ver, "deck": deck,
                    "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "n_games": len(games), "n_new": n_new, "wins": wins,
                    "skipped": dict(skip), "lr_metrics": metrics,
                    "class_counts": dict(Counter(g.opp_class for g in games)),
                    "coin_games": sum(g.coin for g in games)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (root / "LATEST.json").write_text(
        json.dumps({"version": ver}, ensure_ascii=False), encoding="utf-8")
    return ver


# ════════════════════ 8. 报告渲染 ════════════════════

def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def print_table_report(stats: dict, deck_wr: float, carddb: CardDB,
                       prior: dict) -> None:
    """逐卡留牌增益表(以样本最多的对手职业做级联查询)。"""
    by_n = lambda cid: _cell_n(stats[cid].get("*|*", _new_cell()))
    vocab = sorted(stats, key=lambda c: (-by_n(c), c))
    cls_counts = Counter(k.split("|", 1)[0]
                         for cid in stats for k in stats[cid] if k != "*|*")
    main_class = cls_counts.most_common(1)[0][0] if cls_counts else UNKNOWN
    print(f"── 单卡留牌增益(平滑统计表, 优先职业: {class_zh(main_class)}) ──")
    print(f"{'卡牌':<14} {'出处':<6} {'样本':>4} {'留牌率':>7} {'留→胜':>7} "
          f"{'换→胜':>7} {'增益':>8}  结论")
    for cid in vocab:
        adv = card_advice(stats, cid, main_class, None, deck_wr, carddb, prior)
        n_offer = adv["keep_n"] + adv["drop_n"]
        kept_rate = adv["keep_n"] / n_offer if n_offer else None
        name = (carddb.name(cid) or cid)[:12]
        note = "" if adv["prior"] else f"(n={adv['n']})"
        print(f"{name:<14} {adv['src']:<6} {n_offer:>4} {_pct(kept_rate):>7} "
              f"{_pct(adv['keep_wr']):>7} {_pct(adv['drop_wr']):>7} "
              f"{adv['gain'] * 100:>+7.1f}%  {adv['label']}{note}")


# ════════════════════ 9. 子命令 ════════════════════

def cmd_train(cfg: Config, deck: str) -> int:
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    games, skip = build_dataset(Path(cfg.training_dir), deck, cfg.battletag)
    if not games:
        print(f"没有可用于训练的对局(跳过: {dict(skip) or '无语料'})")
        return 1
    root = models_root(cfg, deck)
    prev_ver, prev = _load_latest(root)
    prev_seen = prev.get("seen") or {}
    n_new = sum(1 for g in games
                if prev_seen.get(g.path) != [g.mtime, g.size]) \
        if prev_seen else len(games)

    stats: dict = {}
    for g in games:
        table_update(stats, g)
    deck_wr = sum(g.result for g in games) / len(games)
    prior = load_prior(prior_path(cfg), deck, carddb, set(stats))
    lr = train_lr(games)

    print(f"── 留牌模型训练 ({deck}) ──")
    print(f"语料 {len(games)} 局(较上一版新增 {n_new}) │ 我方胜率 {_pct(deck_wr)} "
          f"│ 先手 {sum(not g.coin for g in games)} / 后手 "
          f"{sum(g.coin for g in games)} │ 对手: "
          + " ".join(f"{class_zh(k)}×{v}" for k, v in
                     Counter(g.opp_class for g in games).most_common()))
    if skip:
        print("跳过: " + " │ ".join(f"{k} {v}" for k, v in skip.items()))
    print(f"专家先验: {len(prior['cards'])} 卡 / {len(prior['pairs'])} 组合"
          + ("" if prior["cards"] or prior["pairs"] else "(无 — 可编辑 data/mulligan_prior.yaml)"))
    print_table_report(stats, deck_wr, carddb, prior)
    if lr is not None:
        m = lr["metrics"]
        print(f"── 逻辑回归(sklearn): {m.get('n_train', '?')} 局训练 / "
              f"{m.get('n_test', '?')} 局时序留出 │ AUC {m.get('test_auc', '—')} │ "
              f"LogLoss {m.get('test_logloss', '—')} │ 组合特征 {len(lr['pairs'])} ──")
    else:
        print(f"── 逻辑回归: 未启用(需 sklearn 且语料 ≥ {LR_MIN_GAMES} 局; 仅统计表) ──")
    print(f"置信度: {len(games)} 局样本"
          + ("尚少, 结论仅供对照(≥200 局后更可靠)" if len(games) < 200 else "。"))
    ver = _save_version(root, deck, Path(cfg.training_dir) / deck,
                        stats, lr, games, n_new, skip)
    print(f"模型已保存: {root / ver}" + (f"(上一版 {prev_ver})" if prev_ver else "(首版)"))
    return 0


def cmd_report(cfg: Config, deck: str) -> int:
    root = models_root(cfg, deck)
    ver, art = _load_latest(root)
    if ver is None:
        print(f"尚无已训练模型: {root}(先运行 train)")
        return 1
    meta = art["meta"]
    stats = art["stats"].get("cards", {})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    prior = load_prior(prior_path(cfg), deck, carddb, set(stats))
    print(f"── 留牌模型 {ver} ({deck}) ──")
    print(f"训练于 {meta['trained_at']} │ {meta['n_games']} 局(当批新增 "
          f"{meta['n_new']}) │ 我方胜率 {_pct(meta['wins'] / meta['n_games'])} │ "
          f"对手: " + " ".join(f"{class_zh(k)}×{v}" for k, v in
                               (meta.get("class_counts") or {}).items()))
    if meta.get("lr_metrics"):
        print(f"逻辑回归指标: {meta['lr_metrics']}")
    print_table_report(stats, art["stats"].get("deck_wr", 0.5), carddb, prior)
    print(f"模型目录: {root / ver}")
    return 0


def cmd_advise(cfg: Config, deck: str, hand: list, opp_class: str,
               coin: int | None, explore: bool = False,
               seed: int | None = None) -> int:
    root = models_root(cfg, deck)
    ver, art = _load_latest(root)
    if ver is None:
        print(f"尚无已训练模型: {root}(先运行 train)")
        return 1
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    stats = art["stats"].get("cards", {})
    lr = art.get("lr")
    digest = art.get("digest") or []
    deck_wr = art["stats"].get("deck_wr", 0.5)

    # 手牌: card:ID 直给, 或按卡表把中文名反查成 card_id
    ids = set(stats) | (set(lr["vocab"]) if lr else set())
    name_map = {carddb.name(cid): cid for cid in ids if carddb.name(cid)}
    cards = [t[5:] if t.startswith("card:") else name_map.get(t, t)
             for t in hand if t]
    cls = opp_class or "*"
    if cls != "*":
        rev = {zh: en for en, zh in CLASS_ZH.items()}
        cls = rev.get(cls) or _class_from_hero_id(cls) or \
            (cls.upper() if cls.upper() in CLASS_ZH else cls)
    prior = load_prior(prior_path(cfg), deck, carddb, ids)
    uniq = sorted(set(cards), key=cards.index)

    print(f"── 留牌建议 ({deck}, 模型 {ver}) ──")
    coin_txt = "后手(有幸运币)" if coin == 1 else "先手" if coin == 0 else "不限先/后手"
    print(f"对手: {class_zh(cls)} │ {coin_txt}"
          + (" │ Thompson 探索局" if explore else ""))
    cost = lambda c: carddb.cost(c)
    print("起手: " + "、".join(
        f"{carddb.name(c)}({'?' if cost(c) is None else cost(c)}费)" for c in cards))

    advs = [(c, card_advice(stats, c, cls, coin, deck_wr, carddb, prior))
            for c in uniq]
    mean_gains = {c: a["gain"] for c, a in advs}
    pair_bonus = {p: b for p, b in prior["pairs"].items()
                  if set(p) <= set(uniq)}

    # 建议权: LR 过 AUC 门控 → 用 LR 给集合打分; 否则统计表增益+专家协同, 集合枚举
    auc = ((lr or {}).get("metrics") or {}).get("test_auc")
    lr_ok = lr is not None and auc is not None and auc >= LR_AUC_GATE
    if lr_ok:
        mean_set = lr_best_set(lr, cards, bool(coin) if coin is not None else False, cls)
        basis = f"LR 集合枚举(AUC {auc:.2f}) + 专家先验"
    else:
        mean_set = best_keep_set(uniq, mean_gains, pair_bonus)
        basis = "统计表+专家先验, 集合枚举" + \
            (f"(LR AUC {auc:.2f} 过低, 仅参考)" if lr is not None and auc is not None else "")

    final_set = mean_set
    if explore:                                # Thompson: 从后验抽样决策
        rng = random.Random(seed) if seed is not None else random.Random()
        sampled = {c: thompson_gain(stats, c, cls, coin, deck_wr, carddb,
                                    prior, rng) for c in uniq}
        final_set = best_keep_set(uniq, sampled, pair_bonus)
        dev = [c for c in uniq if (c in final_set) != (c in mean_set)]
        if dev:
            print("探索说明(与均值建议不同, 用于积累反事实样本): " + "、".join(
                f"{carddb.name(c)}(均值{mean_gains[c] * 100:+.1f}%→抽样"
                f"{sampled[c] * 100:+.1f}%)" for c in dev))
        else:
            print("(本次抽样与均值建议一致, 无探索偏差)")

    nm = lambda c: carddb.name(c) or c
    print(f"建议: 留 ── {'、'.join(nm(c) for c in cards if c in final_set) or '(无)'} │ "
          f"换 ── {'、'.join(nm(c) for c in cards if c not in final_set) or '(无)'}"
          f"   [依据: {basis}]")
    print("逐卡(留→胜率 vs 换→胜率, 平滑增益 │ 同情境匹配证据):")
    for c, a in advs:
        ev = matched_evidence(digest, c, cards, cls, coin) if digest else None
        if ev and ev["level"] != "无可比对局":
            kw, kl = ev["keep"]
            dw, dl = ev["drop"]
            ev_txt = (f"│ {ev['level']}{ev['n']}局: 留{kw + kl}({kw}胜{kl}负) "
                      f"换{dw + dl}({dw}胜{dl}负)")
        else:
            ev_txt = "│ 无可比对局"
        print(f"  {nm(c):<14} {a['label']:<5} 留→{_pct(a['keep_wr'])} "
              f"换→{_pct(a['drop_wr'])} 增益{a['gain'] * 100:+.1f}% "
              f"({a['src']}) {ev_txt}")
    return 0


# ════════════════════ 10. CLI ════════════════════

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="起手留牌 AI 训练器(设计见 docs/MULLIGAN_AI.md)")

    def add_common(p):
        # 与顶层同名同 dest: 顶层已设值时 argparse 不再用子命令默认值覆盖,
        # 因此 --deck/--data 放在子命令前或后都生效。
        p.add_argument("--deck", default=None, help="卡组名(默认取 config deck_name)")
        p.add_argument("--data", default=None, help="训练语料目录")

    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--data-dir", dest="data_dir", default=None,
                    help="数据根目录(默认取 config data_dir)")
    add_common(ap)
    sub = ap.add_subparsers(dest="cmd")
    p_train = sub.add_parser("train", help="扫描语料训练并保存新版本")
    add_common(p_train)
    p_rep = sub.add_parser("report", help="查看已保存模型")
    add_common(p_rep)
    p_adv = sub.add_parser("advise", help="给一个起手场景出建议")
    add_common(p_adv)
    p_adv.add_argument("--hand", required=True,
                       help="起手手牌: 中文名或 card:ID, 逗号分隔")
    p_adv.add_argument("--vs", default="*", help="对手职业: 中文名/CLASS/HERO_06/*")
    p_adv.add_argument("--coin", action="store_true", help="后手")
    p_adv.add_argument("--first", action="store_true", help="先手")
    p_adv.add_argument("--explore", action="store_true",
                       help="Thompson 探索局: 从后验抽样决策, 积累反事实样本")
    p_adv.add_argument("--seed", type=int, default=None, help="探索采样种子(复现用)")

    argv = list(argv) if argv is not None else sys.argv[1:]
    if argv and argv[0] not in ("train", "report", "advise") \
            and not argv[0].startswith("-"):
        argv = ["train"] + argv              # 裸调用 → 等价 train
    args = ap.parse_args(argv)               # 严格解析: 配置参数须在子命令前
    if not args.cmd:
        args.cmd = "train"                   # 无子命令 → 等价 train
    cfg = Config.load({"config": args.config, "training_dir": args.data,
                       "data_dir": args.data_dir})
    deck = args.deck or cfg.deck_name
    if args.cmd == "train":
        return cmd_train(cfg, deck)
    if args.cmd == "report":
        return cmd_report(cfg, deck)
    hand = [t.strip() for t in re.split(r"[,，、]", args.hand) if t.strip()]
    coin = 1 if args.coin else (0 if args.first else None)
    return cmd_advise(cfg, deck, hand, args.vs, coin, args.explore, args.seed)


if __name__ == "__main__":
    sys.exit(main())
