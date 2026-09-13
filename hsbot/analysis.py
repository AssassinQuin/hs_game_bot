"""解析层 —— 卡牌文本与效果指纹的唯一解释点(2026-09-13 分层化)。

三层职责边界:
  store(GameStore)  唯一状态维护: 实体/区域/标签 → 快照与纯状态事实;
  analysis(本层)    卡牌/效果解析: $N 文本伤害、伤害预估、隐藏触发指纹判定;
  render            纯输出: 只格式化事件字段, 不含任何游戏规则。

实时性: watcher._route 在渲染前调 enrich(evt, store) —— 派生结论当拍得出,
不滞后、不二次维护状态; 事件字典的派生字段(pred_dmg/card_id 推断/via)
由本层写入, store 与 render 对规则零感知。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .carddb import CardDB
from .consts import RENATHAL_CARD_ID, RENATHAL_MIN_DECK_COUNT, SPELLPOWER_TYPES
from .effects import Damage, Heal, EffectCache
from .mulligan_ai import UNKNOWN, hero_class, is_coin

_CID_SCAN_RE = re.compile(r"cardId=([A-Za-z0-9_]+)")

_DMG_TYPES = SPELLPOWER_TYPES          # 只有法术/英雄技能吃法强(随从战吼不加成)


class EffectAnalyzer:
    def __init__(self, carddb: CardDB, cache: Optional[EffectCache] = None,
                 mulligan=None) -> None:
        self.carddb = carddb
        self.cache = cache or EffectCache()   # 无路径 = 仅内存增量
        self.mulligan = mulligan              # 留牌建议器(MulliganAdvisor), 可选

    # ---------- IR 访问 ----------
    def _compiled(self, card_id: str | None):
        return self.cache.get_or_compile(self.carddb.raw(card_id))

    # ---------- 卡牌文本解析($N 语义已编译进 IR) ----------
    def text_damage(self, card_id: str | None) -> tuple[int, int] | None:
        """(基础伤害, 段数); 非伤害牌返回 None。"""
        ir = self._compiled(card_id)
        if ir is None:
            return None
        for e in ir.effects:
            if isinstance(e, Damage):
                return e.base, e.hits
        return None

    # ---------- 效果预估 ----------
    def predict_damage(self, card_id: str | None, spellpower: int) -> dict | None:
        """预计伤害 {"total": 总伤, "hits": 段数}; 不吃法强的牌返回 None。"""
        if self.carddb.cardtype(card_id) not in _DMG_TYPES:
            return None
        d = self.text_damage(card_id)
        if not d:
            return None
        base, hits = d
        return {"total": base + spellpower * hits, "hits": hits}

    # ---------- 触发指纹判定 ----------
    def infer_trigger_card(self, keyword: str | None, effect_index: int | None,
                           actor_deck_count: int | None) -> str | None:
        """隐藏开局触发的可判定推断:
        40 卡组局 + START_OF_GAME + EffectIndex=1 → 雷纳索尔王子(40 卡组的
        唯一开局触发牌; 阿扎莉娜复制进牌库的那份同样命中)。不满足不猜。"""
        if ("START_OF_GAME" in str(keyword or "")
                and effect_index == 1
                and (actor_deck_count or 0) >= RENATHAL_MIN_DECK_COUNT):
            return RENATHAL_CARD_ID
        return None

    # ---------- 事件实时富化 ----------
    def enrich(self, evt: dict, store) -> dict:
        """按事件类型补充派生字段(不改 store 状态, 不改 store 的事件事实)。"""
        kind = evt.get("kind")
        if kind == "play":
            pred = self.predict_damage(evt.get("card_id"),
                                       store.spellpower(evt.get("actor")) or 0)
            if pred:
                evt = {**evt, "pred_dmg": pred}
        elif kind == "trigger":
            if evt.get("card_id") is None:
                inferred = self.infer_trigger_card(
                    evt.get("keyword"), evt.get("effect_index"),
                    evt.get("actor_deck_count"))
                if inferred:
                    evt = {**evt, "card_id": inferred, "inferred": True}
        elif kind == "mulligan_offer":
            # 留牌建议(责任链的分析环): 只对我方、只富化不改事件事实;
            # 无建议器/无模型 → 事件原样通过, 渲染层回退到"起手可留"。
            if self.mulligan is not None and evt.get("actor") == store.friendly_key:
                offered = [c for c in evt.get("offered") or []
                           if c and not is_coin(c)]     # 幸运币不可换, 不进决策
                opp_cid = next((cid for pid, cid in
                                (store.heroes_facts() or {}).items()
                                if pid != evt.get("actor")), None)
                advice = self.mulligan.advise(
                    offered, hero_class(opp_cid) or UNKNOWN,
                    coin=any(is_coin(c) for c in evt.get("offered") or []))
                if advice:
                    evt = {**evt, "advice": advice}
        elif kind == "cost":
            eid = evt.get("eid")
            via = store.enchantments_on(eid) if eid is not None else []
            if not via and evt.get("ctx_eid") is not None:
                host_cid = store.cid_of(evt["ctx_eid"])
                if host_cid:
                    via = [host_cid]     # 光环式减益: 归因到所在触发块
            evt = {**evt, "via": via}
        return evt

    # ---------- 批量入口(parse-cards 子命令) ----------
    def compile_seen_cards(self, cache: EffectCache, card_ids) -> dict:
        """对出现过的卡牌做增量编译(已缓存的直接复用); 返回统计。"""
        stats = {"total": 0, "compiled": 0, "reused": 0, "missing": 0,
                 "damage": 0, "heal": 0, "unknown": 0}
        for cid in sorted(card_ids):
            stats["total"] += 1
            card = self.carddb.raw(cid)
            if card is None:
                stats["missing"] += 1
                continue
            was = cid in cache
            ir = cache.get_or_compile(card)
            stats["reused" if was else "compiled"] += 1
            kind = "unknown"
            for e in ir.effects:
                if isinstance(e, Damage):
                    kind = "damage"
                    break
                if isinstance(e, Heal):
                    kind = "heal"
                    break
            stats[kind] += 1
        cache.save()
        return stats


def collect_card_ids(log_paths) -> set:
    """扫描日志文件中出现过的全部 cardId(只采集, 不解析)。"""
    ids: set = set()
    for p in log_paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _CID_SCAN_RE.finditer(text):
            ids.add(m.group(1))
    return ids
