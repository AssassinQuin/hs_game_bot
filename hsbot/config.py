"""运行配置 —— 支持 config.yaml + 子命令注入, 跨平台日志目录探测。

优先级: 子命令注入 > config.yaml > 内置默认(logs_dir 留空时按平台自动探测)。
"""
from __future__ import annotations

import copy
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
    "draw_dedup_seconds": 0.5,  # 同一实体抽牌多重揭示路径的去重窗口(秒)
    # 输出
    "console_echo": True,       # 除悬浮窗外是否同步打印到控制台
    "overlay_enabled": True,    # 半透明置顶日志窗(游戏需无边框/窗口化模式)
    "overlay_topmost": True,    # 窗口置顶: 只应真实运行开启; 测试/回放一律关闭,
                                # 不抢机器前台(2026-09-13 用户要求)
    "overlay_alpha": 0.72,      # 仅 overlay_log_transparent=false(旧行为)时生效
    "overlay_log_transparent": True,  # 上/中上部面板真不透明: 整窗全不透明,
                                      # 日志区背景色镂空(游戏透出/透明区点击穿透);
                                      # false=整窗 overlay_alpha 半透明(旧行为)
    "overlay_geometry": "460x780+8+120",  # 默认宽x高+左边距+上边距; 实际位置/大小
                                          # 会自动记忆到 data/overlay_state.json 并优先
    "overlay_font_size": 10,
    "overlay_borderless": False,
    "overlay_colors": {         # 信息分色: 按人物(我/对面)与事件类型
        "my": "#b8e6a0",        # 我方动作
        "opp": "#ffa477",       # 对面动作
        "unknown": "#9aa4ad",   # 未知方([T·?])
        "header": "#7ec8ff",    # 回合标题
        "snapshot": "#ffd479",  # 快照
        "game_end": "#7ec8ff",  # 终局
        "notice": "#8fb7d4",    # 系统通知(新对局/监控会话)
        "error": "#ff6b6b",     # 错误
        "advice": "#ffd700",    # 留牌建议(开局高亮)
    },
    "mulligan_advice": True,    # 留牌环节高亮建议(需先跑 python -m trainer mulligan)
    "mulligan_v3": True,        # v3 基座评分器总开关; false 时输出与 v2 逐字节一致
    "scorer_backend": "tabpfn_v2",  # tabpfn_v2 | tabicl_v2(训练时对照, AUC 高者产出系数)
    "distill_min_agree": 0.90,  # 蒸馏系数上线门槛(top-1 集合一致率, 设计 §8)
    "lethal_plan": True,        # 斩杀线规划(信息区"可斩"行: DFS 手牌出牌线,
                                # 见 docs/PLAY_ADVICE.md §6; planner 缺席/未解析
                                # 时诚实降级, 不影响既有两行)
    # 训练语料
    "training_dir": "data/training",
    "auto_training": True,      # 每局结束自动导出训练样本
    "auto_train_models": True,  # 语料落盘后局终后台增量训练(mulligan/material/
                                # value 三连子进程; 审计 2026-09-14: 旧键名
                                # auto_train_model 不在 schema, 是关不掉的死开关)
}

_FIELDS = tuple(DEFAULTS)  # Config.__init__ 的合法键


def _candidate_logs_dirs() -> list[Path]:
    """各平台 Hearthstone Logs 候选目录(按常见程度排序; 本机自定义安装位
    不进代码库 —— 显式写进 config.yaml 的 logs_dir, 审计 2026-09-13 五)。"""
    home = Path.home()
    cands: list[Path] = []
    if sys.platform == "win32":
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        cands += [
            Path(pf86) / "Hearthstone" / "Logs",
            Path("D:/Program Files (x86)/Hearthstone/Logs"),
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
    _FLOAT = ("poll_interval", "session_check_interval", "overlay_alpha",
              "draw_dedup_seconds", "distill_min_agree")
    _BOOL = ("console_echo", "overlay_enabled", "overlay_topmost",
             "overlay_borderless", "overlay_log_transparent",
             "auto_training", "auto_train_models",
             "mulligan_advice", "lethal_plan")
    _PATH = ("logs_dir", "data_dir", "training_dir")

    def __init__(self, **values) -> None:
        merged = copy.deepcopy(dict(DEFAULTS))   # 可变默认(overlay_colors)防跨实例共享
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
                # 审计 2026-09-14 中: 白名单外的键不再静默丢弃 —— 拼错的键
                # 曾造成"写了开关却不生效"(如旧 auto_train_model)
                unknown = [k for k in data if k not in _FIELDS]
                if unknown:
                    log.warning("config.yaml 未知键被忽略(不在配置 schema): %s",
                                ", ".join(map(str, unknown)))
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
