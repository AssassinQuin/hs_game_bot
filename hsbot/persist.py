"""持久化 —— 每局一个 jsonl, 每次快照追加一行(M1_MONITOR §7)。"""
from __future__ import annotations

import json
import time
from pathlib import Path


class SessionStore:
    def __init__(self, sessions_dir: str | Path) -> None:
        self.root = Path(sessions_dir) / time.strftime("%Y%m%d_%H%M%S")
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
