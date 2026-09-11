"""入口: python -m hsbot [--config config.yaml] [replay <log> | import-all]

配置一律 config.yaml(优先级: 子命令注入 > config.yaml > 内置默认)。
悬浮窗开启时: watcher 跑后台线程, tkinter 主循环占主线程(Windows 要求)。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from .carddb import CardDB
from .config import Config
from .watcher import Watcher


def _setup_logging(data_dir) -> None:
    """诊断日志双通道: 文件 DEBUG 完整(含时间/模块/堆栈), 控制台只出 WARNING+。"""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_dir / "hsbot.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)


def run_import_all(cfg, carddb) -> None:
    from .corpus import CorpusExporter
    CorpusExporter(cfg, carddb).import_all()


def build_config(argv: list[str] | None) -> tuple[Config, argparse.Namespace]:
    ap = argparse.ArgumentParser(prog="hsbot",
                                 description="奇迹德实时军师 (配置见 config.yaml)")
    ap.add_argument("--config", default=None, help="配置文件路径(默认 ./config.yaml)")
    sub = ap.add_subparsers(dest="cmd")
    p_replay = sub.add_parser("replay", help="重放静态日志(开发/验收用, 自动关悬浮窗)")
    p_replay.add_argument("log", help="Power.log 路径")
    sub.add_parser("import-all", help="批量解析 logs_dir 下所有会话日志 -> 训练语料")
    args = ap.parse_args(argv)

    overrides: dict = {"config": args.config}
    if args.cmd == "replay":
        overrides["replay"] = args.log
        overrides["overlay_enabled"] = False
    return Config.load(overrides), args


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    cfg, args = build_config(argv)
    _setup_logging(cfg.data_dir)
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")

    if args.cmd == "import-all":
        run_import_all(cfg, carddb)
        return

    # ---- 输出枢纽: 控制台 + 会话记录文件 + 悬浮窗(永远存在, 无悬浮窗时 console-only) ----
    from .overlay import OutputHub          # 不依赖 tkinter, 顶层 import 安全
    OverlayWindow = None                    # 悬浮窗类: tkinter 不可用时保持 None(降级)
    if cfg.overlay_enabled:
        try:
            from .overlay import OverlayWindow
        except Exception as exc:  # noqa: BLE001  无 tkinter 环境降级
            print(f"! 悬浮窗不可用({exc}), 仅控制台输出")

    hub = OutputHub(console=cfg.console_echo if OverlayWindow is not None else True,
                    to_overlay=OverlayWindow is not None)

    watcher = Watcher(cfg, carddb, out=hub, hub=hub)

    def run_bot() -> None:
        if cfg.replay:
            watcher.run_replay(cfg.replay)
        else:
            watcher.run_live()

    if hub.q is not None:        # 悬浮窗模式: tkinter 占主线程
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
