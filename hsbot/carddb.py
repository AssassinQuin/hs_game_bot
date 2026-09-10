"""卡牌字典 —— cards.zh.json (hearthstonejson) 的只读缓存 + 人工效果表接口位。

M1 只用 name/cost/cardtype/dbfId 映射; burn_overlay(组件效果表)属于 M2 的 planner。
文件缺失时优雅降级: 一律显示原始 card_id, 程序不崩。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class CardDB:
    def __init__(self, cache_path: str | Path) -> None:
        self._by_id: dict[str, dict] = {}
        self._dbf_to_id: dict[int, str] = {}
        path = Path(cache_path)
        if path.exists():
            try:
                cards = json.loads(path.read_text(encoding="utf-8"))
                for c in cards:
                    cid = c.get("id")
                    if not cid:
                        continue
                    self._by_id[cid] = c
                    if "dbfId" in c:
                        self._dbf_to_id[c["dbfId"]] = cid
            except Exception as exc:  # noqa: BLE001
                log.error("卡表加载失败(%s): %s —— 将以原始 card_id 显示", path, exc)
        else:
            log.warning("卡表不存在(%s) —— 将以原始 card_id 显示", path)

    def __len__(self) -> int:
        return len(self._by_id)

    def name(self, card_id: str | None) -> str:
        if not card_id:
            return "?"
        c = self._by_id.get(card_id)
        return c["name"] if c and c.get("name") else card_id

    def cost(self, card_id: str | None) -> int | None:
        if not card_id:
            return None
        c = self._by_id.get(card_id)
        return c.get("cost") if c else None

    def cardtype(self, card_id: str | None) -> str:
        if not card_id:
            return ""
        c = self._by_id.get(card_id)
        return (c.get("type") or "") if c else ""

    def id_from_dbf(self, dbf_id: int) -> str | None:
        return self._dbf_to_id.get(dbf_id)
