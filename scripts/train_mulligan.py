"""留牌 AI 训练器 —— 从训练语料学习「按对手职业的起手留牌」。

数据: data/training/<卡组>/*.jsonl(首行 _meta + 归一化事件流, corpus.py 产出)。
我方判定用 config.battletag; 对手职业统一从事件流的英雄实体 CLASS 标签提取
(旧样本 _meta.heroes 大面积缺失, 事件流永远可靠); 幸运币不参与留牌决策。

职责分工: 结论的纯函数(统计表/先验/集合枚举/Thompson/匹配证据)在
hsbot/mulligan_ai.py —— 训练器与实时军师(hsbot)共用; 本脚本只负责
训练侧: 语料提取、逻辑回归拟合(skearn)、版本化落盘与 CLI。

增量: 每次 train 全量重扫语料(每局只读 _meta+英雄段, 成本恒小, 无状态漂移),
对比上一版 games_seen 报告新增局数; 产物落版本目录 vNNN + LATEST.json,
MulliganAdvisor 按_mtime 自动切换新版本。

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
from hsbot.mulligan_ai import (CLASS_ZH, MulliganAdvisor, UNKNOWN,
                               best_keep_set, card_advice, cell_n, class_zh,
                               hero_class, is_coin, load_prior, lr_features,
                               matched_evidence, models_root_for, new_cell,
                               read_latest, thompson_gain)
from hsbot.mulligan_ai import table_update as _table_update_ai

LR_MIN_GAMES = 20      # 语料低于此局数不训逻辑回归(训练专用)
LR_TEST_FRAC = 0.2     # 时序留出比例(训练专用)
LR_PAIR_SUPPORT = 4    # 留牌组合特征最少共现次数(训练专用)


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
            hero_class((meta.get("heroes") or {}).get(opp_pid))
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


def table_update(stats: dict, g: Game) -> None:
    """(适配 Game) 一局留牌事实计入统计表。"""
    _table_update_ai(stats, g.opp_class, g.coin, g.result, g.cards, g.kept)

# ════════════════════ 2. 逻辑回归训练(sklearn) ════════════════════

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
    xof = lambda g: lr_features(vocab, classes, pairs, g.cards, g.kept,
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
    xof = lambda g: lr_features(vocab, classes, pairs, g.cards, g.kept,
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


# ════════════════════ 3. 模型库(版本目录 + LATEST) ════════════════════

def models_root(cfg: Config, deck: str) -> Path:
    return models_root_for(cfg.data_dir, deck)


def prior_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "mulligan_prior.yaml"


def _load_latest(root: Path) -> tuple[str | None, dict]:
    return read_latest(root)


def _save_version(root: Path, deck: str, stats: dict, lr: dict | None,
                  games: list, n_new: int, skip: Counter) -> str:
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


# ════════════════════ 4. 报告渲染 ════════════════════

def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def print_table_report(stats: dict, deck_wr: float, carddb: CardDB,
                       prior: dict) -> None:
    """逐卡留牌增益表(以样本最多的对手职业做级联查询)。"""
    by_n = lambda cid: cell_n(stats[cid].get("*|*", new_cell()))
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


# ════════════════════ 5. 子命令 ════════════════════

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
    if n_new == 0 and prev_ver is not None:
        print(f"无新增对局, 沿用 {prev_ver}: {root / prev_ver}")
        return 0

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
          + ("" if prior["cards"] or prior["pairs"]
             else f"(无 — 可编辑 {prior_path(cfg)})"))
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
    ver = _save_version(root, deck, stats, lr, games, n_new, skip)
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
    ver, _ = _load_latest(root)
    if ver is None:
        print(f"尚无已训练模型: {root}(先运行 train)")
        return 1
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    ids = set(json.loads((root / ver / "stats.json").read_text(
        encoding="utf-8")).get("cards", {}))
    model_path = root / ver / "model.json"
    if model_path.exists():
        ids |= set(json.loads(model_path.read_text(encoding="utf-8")).get("vocab", []))

    # 手牌与对手: card:ID 直给, 或按卡表把中文名反查成 card_id
    name_map = {}
    for cid in ids:
        nm = carddb.name(cid)
        if nm and nm != cid:
            name_map[nm] = cid
    cards = [t[5:] if t.startswith("card:") else name_map.get(t, t)
             for t in hand if t]
    cls = opp_class or "*"
    if cls != "*":
        rev = {zh: en for en, zh in CLASS_ZH.items()}
        cls = rev.get(cls) or hero_class(cls) or \
            (cls.upper() if cls.upper() in CLASS_ZH else cls)

    advisor = MulliganAdvisor(root, deck, carddb,
                              prior_path=prior_path(cfg))
    r = advisor.advise(cards, cls, coin=bool(coin) if coin is not None else False,
                       explore=explore, seed=seed)
    if r is None:
        print("(模型无可用数据)")
        return 1

    print(f"── 留牌建议 ({deck}, 模型 {r['version']}) ──")
    coin_txt = "后手(有幸运币)" if coin == 1 else "先手" if coin == 0 else "不限先/后手"
    print(f"对手: {class_zh(cls)} │ {coin_txt}"
          + (" │ Thompson 探索局" if explore else ""))
    cost = lambda c: carddb.cost(c)
    print("起手: " + "、".join(
        f"{carddb.name(c)}({'?' if cost(c) is None else cost(c)}费)" for c in cards))
    for d in r["deviations"]:
        nm = carddb.name(d["card_id"]) or d["card_id"]
        print(f"探索说明(与均值建议不同, 用于积累反事实样本): {nm}"
              f"(均值{d['mean'] * 100:+.1f}%→抽样{d['sample'] * 100:+.1f}%)")
    if explore and not r["deviations"]:
        print("(本次抽样与均值建议一致, 无探索偏差)")
    nm = lambda c: carddb.name(c) or c
    print(f"建议: 留 ── {'、'.join(nm(c) for c in r['keep']) or '(无)'} │ "
          f"换 ── {'、'.join(nm(c) for c in r['drop']) or '(无)'}"
          f"   [依据: {r['basis']}]")
    print("逐卡(留→胜率 vs 换→胜率, 平滑增益 │ 同情境匹配证据):")
    for cid, a in r["per_card"].items():
        ev_txt = f"│ {a['ev']}" if a["ev"] else "│ 无可比对局"
        print(f"  {a['name']:<14} {a['label']:<5} 留→{_pct(a['keep_wr'])} "
              f"换→{_pct(a['drop_wr'])} 增益{a['gain'] * 100:+.1f}% "
              f"({a['src']}) {ev_txt}")
    return 0


# ════════════════════ 6. CLI ════════════════════

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
