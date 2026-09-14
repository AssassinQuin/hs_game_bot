"""留牌评分器(v3 阶段二) —— 决策时特征 → P(胜) + 蒸馏系数(设计 §5-§7)。

训练行 = 每局一行(特征含该局真实留集, 标签是该局结果/Q 蒸馏); 反事实候选集
只在推理端枚举(spec §5.2 禁令——给未发生的留集复制标签 = 假信号注入)。
蒸馏: 把基座对候选空间的打分面最小二乘分解为 Σgain + Σsyn(目标 = 基座自己的
打分, 绝不触碰真实胜负), live 军师查表毫秒出建议(零 torch)。
"""
from __future__ import annotations

import re
from collections import Counter
from itertools import combinations

from hsbot.mulligan_ai import mulligan3_features, v3_layout_offsets
from trainer.mulligan import LR_PAIR_SUPPORT

# 选定的超参数(看数据前定下; 沿用 v2 同款哲学)
LR_C = 0.3                 # sklearn 逻辑回归正则(v2 同款)
N_SPLITS = 5               # 组级 CV 折数(backtest.group_folds)

# 后端注册表(spec §7): 训练时都跑对照, AUC 谁高谁产出蒸馏系数
_BACKENDS = ("tabpfn_v2", "tabicl_v2")


def _backend_available(name: str) -> bool:
    from hsbot.mulligan_ai import tabpfn_env
    tabpfn_env(".")
    try:
        if name == "tabpfn_v2":
            import tabpfn  # noqa: F401
        elif name == "tabicl_v2":
            import tabicl  # noqa: F401
        else:
            return False
        return True
    except ImportError:
        return False


# ════════════════════ 数据集构建 ════════════════════

def scorer_dataset(rows_v2: list[dict], carddb, prior: dict,
                   q_by_game: dict[str, float] | None = None,
                   q_gated: bool = False) -> dict:
    """v3 素材 → 评分器数据集(每局 1 行)。
    y = 0.5·result + 0.5·Q₁₎₃(q_gated 且该局有锚点), 否则纯 result(设计 §5.2)。"""
    mull = [r for r in rows_v2 if r.get("row") == "mulligan"]
    if not mull:
        raise SystemExit("v3 素材无决策行(先运行 material-v2)")
    deck_n: Counter = Counter()
    for r in mull:
        deck_n.update(r.get("decklist") or {})
    # 卡组词表 = 各局 decklist 并集(常见优先, 稳定小词表); offered 天然 ⊆ 词表
    vocab = [c for c, _ in sorted(deck_n.items(),
                                  key=lambda kv: (-kv[1], kv[0])) if c]
    classes = sorted({r["opp_class"] for r in mull})
    kept_pair_n: Counter = Counter()
    for r in mull:
        ks = sorted(set(r["kept"]))
        for a, b in combinations(ks, 2):
            kept_pair_n[(a, b)] += 1
    pairs = [list(p) for p, n in sorted(kept_pair_n.items())
             if n >= LR_PAIR_SUPPORT]
    engines = sorted(set(prior.get("engine") or []))

    X: list = []
    y: list = []
    games: list = []
    tails: list = []
    deck_engines: list = []
    offered_l: list = []
    kept_l: list = []
    coin_l: list = []
    opp_l: list = []
    for r in mull:
        decklist = r.get("decklist") or {}
        deck_engine = sum(n for c, n in decklist.items() if c in engines)
        tail = _deck_tail(decklist, carddb, prior)
        model = {"vocab": vocab, "classes": classes, "pairs": pairs,
                 "engine": engines, "cost": carddb.cost,
                 "cardtype": carddb.cardtype, "deck_tail": tail,
                 "deck_engine": deck_engine}
        X.append(mulligan3_features(model, r["offered"], r["kept"],
                                    bool(r["coin"]), r["opp_class"]))
        result = float(r["result"])
        qv = (q_by_game or {}).get(r["game"])
        y.append(0.5 * result + 0.5 * qv
                 if (q_gated and qv is not None) else result)
        games.append(r["game"])
        tails.append(tail)
        deck_engines.append(deck_engine)
        offered_l.append(r["offered"])
        kept_l.append(r["kept"])
        coin_l.append(bool(r["coin"]))
        opp_l.append(r["opp_class"])
    # 组 = 会话(切片名剥 _gNN.power.log 后缀): 同会话绝不跨训练/验证组
    sess = lambda g: re.sub(r"_g\d+(\.power\.log)?$", "", g.split("#")[0])
    return {"X": X, "y": y, "groups": [sess(g) for g in games],
            "games": games, "tails": tails, "deck_engines": deck_engines,
            "offered": offered_l, "kept": kept_l, "coin": coin_l,
            "opp": opp_l, "vocab": vocab, "classes": classes,
            "pairs": pairs, "engines": engines,
            "cost": carddb.cost, "cardtype": carddb.cardtype}


