"""Q 模型 —— 第 K 回合局面 → P(胜)(v3 两阶段架构的阶段一, 设计 §4)。

允许回合 K 已发生的信息(逐回合抽牌/换入牌/场面)——它在第 K 回合服务,
那时这些信息已经发生; 与留牌评分器的决策时特征(spec §2 防泄漏铁律)分立。
训练 = 表格基座 in-context(无 epochs); 评估 = 组级时序留出(复用 backtest
的 group_folds, 同局绝不跨组); 门控 AUC ≥ LR_AUC_GATE 才作为蒸馏标签源,
不过门 → 阶段二标签退化为纯胜负(不阻塞)。
"""
from __future__ import annotations

from hsbot.mulligan_ai import LR_AUC_GATE

_AGG_NAMES = ["low_spell", "engine", "cov_1", "cov_2", "cov_3"]


def _agg(cards: list, carddb, engines: set) -> list:
    """牌列表 → 5 维聚合: 低费(≤2)法术数 / 引擎件数 / 1/2/3 费档覆盖。"""
    costs = [carddb.cost(c) for c in cards]
    types = [carddb.cardtype(c) for c in cards]
    low_spell = sum(1 for c, t in zip(cards, types)
                    if t == "SPELL" and carddb.cost(c) is not None
                    and carddb.cost(c) <= 2)
    n_engine = sum(1 for c in cards if c in engines)
    cov = [1.0 if k in [c for c in costs if isinstance(c, int)] else 0.0
           for k in (1, 2, 3)]
    return [float(low_spell), float(n_engine)] + cov


def q_rows(rows_v2: list[dict], carddb, prior: dict) -> tuple:
    """v3 素材行 → Q 样本。→ (X, y, groups, q3_index_by_game, meta)。

    特征 = flatten(K 回合快照) + 前 K 回合抽牌聚合(turn ≤ K, 含本回合)
    + 换入牌聚合 + 对手职业 onehot + deck_features 尾段(布局单点)。
    q3_index_by_game = {game: 行下标}: 该局 turn==3 行(缺则 turn==2)。"""
    from trainer.mulligan import deck_features
    from trainer.states import flatten

    engines = set(prior.get("engine") or [])
    mull = {r["game"]: r for r in rows_v2 if r.get("row") == "mulligan"}
    classes = sorted({r["opp_class"] for r in mull.values()})
    tail_names = deck_features(None, carddb, prior)[1]
    names = list(flatten(_snap(2))[1]) \
        + [f"draw_{n}" for n in _AGG_NAMES] \
        + [f"repl_{n}" for n in _AGG_NAMES] \
        + [f"opp_{c}" for c in classes] + tail_names

    X: list = []
    y: list = []
    groups: list = []
    q3: dict = {}
    by_game: dict = {}
    for r in rows_v2:
        if r.get("row") != "turn":
            continue
        by_game.setdefault(r["game"], []).append(r)
    for game, turns in by_game.items():
        mu = mull.get(game)
        tail = deck_features((mu or {}).get("decklist"), carddb, prior)[0]
        opp = (mu or {}).get("opp_class") or "UNKNOWN"
        repl = (mu or {}).get("replaced_in") or []
        repl_agg = _agg(repl, carddb, engines)
        # 前 K 抽牌聚合: turn 升序累积(本回合 drawn_this_turn 含入——Q 服务
        # 于回合 K 末视角, 允许当回合已发生信息)
        seen: list = []
        aggs: dict = {}
        for r in sorted(turns, key=lambda r: r["snap"]["turn"]):
            seen.extend(r.get("drawn_this_turn") or [])
            aggs[r["snap"]["turn"]] = _agg(seen, carddb, engines)
        for r in turns:
            snap_vec, _ = flatten(r["snap"])
            t = r["snap"]["turn"]
            vec = snap_vec + aggs[t] + repl_agg \
                + [1.0 if c == opp else 0.0 for c in classes] + tail
            X.append([float(v) for v in vec])
            y.append(float(r["result"]))
            groups.append(game)
            if t == 3 or (t == 2 and game not in q3):
                q3[game] = len(X) - 1
    return X, y, groups, q3, {"names": names, "classes": classes}


def _snap(turn):
    """flatten 需要的最小快照(仅为取特征名)。"""
    side = {"hp": 0, "armor": 0, "hand": [], "board": [], "deck": 0,
            "secrets": 0, "mana": 0, "mana_cap": 0, "overload": 0,
            "fatigue": 0, "spellpower": 0, "class": None}
    return {"turn": turn, "my_turn": True, "me": dict(side), "opp": dict(side)}


def q_oof_fit(X: list, y: list, groups: list, data_dir,
              n_splits: int = 5) -> dict:
    """组级 CV 的 OOF 预测(每折 TabPFN fit→折外打分)。
    → {"auc": 折均值|None, "aucs": [...], "oof": {样本下标: P(胜)}}。
    tabpfn 不可导入 → {"auc": None, "aucs": [], "oof": {}}(门控不过路径)。"""
    from hsbot.mulligan_ai import tabpfn_env
    tabpfn_env(data_dir)
    try:
        import numpy as np
        from tabpfn import TabPFNClassifier
    except ImportError:
        return {"auc": None, "aucs": [], "oof": {}}
    from .backtest import auc as auc_of, group_folds
    oof: dict = {}
    aucs: list = []
    for tr, va in group_folds(groups, n_splits):
        ytr = [y[i] for i in tr]
        if len(set(ytr)) < 2 or len({y[i] for i in va}) < 2:
            continue
        clf = TabPFNClassifier(device="cpu",
                               ignore_pretraining_limits=True)
        clf.fit(np.array([X[i] for i in tr], dtype=np.float32),
                np.array(ytr, dtype=int))
        p = clf.predict_proba(np.array([X[i] for i in va],
                                       dtype=np.float32))[:, 1]
        for i, pi in zip(va, p):
            oof[i] = float(pi)
        a = auc_of([y[i] for i in va], [float(v) for v in p])
        if a is not None:
            aucs.append(a)
    return {"auc": round(sum(aucs) / len(aucs), 3) if aucs else None,
            "aucs": [round(a, 3) for a in aucs], "oof": oof}


def q_gate_passes(metrics: dict) -> bool:
    """门控(设计 §4): AUC ≥ LR_AUC_GATE 才作蒸馏标签源。"""
    auc = metrics.get("auc")
    return auc is not None and auc >= LR_AUC_GATE
