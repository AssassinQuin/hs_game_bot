"""知识层 —— 组件台账 + 牌库序视图。

全量从 GameStore 重建, 不维护历史(M1_MONITOR §3.1):
  台账   = decklist − 己方各区域内"来自牌库"的牌(衍生牌有 CREATOR 标签, 不计)
  牌库序 = DECK 区 card_id 非空(被探底/发现揭示过)的实体 + ZONE_POSITION
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from hearthstone.enums import CardType, GameTag, Zone

from .carddb import CardDB
from .store import GameStore, is_generated, zone_pos

# 台账只统计"真牌", 排除英雄/技能/附魔等
_LEDGER_TYPES = (CardType.SPELL, CardType.MINION, CardType.WEAPON,
                 CardType.ITEM, CardType.TOKEN)


@dataclass
class Ledger:
    in_hand: Counter = field(default_factory=Counter)
    used: Counter = field(default_factory=Counter)       # 已打出/已进墓地/奥秘位
    remaining: Counter = field(default_factory=Counter)  # 牌库剩余期望组成
    deck_actual: int = 0                                 # DECK 区真实张数
    known_top: str | None = None                         # 顶牌已知(=下次抽确定)
    known_bottom: list = field(default_factory=list)     # [(card_id, 牌位)] 牌位降序, 仅牌位>0
    known_unpositioned: list = field(default_factory=list)  # 已见但位置未知(换牌换回等)
    unknown_middle: int = 0


class DeckKnowledge:
    def __init__(self, decklist: dict[str, int], carddb: CardDB, deck_name: str = "") -> None:
        self.decklist = dict(decklist)
        self.carddb = carddb
        self.deck_name = deck_name
        self.ledger: Ledger = Ledger()

    # ---------- 构造 ----------
    @classmethod
    def from_code(cls, code: str, carddb: CardDB, deck_name: str = "") -> "DeckKnowledge":
        from hearthstone.deckstrings import parse_deckstring
        cards, _heroes, _fmt, _sb = parse_deckstring(code)
        decklist: dict[str, int] = {}
        for dbf, n in cards:
            cid = carddb.id_from_dbf(dbf)
            if cid:
                decklist[cid] = decklist.get(cid, 0) + n
            else:
                print(f"! 卡组中 dbfId={dbf} 无法映射到 card_id, 已跳过")
        return cls(decklist, carddb, deck_name)

    # ---------- 每次快照全量重建 ----------
    def rebuild(self, st: GameStore) -> Ledger:
        led = Ledger()
        me = st.friendly_key
        if me is not None:
            hand: Counter = Counter()
            used: Counter = Counter()
            for e in st.entities():
                if (st.ctrl_key(e) != me or not e.card_id
                        or e.card_id not in self.decklist
                        or is_generated(e)
                        or e.tags.get(GameTag.CARDTYPE) not in _LEDGER_TYPES):
                    continue
                if e.zone == Zone.HAND:
                    hand[e.card_id] += 1
                elif e.zone in (Zone.PLAY, Zone.GRAVEYARD, Zone.SECRET):
                    used[e.card_id] += 1
            led.in_hand = hand
            led.used = used
            led.remaining = Counter(self.decklist) - hand - used
            deck_ents = st.deck_entities(me)
            led.deck_actual = len(deck_ents)

            revealed = sorted((e for e in deck_ents if e.card_id),
                              key=zone_pos)
            if revealed and zone_pos(revealed[0]) == 1:
                led.known_top = revealed[0].card_id
            pos_known = [e for e in revealed if zone_pos(e) > 0]
            led.known_bottom = [(e.card_id, zone_pos(e)) for e in
                                sorted(pos_known, key=lambda e: -zone_pos(e))[:3]]
            # 换牌换回的牌位置随机(0/未知) —— 不能标成"牌库底"
            led.known_unpositioned = [e.card_id for e in revealed if zone_pos(e) == 0]
            led.unknown_middle = len(deck_ents) - len(pos_known)
        self.ledger = led
        return led

    def mismatch_count(self, played_card_ids) -> int:
        """打出的不属于卡组的牌数(判"通用模式": ≥2 判切卡组)。"""
        return sum(1 for c in played_card_ids if c not in self.decklist)


# ---------- Decks.log 解析 ----------
_CODE_RE = re.compile(r"^AA[A-Za-z0-9+/=]{30,}$")
_TS_RE = re.compile(r"^[IWD] \d[\d:.]+ ?(.*)$")


def parse_decks_log(path: str | Path) -> dict[str, str]:
    """Decks.log → {卡组名: 最新 deck code}。客户端每次开局/编辑卡组都会追加。"""
    codes: dict[str, str] = {}
    last_name: str | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fp:
            for raw in fp:
                m = _TS_RE.match(raw.strip())
                content = (m.group(1) if m else raw).strip()
                if content.startswith("### "):
                    last_name = content[4:].strip()
                elif last_name and _CODE_RE.match(content):
                    codes[last_name] = content
                    last_name = None
    except OSError:
        pass
    return codes
