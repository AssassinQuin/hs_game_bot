"""回测 —— 分组交叉验证: 同一局的样本绝不跨训练/验证组(防同局泄漏虚高)。

留牌每局 1 行(组=会话, 同会话连局的对手/手感相关); 价值素材每回合 1 行
(组=局)。报告各模型 mean±std AUC, 与"恒猜多数"基线对照 —— 低于基线的
模型 = 噪声, 一目了然。
"""
from __future__ import annotations

import random
from collections import defaultdict
from statistics import mean, stdev


def group_folds(groups: list, n_splits: int = 5, seed: int = 0) -> list:
    """按组切 n 折(同一组的样本永不跨折), 返回 [(train_idx, valid_idx)]。"""
    uniq = sorted(set(groups))
    random.Random(seed).shuffle(uniq)
    folds: list[set] = [set() for _ in range(n_splits)]
    for i, g in enumerate(uniq):
        folds[i % n_splits].add(g)
    out = []
    for f in folds:
        tr = [i for i, g in enumerate(groups) if g not in f]
        va = [i for i, g in enumerate(groups) if g in f]
        if tr and va:
            out.append((tr, va))
    return out


def auc(y_true: list, scores: list) -> float | None:
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


def cv_auc(models: dict, X: list, y: list, groups: list,
           n_splits: int = 5, seed: int = 0) -> dict:
    """models = {名称: fit函数(X子集,y子集)→打分器}; 打分器(X子集)→[0,1] 概率。
    返回 {名称: {"aucs": [每折], "mean": x, "std": x, "folds": 有效折数}}。"""
    scores = defaultdict(list)
    for tr, va in group_folds(groups, n_splits, seed):
        ytr = [y[i] for i in tr]
        if len(set(ytr)) < 2 or len(set(y[i] for i in va)) < 2:
            continue
        for name, fit in models.items():
            predict = fit([X[i] for i in tr], ytr)
            if predict is None:                      # 该折不可训(缺类/缺包)
                continue
            scores[name].append(auc([y[i] for i in va],
                                    list(predict([X[i] for i in va]))))
    return {name: {"aucs": [round(a, 3) for a in aucs],
                   "mean": round(mean(aucs), 3) if aucs else None,
                   "std": round(stdev(aucs), 3) if len(aucs) > 1 else 0.0,
                   "folds": len(aucs)}
            for name, aucs in scores.items()}


def print_cv_report(title: str, report: dict, baseline: str = "恒猜多数") -> None:
    print(f"── {title} ──")
    print(f"{'模型':<22} {'折数':>4} {'AUC均值':>8} {'波动':>7}   各折")
    for name, r in report.items():
        mark = " ←基线" if name == baseline else ""
        print(f"{name:<22} {r['folds']:>4} "
              f"{('%5.3f' % r['mean']) if r['mean'] is not None else '   —':>8} "
              f"±{r['std']:.3f}  {r['aucs']}{mark}")