def _deck_tail(decklist: dict | None, carddb, prior: dict) -> list:
    from trainer.mulligan import deck_features
    return deck_features(decklist or {}, carddb, prior)[0]


# ════════════════════ 后端拟合 + 组级 CV 对照 ════════════════════

def _fit_backend(name: str, Xtr, ytr, data_dir):
    """backend → predict(X)→[P(胜)]; 缺包/坏折 → None。"""
    from hsbot.mulligan_ai import tabpfn_env
    tabpfn_env(data_dir)
    try:
        import numpy as np
        if name == "tabpfn_v2":
            from tabpfn import TabPFNClassifier
            clf = TabPFNClassifier(device="cpu",
                                   ignore_pretraining_limits=True)
        elif name == "tabicl_v2":
            from tabicl import TabICLClassifier
            clf = TabICLClassifier()
        elif name == "lr":
            from sklearn.linear_model import LogisticRegression
            return _fit_lr(Xtr, ytr)
        else:
            return None
    except ImportError:
        return None
    clf.fit(np.array(Xtr, dtype=np.float32), np.array(ytr, dtype=int))
    return lambda Xv: clf.predict_proba(
        np.array(Xv, dtype=np.float32))[:, 1]


def _fit_lr(Xtr, ytr):
    from sklearn.linear_model import LogisticRegression
    m = LogisticRegression(C=LR_C, max_iter=2000).fit(Xtr, ytr)
    return lambda Xv: m.predict_proba(Xv)[:, 1]


def fit_scorer(ds: dict, data_dir, backend: str = "tabpfn_v2"):
    """全量拟合(服务蒸馏); 偏好后端缺失 → 另一基座 → LR(设计 §7 级联)。"""
    order = [b for b in (backend,) + _BACKENDS + ("lr",) if b != backend]
    for name in (backend, *order):
        predict = _fit_backend(name, ds["X"], ds["y"], data_dir)
        if predict is not None:
            return name, predict
    return None, None


def cv_compare(ds: dict, data_dir, pref: str = "tabpfn_v2") -> tuple:
    """组级 CV 对照表(设计 §9.1): 基座×2 / 逻辑回归 / 统计表加性基线。
    → (report, winner_machine_key, winner 全量 predict | None)。"""
    from hsbot.mulligan_ai import card_advice, table_update
    from .backtest import auc as auc_of, group_folds
    X, y = ds["X"], ds["y"]
    names = list(_BACKENDS) + ["lr"]
    scores: dict[str, list] = {n: [] for n in names}
    table_scores: list = []
    for tr, va in group_folds(ds["groups"], N_SPLITS):
        ytr = [y[i] for i in tr]
        if len(set(ytr)) < 2 or len({y[i] for i in va}) < 2:
            continue
        yva = [y[i] for i in va]
        for name in names:
            if name != "lr" and not _backend_available(name):
                continue
            predict = _fit_backend(name, [X[i] for i in tr], ytr, data_dir)
            if predict is None:
                continue
            p = [float(v) for v in predict([X[i] for i in va])]
            scores[name].append(auc_of(yva, p))
        # 统计表加性基线: 折内统计表 → 逐卡增益加性和(秩可比, 非概率)
        stats: dict = {}
        for i in tr:
            table_update(stats, ds["opp"][i], ds["coin"][i],
                         1 if y[i] >= 0.5 else 0, ds["offered"][i],
                         ds["kept"][i])
        deck_wr = sum(1 for i in tr if y[i] >= 0.5) / max(1, len(tr))
        sv = []
        for i in va:
            g = {c: card_advice(stats, c, ds["opp"][i],
                                1 if ds["coin"][i] else 0, deck_wr,
                                None, None)["gain"]
                 for c in ds["kept"][i]}
            sv.append(sum(g.values()))
        table_scores.append(auc_of(yva, sv))
    report: dict = {"统计表加性": {
        "auc": round(sum(table_scores) / len(table_scores), 3)
        if table_scores else None, "folds": len(table_scores)}}
    display = {"tabpfn_v2": "TabPFN v2", "tabicl_v2": "TabICL v2", "lr": "LR"}
    for name in names:
        aucs = [a for a in scores[name] if a is not None]
        if scores[name]:
            report[display[name]] = {
                "auc": round(sum(aucs) / len(aucs), 3) if aucs else None,
                "folds": len(aucs)}
    # 胜者 = 偏好后端可导入则用之, 否则另一基座, 再否则 LR(spec §7)
    winner = "lr"
    for name in (pref, *[b for b in _BACKENDS if b != pref], "lr"):
        if name == "lr" or _backend_available(name):
            winner = name
            break
    full = _fit_backend(winner, X, y, data_dir) if winner != "lr" \
        else (_fit_lr(X, y) if _sklearn_ok() else None)
    return report, winner, full


