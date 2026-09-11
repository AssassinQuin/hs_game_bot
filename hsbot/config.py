"""运行配置 —— 支持 config.yaml + 命令行覆盖, 跨平台日志目录探测。

优先级: 命令行参数 > config.yaml > 内置默认(logs_dir 留空时按平台自动探测)。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    "deck_name": "奇迹德",
    "deck_code": "",            # 直接给 deck code 时优先于 Decks.log 匹配
    "logs_dir": "",             # 空 = 按平台自动探测
    "data_dir": "data",
    "battletag": "湫然#51704",   # 友方判定首选(exporter 启发式会翻转, 见 M1_MONITOR §1)
    "replay": "",               # 重放模式: 静态日志路径
    "poll_interval": 0.5,       # 实时轮询间隔(秒)
    "session_check_interval": 10.0,  # 检查新会话目录的间隔(秒)
    # 输出
    "console_echo": True,       # 除悬浮窗外是否同步打印到控制台
    "overlay_enabled": True,    # 半透明置顶日志窗(游戏需无边框/窗口化模式)
    "overlay_alpha": 0.72,
    "overlay_geometry": "460x780+8+120",  # 宽x高+左边距+上边距(贴屏幕左侧)
    "overlay_font_size": 10,
    "overlay_borderless": False,
    # 训练语料
    "training_dir": "data/training",
    "auto_training": True,      # 每局结束自动导出训练样本
}

_FIELDS = tuple(DEFAULTS)  # Config.__init__ 的合法键


def _candidate_logs_dirs() -> list[Path]:
    """各平台 Hearthstone Logs 候选目录(按常见程度排序)。"""
    home = Path.home()
    cands: list[Path] = []
    if sys.platform == "win32":
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        cands += [
            Path(pf86) / "Hearthstone" / "Logs",
            Path("D:/Program Files (x86)/Hearthstone/Logs"),
            Path("E:/battle/Hearthstone/Logs"),  # 本机自定义安装位
            Path(os.environ.get("LOCALAPPDATA", "")) / "Blizzard" / "Hearthstone" / "Logs",
        ]
    elif sys.platform == "darwin":
        cands += [
            home / "Library" / "Preferences" / "Blizzard" / "Hearthstone" / "Logs",
            home / "Library" / "Application Support" / "Blizzard" / "Hearthstone" / "Logs",
            Path("/Applications/Hearthstone/Logs"),
        ]
    else:  # Linux: Proton / Lutris / Wine 常见前缀
        cands += [
            home / "Games" / "hearthstone" / "drive_c" / "Program Files (x86)" / "Hearthstone" / "Logs",
            home / ".steam" / "steam" / "steamapps" / "compatdata" / "0" / "pfx" / "drive_c"
                / "Program Files (x86)" / "Hearthstone" / "Logs",
            home / ".wine" / "drive_c" / "Program Files (x86)" / "Hearthstone" / "Logs",
        ]
    return [c for c in cands if str(c) not in ("", ".")]


def auto_logs_dir() -> str:
    """返回第一个存在的候选目录; 都不存在返回空串。"""
    for c in _candidate_logs_dirs():
        if c.is_dir():
            return str(c)
    return ""


class Config:
    _INT = ("overlay_font_size",)
    _FLOAT = ("poll_interval", "session_check_interval", "overlay_alpha")
    _BOOL = ("console_echo", "overlay_enabled", "overlay_borderless", "auto_training")
    _PATH = ("logs_dir", "data_dir", "training_dir")

    def __init__(self, **values) -> None:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in values.items() if k in _FIELDS})
        for k, v in merged.items():
            if k in self._INT:
                v = int(v)
            elif k in self._FLOAT:
                v = float(v)
            elif k in self._BOOL:
                v = bool(v)
            elif k in self._PATH:
                v = Path(v)
            setattr(self, k, v)

    # ---------- 加载 ----------
    @classmethod
    def load(cls, cli: dict | None = None) -> "Config":
        """cli 里值为 None 的项不覆盖; 返回 (config, 配置来源说明)。"""
        values = dict(DEFAULTS)
        source = "内置默认"

        yaml_path = Path((cli or {}).get("config") or "config.yaml")
        if yaml_path.exists():
            try:
                import yaml
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
                picked = {k: data[k] for k in _FIELDS if data.get(k) not in (None, "")}
                values.update(picked)
                source = str(yaml_path)
            except Exception as exc:  # noqa: BLE001
                log.error("config.yaml 解析失败(%s), 改用内置默认", exc)
        else:
            source = "内置默认 (无 config.yaml)"

        for k, v in (cli or {}).items():
            if k in _FIELDS and v not in (None, ""):
                values[k] = v
                source = "命令行覆盖"

        if not values["logs_dir"]:
            values["logs_dir"] = auto_logs_dir()
        cfg = cls(**{k: v for k, v in values.items() if k in _FIELDS})
        cfg.source = source
        return cfg

    # ---------- 派生路径 ----------
    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    # ---------- 展示 ----------
    def summary(self) -> str:
        auto = "" if str(self.logs_dir) else "  (!! 未找到日志目录, 请在 config.yaml 设置 logs_dir)"
        return (f"卡组={self.deck_name} │ 战网名={self.battletag or '(未设)'} │ "
                f"日志目录={self.logs_dir or '(空)'}{auto}")
