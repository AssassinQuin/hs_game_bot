"""trainer CLI —— 离线训练系统入口(与 bot 解耦; 接合点=文件)。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hsbot.carddb import CardDB
from hsbot.config import Config

from . import material, mulligan, value
from .backtest import cv_auc, print_cv_report


def main(argv=None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    ap = argparse.ArgumentParser(prog="trainer", description="离线训练系统(价值模型/留牌)")

    def add_common(p, sub=False):
        # 顶层与子命令同名同 dest。子命令副本的 default 必须 SUPPRESS: argparse
        # 子解析会先写入自身默认值, 把子命令前设置的同名旗标盖回 None(实测),
        # SUPPRESS 后旗标放在子命令前或后都生效。
        kw = {"default": argparse.SUPPRESS} if sub else {"default": None}
        p.add_argument("--deck", help="卡组名(默认取 config deck_name)", **kw)
        p.add_argument("--battletag", help="我方战网名(默认取 config)", **kw)
        p.add_argument("--corpus", help="语料目录(默认 data/training)", **kw)
        p.add_argument("--out", help="产出目录(默认 trainer/data/<卡组>)", **kw)

    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--data-dir", dest="data_dir", default=None)
    add_common(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_mat = sub.add_parser("material", help="语料原始切片 → 回合级训练素材")
    add_common(p_mat, sub=True)
    p_mat2 = sub.add_parser("material-v2", help="语料原始切片 → v3 素材(决策行/逐回合抽牌)")
    add_common(p_mat2, sub=True)
    p_train = sub.add_parser("train", help="素材 → 价值模型(时序 AUC 报告)")
    add_common(p_train, sub=True)
    p_bt = sub.add_parser("backtest", help="素材 → 分组交叉验证(同局绝不跨训练/验证组)")
    add_common(p_bt, sub=True)
    if "mulligan" in argv:                   # 留牌训练器(独立子命令族, 旗标可在前后)
        i = argv.index("mulligan")
        return mulligan.main(argv[:i] + argv[i + 1:])
    if "sim" in argv:                       # 留牌模拟器(v3.1, 独立子命令族)
        i = argv.index("sim")
        from . import sim
        return sim.main(argv[:i] + argv[i + 1:])
    args = ap.parse_args(argv)

    cfg = Config.load({"config": args.config, "data_dir": args.data_dir})
    deck = args.deck or cfg.deck_name
    battletag = args.battletag or cfg.battletag
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    corpus = Path(args.corpus or cfg.training_dir)
    out = Path(args.out) if args.out else \
        Path(__file__).resolve().parent / "data" / deck

    if args.cmd == "backtest":
        rows = [r for r in material.load_material(out) if "result" in r]
        if len(rows) < 20:
            raise SystemExit(f"素材不足({len(rows)} 行 < 20)")
        from .states import flatten
        X, y, names = [], [], []
        for i, r in enumerate(rows):
            vec, nm = flatten(r["snap"])
            names = nm or names
            X.append(vec)
            y.append(r["result"])
        groups = [r["src"].split("#")[0] for r in rows]   # 组 = 局

        def gbm(Xtr, ytr):
            from sklearn.ensemble import GradientBoostingClassifier
            m = GradientBoostingClassifier(max_depth=2, n_estimators=150,
                                           learning_rate=0.05,
                                           random_state=0).fit(Xtr, ytr)
            return lambda Xv: m.predict_proba(Xv)[:, 1]

        def lr(Xtr, ytr):
            from sklearn.linear_model import LogisticRegression
            m = LogisticRegression(C=0.3, max_iter=2000).fit(Xtr, ytr)
            return lambda Xv: m.predict_proba(Xv)[:, 1]

        def dummy(Xtr, ytr):
            p = sum(ytr) / len(ytr)
            return lambda Xv: [p] * len(Xv)

        def tab(Xtr, ytr):
            try:
                from hsbot.mulligan_ai import tabpfn_env
                tabpfn_env(Path(cfg.data_dir))
                from tabpfn import TabPFNClassifier
            except ImportError:
                return None
            clf = TabPFNClassifier(device="cpu",
                                   ignore_pretraining_limits=True).fit(Xtr, ytr)
            return lambda Xv: clf.predict_proba(Xv)[:, 1]

        report = cv_auc({"梯度提升": gbm, "逻辑回归": lr, "TabPFN基座": tab,
                         "恒猜多数": dummy},
                        X, y, groups)
        print_cv_report(f"价值模型 P(胜│局面) 分组CV — {len(rows)} 行/{len(set(groups))} 局",
                        report)
        return 0

    if args.cmd == "material":
        stats = material.build_material(corpus, deck, out, carddb, battletag)
        print(f"素材: {stats['rows']} 个决策点({stats['games']} 局, 切片 "
              f"{stats['slices']}, 胜 {stats['wins']}) → {out / 'material.jsonl'}")
        if stats["skip"]:
            print("跳过: " + " │ ".join(f"{k} {v}" for k, v in stats["skip"].items()))
        return 0
    if args.cmd == "material-v2":
        stats = material.build_material_v2(corpus, deck, out, carddb, battletag)
        print(f"v3 素材: {stats['mulligan_rows']} 决策行 / {stats['games']} 局 "
              f"(切片 {stats['slices']}) → {out / 'material_v2.jsonl'}")
        if stats["skip"]:
            print("跳过: " + " │ ".join(f"{k} {v}" for k, v in stats["skip"].items()))
        return 0
    metrics = value.train(out, out)
    print(f"── 价值模型 ({deck}) ──")
    print(f"素材 {metrics['n_rows']} 行 │ 时序留出 {metrics['n_train']}/{metrics['n_test']} "
          f"│ AUC {metrics.get('test_auc', '—')} │ 特征 {metrics['n_features']} 维")
    print(f"模型: {out / 'value.pkl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