def _sklearn_ok() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


# ════════════════════ 蒸馏系数(纯 Python 最小二乘, 零 torch) ════════════════════

def _solve_lstsq(D: list, t: list, intercept: float = 0.0) -> list:
    """正规方程 (DᵀD)w = Dᵀ(t − 截距) + 列主元高斯消元(列数 ≤ ~60, 纯 stdlib)。"""
    ncol = len(D[0]) if D else 0
    target = [ti - intercept for ti in t]
    a = [[sum(row[i] * row[j] for row in D) for j in range(ncol)]
         + [sum(row[i] * ti for row, ti in zip(D, target))]
         for i in range(ncol)]
    for col in range(ncol):                       # 列主元消元
        piv = max(range(col, ncol), key=lambda r: abs(a[r][col]))
        a[col], a[piv] = a[piv], a[col]
        if abs(a[col][col]) < 1e-12:
            a[col][col] = 1e-12                   # 奇异列(共现=0 的 pair)置零解
        for r in range(ncol):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for c in range(col, ncol + 1):
                a[r][c] -= f * a[col][c]
    return [a[i][ncol] / a[i][i] for i in range(ncol)]


def distill(ds: dict, predict) -> tuple[dict, dict, float]:
    """基座打分面 → 加性系数(gain/syn) + top-1 一致率(设计 §6)。
    目标 = predict(候选集特征) —— 基座自己的打分面, 不触碰真实胜负。"""
    shared = {"vocab": ds["vocab"], "classes": ds["classes"],
              "pairs": ds["pairs"], "engine": ds["engines"],
              "cost": ds["cost"], "cardtype": ds["cardtype"]}
    off = v3_layout_offsets(ds["vocab"], ds["pairs"])
    nv, npair = len(ds["vocab"]), len(ds["pairs"])
    gain: dict = {}
    gain_n: Counter = Counter()          # 只在该卡被 offering 的局间平均
    syn: dict = {}
    syn_n: Counter = Counter()
    match = 0
    total = 0
    for i, offered in enumerate(ds["offered"]):
        model = {**shared, "deck_tail": ds["tails"][i],
                 "deck_engine": ds["deck_engines"][i]}
        uniq = sorted(set(offered))
        cands = [set(c) for k in range(len(uniq) + 1)
                 for c in combinations(uniq, k)]
        feats = [mulligan3_features(model, offered, sorted(c),
                                    ds["coin"][i], ds["opp"][i])
                 for c in cands]
        ps = [float(v) for v in predict(feats)]
        # 设计矩阵 = [vocab 指示 | pair 指示]; 目标 = 基座打分 → 最小二乘
        # 首列自由截距(基率 p(空集)), 解出后丢弃——增益/协同只留边际贡献,
        # 与 best_keep_set 的"集合间比较不受常数平移影响"口径一致
        D = [[1.0] + [1.0 if ds["vocab"][j] in c else 0.0 for j in range(nv)]
             + [1.0 if a in c and b in c else 0.0
                for a, b in ds["pairs"]] for c in cands]
        w = _solve_lstsq(D, ps, intercept=0.0)
        for j, c in enumerate(ds["vocab"]):
            if c in uniq:                        # 未 offering 的卡该局不可辨识
                gain[c] = gain.get(c, 0.0) + w[1 + j]
                gain_n[c] += 1
        for j, (a, b) in enumerate(ds["pairs"]):
            if a in uniq and b in uniq:
                syn[f"{a}|{b}"] = syn.get(f"{a}|{b}", 0.0) + w[1 + nv + j]
                syn_n[f"{a}|{b}"] += 1
        base_top = cands[max(range(len(ps)), key=lambda k: ps[k])]
        # 加性近似分 = D·w(跳过截距列): 与 live best_keep_set 同一打分口径
        add_scores = [sum(row[1 + j] * w[1 + j] for j in range(nv + npair))
                      for row in D]
        add_top = cands[max(range(len(add_scores)),
                            key=lambda k: add_scores[k])]
        match += int(base_top == add_top)
        total += 1
    return ({c: gain[c] / gain_n[c] for c in gain if gain_n[c]},
            {k: syn[k] / syn_n[k] for k in syn if syn_n[k]},
            match / total if total else 0.0)


