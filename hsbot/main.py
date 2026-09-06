"""入口: python -m hsbot [--config config.yaml] [--replay x.log] [--import-all]

配置优先级: 命令行 > config.yaml > 内置默认(logs_dir 留空时按平台自动探测)。
悬浮窗开启时: watcher 跑后台线程, tkinter 主循环占主线程(Windows 要求)。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

from .carddb import CardDB
from .config import Config
from .watcher import Watcher


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(prog="hsbot",
                                 description="奇迹德实时军师 (配置见 config.yaml)")
    ap.add_argument("--config", default=None, help="配置文件路径(默认 ./config.yaml)")
    ap.add_argument("--deck", default=None, help="所选卡组名(匹配 Decks.log)")
    ap.add_argument("--deck-code", default=None, help="直接指定 deck code(优先于 Decks.log)")
    ap.add_argument("--logs-dir", default=None, help="Hearthstone Logs 根目录(留空自动探测)")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--battletag", default=None, help="本机战网名(友方判定首选)")
    ap.add_argument("--replay", default=None, help="重放静态日志(开发/验收用)")
    ap.add_argument("--throttle-ms", type=int, default=None)
    ap.add_argument("--no-overlay", dest="overlay_enabled", action="store_false",
                    help="不开启悬浮日志窗(调试/回放常用)")
    ap.add_argument("--import-all", action="store_true",
                    help="批量解析 logs_dir 下所有会话日志 -> 训练语料目录(按卡组分文件夹)")
    args = ap.parse_args(argv)

    cfg = Config.load({
        "deck_name": args.deck, "deck_code": args.deck_code, "logs_dir": args.logs_dir,
        "data_dir": args.data_dir, "battletag": args.battletag,
        "replay": args.replay, "throttle_ms": args.throttle_ms,
        "config": args.config,
    })

    carddb = CardDB(cfg.cache_dir / "cards.zh.json")

    if args.import_all:
        from .corpus import CorpusExporter
        CorpusExporter(cfg, carddb).import_all()
        return

    # ---- 输出枢纽: 控制台 + 会话记录文件 + 悬浮窗 ----
    if cfg.overlay_enabled:
        try:
            from .overlay import OutputHub, OverlayWindow
        except Exception as exc:  # noqa: BLE001  无 tkinter 环境降级
            print(f"! 悬浮窗不可用({exc}), 仅控制台输出")
            hub = None
        else:
            hub = OutputHub(console=cfg.console_echo, to_overlay=True)
    else:
        hub = None

    watcher = Watcher(cfg, carddb, out=hub if hub is not None else print, hub=hub)

    def run_bot() -> None:
        if cfg.replay:
            watcher.run_replay(cfg.replay)
        else:
            watcher.run_live()

    if hub is not None:
        t = threading.Thread(target=run_bot, daemon=True, name="hsbot-watcher")
        t.start()
        try:
            OverlayWindow(cfg, hub.q).run()
        finally:
            print("悬浮窗已关闭, 监控退出。")
    else:
        try:
            run_bot()
        except KeyboardInterrupt:
            print("已退出。")


if __name__ == "__main__":
    main()
