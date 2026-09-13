"""留牌 AI 训练器 —— 从训练语料学习「按对手职业的起手留牌」。

数据: data/training/<卡组>/*.jsonl(首行 _meta + 归一化事件流, corpus.py 产出)。
我方判定用 config.battletag; 对手职业统一从事件流的英雄实体 CLASS 标签提取
(旧样本 _meta.heroes 大面积缺失, 事件流永远可靠); 幸运币不参与留牌决策。

模型(双层, 设计与偏差讨论见 docs/MULLIGAN_AI.md):
  1) 平滑统计表 —— (卡牌 × 对手职业 × 先/后手) 的 留/换 胜负计数, 三级级联
     (同先手行 → 本职业行 → 全体) + Beta 平滑 + 费用启发先验。零依赖、
     可解释 —— 每张卡结论的直接依据。
  2) 逻辑回归胜率模型(可选, sklearn) —— 每局一行: 手牌 onehot + 留牌 onehot
     + 先手/对手职业; 时序留出评估 AUC/LogLoss; 贪心反事实翻位出建议留牌集。
     系数落 JSON, 推理不依赖 sklearn。

增量: 每次 train 全量重扫语料(每局只读 _meta + 英雄段, 成本恒小, 无状态漂移),
对比上一版 games_seen 报告新增局数; 产物落版本目录 vNNN + LATEST.json。

用法:
  python scripts/train_mulligan.py                     # 训练+报告(config 默认卡组)
  python scripts/train_mulligan.py report              # 查看已保存模型
  python scripts/train_mulligan.py advise --vs 圣骑士 --coin \
      --hand 交易馆长,危机,JAIL_718
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
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
BETA_PRIOR_N = 2.0     # Beta 平滑虚拟样本数(以全局胜率为先验均值)
SHRINK_N = 4.0         # 数据向费用启发先验收缩: 权重 n/(n+SHRINK_N)
N_COIN_CELL = 3        # (职业×先手) 格子最少样本, 否则级联回本职业行
N_CLASS_CELL = 5       # 本职业格最少样本, 否则级联回全体
N_ADVICE_MIN = 6       # 低于此样本只报"样本不足"
GAIN_KEEP = 0.03       # 增益 ≥ +3% → 建议留
GAIN_DROP = -0.03      # 增益 ≤ -3% → 建议换
LR_MIN_GAMES = 20      # 语料低于此局数不训逻辑回归
LR_AUC_GATE = 0.55     # LR 时序留出 AUC 低于此值时, 建议权归统计表
LR_TEST_FRAC = 0.2     # 时序留出比例


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


# ════════════════════ 2. 平滑统计表 ════════════════════
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


def cost_prior(cid: str, carddb: CardDB) -> float:
    """费用启发先验(弱): 低费倾向留、高费倾向换。样本上来后数据主导。"""
    cost = carddb.cost(cid)
    if cost is None:
        return 0.0
    if cost <= 2:
        return 0.03
    if cost == 3:
        return 0.0
    return max(-0.15, -(cost - 3) * 0.04)


def _wr(side: dict, prior_wr: float) -> float:
    n = side["w"] + side["l"]
    return (side["w"] + BETA_PRIOR_N * prior_wr) / (n + BETA_PRIOR_N)


def card_advice(stats: dict, cid: str, opp_class: str, coin: int | None,
                deck_wr: float, carddb: CardDB) -> dict:
    """逐卡留牌增益 = P(胜|留) - P(胜|换); 级联取样本最足的格子。"""
    cascade = [("*|*", 0, "全体")]
    if opp_class != "*":
        cascade.insert(0, (f"{opp_class}|*", N_CLASS_CELL, "本职业"))
        if coin is not None:
            cascade.insert(0, (f"{opp_class}|{coin}", N_COIN_CELL, "同先手"))
    ent = stats.get(cid) or {}
    cell, src = None, None
    for key, min_n, label in cascade:
        c = ent.get(key)
        if c and _cell_n(c) >= max(min_n, 1):
            cell, src = c, label
            break
    if cell is None:                              # 完全无数据: 纯费用先验
        return {"n": 0, "gain": cost_prior(cid, carddb), "prior": True,
                "src": "无数据", "label": "样本不足", "keep_wr": None,
                "drop_wr": None, "keep_n": 0, "drop_n": 0}
    nk, nd = sum(cell["keep"].values()), sum(cell["drop"].values())
    keep_wr, drop_wr = _wr(cell["keep"], deck_wr), _wr(cell["drop"], deck_wr)
    w = min(nk, nd) / (min(nk, nd) + SHRINK_N)
    gain = w * (keep_wr - drop_wr) + (1 - w) * cost_prior(cid, carddb)
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


# ════════════════════ 3. 逻辑回归(sklearn 训练 / 纯 JSON 推理) ════════════════════

def _lr_features(vocab: list, classes: list, cards: list, kept: list,
                 coin: bool, opp_class: str) -> list:
    nc = len(vocab)
    x = [0.0] * (2 * nc + 1 + len(classes))
    for cid in cards:
        if cid in vocab:
            x[vocab.index(cid)] = 1.0
    for cid in kept:
        if cid in vocab:
            x[nc + vocab.index(cid)] = 1.0
    x[2 * nc] = 1.0 if coin else 0.0
    if opp_class in classes:
        x[2 * nc + 1 + classes.index(opp_class)] = 1.0
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


def _fit_eval(games: list) -> dict | None:
    """时序留出评估(前 80% 拟合 → 后 20% 打分), 返回测试段指标。"""
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    n_test = max(1, int(len(games) * LR_TEST_FRAC))
    tr, te = games[:-n_test], games[-n_test:]
    vocab = sorted({c for g in games for c in g.cards})
    classes = sorted({g.opp_class for g in games if g.opp_class != UNKNOWN})
    xof = lambda g: _lr_features(vocab, classes, g.cards, g.kept, g.coin, g.opp_class)
    ytr = [g.result for g in tr]
    if len(set(ytr)) < 2:
        return None
    try:
        m = LogisticRegression(C=0.3, max_iter=2000).fit([xof(g) for g in tr], ytr)
        pte = m.predict_proba([xof(g) for g in te])[:, 1]
        yte = [g.result for g in te]
    except ValueError:
        return None
    out = {"n_train": len(tr), "n_test": len(te),
           "test_auc": _auc(yte, list(pte))}
    if 0 < sum(yte) < len(yte):
        ll = -sum(y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9))
                  for y, p in zip(yte, pte)) / len(yte)
        out["test_logloss"] = round(ll, 3)
    if out["test_auc"] is not None:
        out["test_auc"] = round(out["test_auc"], 3)
    return out


def train_lr(games: list) -> dict | None:
    """全量拟合出服务模型; 指标来自时序留出(不泄漏)。"""
    if len(games) < LR_MIN_GAMES:
        return None
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    metrics = _fit_eval(games)
    vocab = sorted({c for g in games for c in g.cards})
    classes = sorted({g.opp_class for g in games if g.opp_class != UNKNOWN})
    xof = lambda g: _lr_features(vocab, classes, g.cards, g.kept, g.coin, g.opp_class)
    try:
        model = LogisticRegression(C=0.3, max_iter=2000).fit(
            [xof(g) for g in games], [g.result for g in games])
    except ValueError:
        return None
    return {"vocab": vocab, "classes": classes, "coef": model.coef_[0].tolist(),
            "intercept": float(model.intercept_[0]),
            "metrics": metrics or {}}


def _lr_p(model: dict, cards: list, kept: list, coin: bool,
          opp_class: str) -> float:
    x = _lr_features(model["vocab"], model["classes"], cards, kept,
                     coin, opp_class)
    z = model["intercept"] + sum(w * v for w, v in zip(model["coef"], x))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def lr_advice(model: dict, cards: list, coin: bool, opp_class: str) -> list:
    """贪心反事实翻位: 从全换出发, 留使 P(胜) 提高的卡, 迭代到不动点。"""
    cards = sorted(set(cards), key=cards.index)   # 去重保序
    keep: list = []
    for _ in range(3):
        changed = False
        for cid in cards:                     # 加入增益为正的卡
            if cid in keep:
                continue
            base = _lr_p(model, cards, keep, coin, opp_class)
            if _lr_p(model, cards, keep + [cid], coin, opp_class) > base:
                keep = sorted(keep + [cid], key=cards.index)
                changed = True
        for cid in list(keep):                # 复核: 不再增益的退出
            without = [c for c in keep if c != cid]
            if _lr_p(model, cards, without, coin, opp_class) >= \
                    _lr_p(model, cards, keep, coin, opp_class):
                keep.remove(cid)
                changed = True
        if not changed:
            break
    return keep


# ════════════════════ 4. 模型库(版本目录 + LATEST) ════════════════════

def models_root(cfg: Config, deck: str) -> Path:
    safe = "".join("_" if c in '\\/:*?"<>|' else c for c in deck).strip() or "未知卡组"
    return Path(cfg.data_dir) / "models" / "mulligan" / safe


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
        mpath = vdir / "model.json"
        art["lr"] = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else None
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


# ════════════════════ 5. 报告渲染 ════════════════════

def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def print_table_report(stats: dict, deck_wr: float, carddb: CardDB) -> None:
    """逐卡留牌增益表(以样本最多的对手职业做级联查询)。"""
    by_n = lambda cid: _cell_n(stats[cid].get("*|*", _new_cell()))
    vocab = sorted(stats, key=lambda c: (-by_n(c), c))
    cls_counts = Counter(k.split("|", 1)[0]
                         for cid in stats for k in stats[cid] if k != "*|*")
    main_class = cls_counts.most_common(1)[0][0] if cls_counts else UNKNOWN
    print(f"── 单卡留牌增益(平滑统计表, 优先职业: {class_zh(main_class)}) ──")
    print(f"{'卡牌':<14} {'格子':<5} {'样本':>4} {'留牌率':>7} {'留→胜':>7} "
          f"{'换→胜':>7} {'增益':>8}  结论")
    for cid in vocab:
        adv = card_advice(stats, cid, main_class, None, deck_wr, carddb)
        n_offer = adv["keep_n"] + adv["drop_n"]
        kept_rate = adv["keep_n"] / n_offer if n_offer else None
        name = (carddb.name(cid) or cid)[:12]
        note = "" if adv["prior"] else f"(n={adv['n']})"
        print(f"{name:<14} {adv['src']:<5} {n_offer:>4} {_pct(kept_rate):>7} "
              f"{_pct(adv['keep_wr']):>7} {_pct(adv['drop_wr']):>7} "
              f"{adv['gain'] * 100:>+7.1f}%  {adv['label']}{note}")


# ════════════════════ 6. 子命令 ════════════════════

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
    lr = train_lr(games)

    print(f"── 留牌模型训练 ({deck}) ──")
    print(f"语料 {len(games)} 局(较上一版新增 {n_new}) │ 我方胜率 {_pct(deck_wr)} "
          f"│ 先手 {sum(not g.coin for g in games)} / 后手 "
          f"{sum(g.coin for g in games)} │ 对手: "
          + " ".join(f"{class_zh(k)}×{v}" for k, v in
                     Counter(g.opp_class for g in games).most_common()))
    if skip:
        print("跳过: " + " │ ".join(f"{k} {v}" for k, v in skip.items()))
    print_table_report(stats, deck_wr, carddb)
    if lr is not None:
        m = lr["metrics"]
        print(f"── 逻辑回归(sklearn): {m.get('n_train', '?')} 局训练 / "
              f"{m.get('n_test', '?')} 局时序留出 │ AUC {m.get('test_auc', '—')} │ "
              f"LogLoss {m.get('test_logloss', '—')} ──")
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
    print(f"── 留牌模型 {ver} ({deck}) ──")
    print(f"训练于 {meta['trained_at']} │ {meta['n_games']} 局(当批新增 "
          f"{meta['n_new']}) │ 我方胜率 {_pct(meta['wins'] / meta['n_games'])} │ "
          f"对手: " + " ".join(f"{class_zh(k)}×{v}" for k, v in
                               (meta.get("class_counts") or {}).items()))
    if meta.get("lr_metrics"):
        print(f"逻辑回归指标: {meta['lr_metrics']}")
    print_table_report(stats, art["stats"].get("deck_wr", 0.5), carddb)
    print(f"模型目录: {root / ver}")
    return 0


def cmd_advise(cfg: Config, deck: str, hand: list, opp_class: str,
               coin: int | None) -> int:
    root = models_root(cfg, deck)
    ver, art = _load_latest(root)
    if ver is None:
        print(f"尚无已训练模型: {root}(先运行 train)")
        return 1
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    stats = art["stats"].get("cards", {})
    lr = art.get("lr")
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

    print(f"── 留牌建议 ({deck}, 模型 {ver}) ──")
    coin_txt = "后手(有幸运币)" if coin == 1 else "先手" if coin == 0 else "不限先/后手"
    print(f"对手: {class_zh(cls)} │ {coin_txt}")
    cost = lambda c: carddb.cost(c)
    print("起手: " + "、".join(
        f"{carddb.name(c)}({'?' if cost(c) is None else cost(c)}费)" for c in cards))
    advs = [(c, card_advice(stats, c, cls, coin, deck_wr, carddb)) for c in cards]
    # LR 只有通过时序留出验证(AUC ≥ LR_AUC_GATE)才对最终建议有拍板权,
    # 否则降为参考(59 局量级下 LR 常是噪声, 统计表更稳)。
    auc = ((lr or {}).get("metrics") or {}).get("test_auc")
    lr_ok = lr is not None and auc is not None and auc >= LR_AUC_GATE
    if lr_ok:
        if coin is None:                     # 不限先/后手: 取两个手都建议留的交集
            keep_set = set(lr_advice(lr, cards, False, cls)) & \
                set(lr_advice(lr, cards, True, cls))
            basis = f"LR(AUC {auc:.2f}, 先/后手交集) + 统计表"
        else:
            keep_set = set(lr_advice(lr, cards, bool(coin), cls))
            basis = f"LR(AUC {auc:.2f}) + 统计表"
    else:
        keep_set = {c for c, a in advs if a["label"] == "建议留"}
        basis = "统计表" + (f"(LR AUC {auc:.2f} 过低, 仅参考)"
                            if lr is not None and auc is not None else "")
    nm = lambda c: carddb.name(c) or c
    print(f"建议: 留 ── {'、'.join(nm(c) for c in cards if c in keep_set) or '(无)'} │ "
          f"换 ── {'、'.join(nm(c) for c in cards if c not in keep_set) or '(无)'}"
          f"   [依据: {basis}]")
    print("逐卡(留→胜率 vs 换→胜率, 平滑增益):")
    for c, a in advs:
        print(f"  {nm(c):<14} {a['label']:<5} 留→{_pct(a['keep_wr'])} "
              f"换→{_pct(a['drop_wr'])} 增益{a['gain'] * 100:+.1f}% "
              f"(n={a['n']}, {a['src']})")
    return 0


# ════════════════════ 7. CLI ════════════════════

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
    return cmd_advise(cfg, deck, hand, args.vs, coin)


if __name__ == "__main__":
    sys.exit(main())