# ════════════════════ v3 训练编排(material_v2 → Q → 评分器 → 蒸馏) ════════════════════

_MIN_MULL_ROWS = 10        # 决策行少于此值不开训(小样本诚实条款, 与 LR_MIN_GAMES 同哲学)


def train_v3(cfg, deck: str, carddb, prior: dict) -> dict | None:
    """v3 全流程 → v3.json 内容(设计 §3-§7); 决策行不足/基座缺失 → None。
    失败不抛(调用方已兜底), 阶段门控逐级诚实降级。"""
    from pathlib import Path

    from hsbot.mulligan_ai import ANTI_SYNERGY_THR, LR_AUC_GATE, V3_LAYOUT
    from .material import build_material_v2, load_material_v2
    from . import qvalue

    out_dir = Path(__file__).resolve().parent / "data" / deck
    corpus_dir = Path(cfg.training_dir)
    slices = sorted((corpus_dir / deck).glob("*.power.log"))
    mv2 = out_dir / "material_v2.jsonl"
    # 成本闸: 素材比全部切片新则复用(局终自动训练不重复全量重放)
    if mv2.exists() and slices \
            and mv2.stat().st_mtime > max(s.stat().st_mtime for s in slices):
        rows = load_material_v2(out_dir)
    else:
        build_material_v2(corpus_dir, deck, out_dir, carddb, cfg.battletag)
        rows = load_material_v2(out_dir)
    n_mull = sum(1 for r in rows if r.get("row") == "mulligan")
    if n_mull < _MIN_MULL_ROWS:
        print(f"v3: 决策行不足({n_mull} < {_MIN_MULL_ROWS}), 跳过")
        return None

    # 阶段一: Q 模型 OOF + 门控(不过 → 阶段二标签退化纯胜负)
    Xq, yq, gq, q3idx, _meta = qvalue.q_rows(rows, carddb, prior)
    qm = qvalue.q_oof_fit(Xq, yq, gq, cfg.data_dir)
    q_gated = qvalue.q_gate_passes(qm)
    q_by_game = {g: qm["oof"][i] for g, i in q3idx.items() if i in qm["oof"]}

    # 阶段二: 评分器(蒸馏标签) + 对照表 + 蒸馏系数
    ds = scorer_dataset(rows, carddb, prior, q_by_game, q_gated)
    report, winner, _full = cv_compare(ds, cfg.data_dir, cfg.scorer_backend)
    backend, predict = fit_scorer(ds, cfg.data_dir, cfg.scorer_backend)
    if predict is None:
        print("v3: 基座后端不可用(缺 tabpfn), 跳过")
        return None
    gain, syn, agree = distill(ds, predict)
    display = {"tabpfn_v2": "TabPFN v2", "tabicl_v2": "TabICL v2", "lr": "LR"}
    auc = (report.get(display.get(winner, ""), {}) or {}).get("auc")
    distill_ok = (agree >= float(getattr(cfg, "distill_min_agree", 0.9))
                  and auc is not None and auc >= LR_AUC_GATE)
    # live 用系数表不消费 deck_tail; CLI 基座枚举取训练局众数尾段(单卡组前提)
    tail_mode = Counter(tuple(t) for t in ds["tails"]).most_common(1)
    return {"layout": V3_LAYOUT, "backend": backend,
            "vocab": ds["vocab"], "classes": ds["classes"],
            "pairs": ds["pairs"], "engine": ds["engines"],
            "gain": gain, "syn": syn,
            "agree": round(agree, 3), "auc": auc, "q_auc": qm["auc"],
            "q_gated": q_gated, "distill_ok": distill_ok,
            "anti_thr": ANTI_SYNERGY_THR,
            "deck_tail": list(tail_mode[0][0]) if tail_mode else [],
            "deck_engine": float(Counter(ds["deck_engines"]).most_common(1)[0][0])
            if ds["deck_engines"] else 0.0,
            "X": ds["X"], "y": ds["y"]}
