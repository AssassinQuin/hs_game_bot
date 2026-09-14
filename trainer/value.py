"""价值模型 —— P(胜│回合快照)。v1: 梯度提升(sklearn, 零新增依赖)。

训练 = 素材行 flatten → 定长向量, 时序留出(前 80% 拟合 → 后 20% 打 AUC),
服务模型用全量重拟合; 产物 = value.pkl + value_meta.json(特征名/AUC/行数)。
留牌等其它决策后续复用同一"价值"口径: 候选动作推演后的局面谁 P(胜) 高。
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

from .states import flatten


def build_matrix(rows: list[dict]) -> tuple[list, list, list]:
    X, names = [], []
    for i, r in enumerate(rows):
        vec, names_i = flatten(r["snap"])
        if i == 0:
            names = names_i
        X.append(vec)
    y = [r["result"] for r in rows]
    return X, y, names


def _game_level_split(rows: list[dict], test_frac: float = 0.2) -> int:
    """组级(按局)时序切分: 返回测试段起始下标 —— 同局的行绝不跨训练/验证
    (行级切分会把同局近重复行同时放两侧, test_auc 虚高; backtest 同款口径,
    审计 2026-09-14 中#12)。rows 按局连续排列(material 逐切片顺序产出)。"""
    games: list[str] = []
    for r in rows:
        k = str(r.get("src", "")).split("#")[0]
        if not games or games[-1] != k:
            games.append(k)
    n_test_games = max(1, round(len(games) * test_frac))
    if n_test_games >= len(games):
        return 0
    split_key = games[-n_test_games]
    return next(i for i, r in enumerate(rows)
                if str(r.get("src", "")).split("#")[0] == split_key)


def train(material_dir: Path | str, out_dir: Path | str) -> dict:
    from sklearn.ensemble import GradientBoostingClassifier

    rows = [r for r in material_rows(material_dir) if "result" in r]
    if len(rows) < 20:
        raise SystemExit(f"素材不足({len(rows)} 行 < 20): 先多打几局再训")
    X, y, names = build_matrix(rows)
    cut = _game_level_split(rows)              # 组级时序留出(防同局泄漏)
    xtr, ytr = X[:cut], y[:cut]
    xte, yte = X[cut:], y[cut:]

    def new_model():
        return GradientBoostingClassifier(max_depth=2, n_estimators=150,
                                          learning_rate=0.05, random_state=0)

    metrics = {"n_rows": len(rows), "n_train": len(xtr), "n_test": len(xte),
               "n_features": len(names), "features": names,
               # 语料局数(组数): 实时军师的开口门槛依据(hsbot/play_ai.py
               # PLAY_N_GAMES_MIN=300, docs/PLAY_ADVICE.md §1-T2)
               "n_games": len({str(r.get("src", "")).split("#")[0]
                               for r in rows})}
    if len(set(ytr)) == 2 and len(set(yte)) == 2:
        m = new_model().fit(xtr, ytr)
        pte = m.predict_proba(xte)[:, 1]
        metrics["test_auc"] = round(_auc(yte, list(pte)), 3)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    final = new_model().fit(X, y)                # 服务模型: 全量重拟合
    (out / "value.pkl").write_bytes(pickle.dumps({"model": final, "names": names}))
    (out / "value_meta.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    return metrics


def material_rows(material_dir: Path | str):
    from .material import load_material
    return load_material(material_dir)


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


def predict(material_dir_unused, model_dir: Path | str, snap: dict) -> float:
    """单局面 → P(胜)(供后续军师接入; 接合点 = 模型产物文件)。"""
    blob = pickle.loads((Path(model_dir) / "value.pkl").read_bytes())
    vec, _ = flatten(snap)
    return float(blob["model"].predict_proba([vec])[0, 1])
