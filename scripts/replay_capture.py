"""回放采集 —— 把 watcher 输出落盘, 供新旧版本 diff 验收(spec §6)。

用法: python3 scripts/replay_capture.py <Power.log...> [-o 输出目录] [--data-dir D]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.watcher import Watcher


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("-o", "--out", default="data/baseline")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       **({"data_dir": args.data_dir} if args.data_dir else {})})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    rc = 0
    for log in args.logs:
        lines: list[str] = []
        w = Watcher(cfg, carddb, out=lines.append)
        w.run_replay(log)
        dest = out / (Path(log).stem + ".txt")
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        bad = sum(1 for l in lines if "快照导出失败" in l)
        print(f"{log}: {len(lines)} 行 -> {dest}" + (f" !!快照失败{bad}处" if bad else ""))
        if bad:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
