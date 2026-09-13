"""持久化 —— 每局一个 jsonl, 每次快照追加一行(M1_MONITOR §7)。

本模块同时提供 atomic_write_text: 项目内所有"整文件覆盖"类落盘
(训练 jsonl / _imported.json / effects.json / overlay_state.json / decklist.json)
统一走 临时文件 + 原子替换, 中途崩溃不再产生截断文件(审计 2026-09-13 中#6)。
追加型写入(快照 jsonl / 会话 .log)无需原子化。
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path


def atomic_write_text(path: str | Path, text: str) -> None:
    """整文件覆盖的原子写: 同目录临时文件 + os.replace(同盘原子)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, sessions_dir: str | Path) -> None:
        # 目录名带随机后缀: 同秒双开两个 bot 不再交错写同一批文件
        self.root = (Path(sessions_dir) /
                     f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.game_no = 0
        self.path: Path | None = None

    def new_game(self) -> Path:
        self.game_no += 1
        self.path = self.root / f"game_{self.game_no:03d}.jsonl"
        self.path.touch()
        return self.path

    def write_snapshot(self, payload: dict) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
