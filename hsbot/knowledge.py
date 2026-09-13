"""知识层 —— 组件台账 + 牌库序视图。

全量从 GameStore 重建, 不维护历史(M1_MONITOR §3.1):
  台账   = decklist − 己方各区域内"来自牌库"的牌(衍生牌有 CREATOR 标签, 不计)
  牌库序 = DECK 区 card_id 非空(被探底/发现揭示过)的实体 + ZONE_POSITION
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from hearthstone.enums import CardType, GameTag, Zone

from .carddb import CardDB
from .store import GameStore, is_generated, zone_pos

log = logging.getLogger(__name__)

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
                log.warning("卡组中 dbfId=%s 无法映射到 card_id, 已跳过", dbf)
        return cls(decklist, carddb, deck_name)

    # ---------- 每次快照全量重建 ----------
    def rebuild(self, st: GameStore) -> Ledger:
        led = Ledger()
        me = st.friendly_key
        if me is not None:
            hand: Counter = Counter()
            used: Counter = Counter()
            for e in st.entities():
                cid = getattr(e, "card_id", None)   # Player/GameEntity 无 card_id
                if (st.ctrl_key(e) != me or not cid
                        or cid not in self.decklist
                        or is_generated(e)
                        or e.tags.get(GameTag.CARDTYPE) not in _LEDGER_TYPES):
                    continue
                if e.zone == Zone.HAND:
                    hand[cid] += 1
                elif e.zone in (Zone.PLAY, Zone.GRAVEYARD, Zone.SECRET):
                    used[cid] += 1
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


# ---------- Decks.log 解析(唯一解释点; 冗余#2: parse_decks_log 与
# corpus 的排队时间线同源于 _parse_decks_log 单遍扫描) ----------
_CODE_RE = re.compile(r"^AA[A-Za-z0-9+/=]{30,}$")
_TS_RE = re.compile(r"^[IWD] (\d[\d:.]+) ?(.*)$")
_SESSION_DATE_RE = re.compile(r"Hearthstone_(\d{4})_(\d{2})_(\d{2})_")


def parse_log_time(s: str, day=None):
    """'20:59:52.6130042' -> day(缺省今天)该时刻的 datetime。"""
    s = s.split(".")[0]
    return datetime.combine(day or datetime.now().date(),
                            datetime.strptime(s, "%H:%M:%S").time())


def session_date(session: str | None):
    """会话目录名 Hearthstone_YYYY_MM_DD_HH_MM_SS 中的日期。

    Decks.log 行只有时刻没有日期, 归因需要绝对时间 —— 会话目录名是唯一
    可靠的日期锚(审计 2026-09-13 中#7: 用"今天"拼日期, 跨午夜会归错卡组)。"""
    m = _SESSION_DATE_RE.search(session or "")
    if m is None:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_decks_log(path: str | Path, day=None) -> tuple[dict[str, str], list]:
    """单遍扫描, 双出口:
      codes    {卡组名: 最新 deck code} —— 客户端每次开局/编辑卡组都会追加;
      timeline [(时刻, 卡组名, code)] —— 只取每次 'Finding Game With Deck'
               序列(有时刻锚才入列, 供训练导出按局归因)。
    无 Finding 的 '### 名 + code'(编辑卡组)只进 codes: 若只看排队序列,
    "编辑了卡组但上次玩的是别的"会丢最新代码。"""
    codes: dict[str, str] = {}
    timeline: list = []
    if not path or not Path(path).exists():
        return codes, timeline
    pending_t = None       # 最近一次 Finding 的时刻
    pending_name = None    # 最近一次 ### 卡组名
    try:
        with open(path, encoding="utf-8", errors="replace") as fp:
            lines = fp.readlines()
    except OSError:
        return codes, timeline
    for raw in lines:
        m = _TS_RE.match(raw.strip())
        t = None
        if m is not None:
            try:
                t = parse_log_time(m.group(1), day)
            except ValueError:
                t = None              # 畸形时刻: 该条不参与时间线
            content = m.group(2)
        else:
            content = raw.strip()
        if content.startswith("Finding Game With Deck"):
            if t is not None:
                pending_t, pending_name = t, None
            continue
        if content.startswith("### "):
            pending_name = content[4:].strip()
        elif pending_name and _CODE_RE.match(content):
            codes[pending_name] = content
            if pending_t is not None:
                timeline.append((pending_t, pending_name, content))
            pending_t = pending_name = None
    return codes, timeline


def parse_decks_log(path: str | Path) -> dict[str, str]:
    """Decks.log → {卡组名: 最新 deck code}(卡组代码匹配用)。"""
    return _parse_decks_log(path)[0]


def deck_timeline(path: str | Path, day=None) -> list:
    """Decks.log 排队时间线 [(时刻, 卡组名, deck code)](训练导出归因用)。

    day: 行时刻的日期锚(传会话目录名解析出的日期); 缺省今天,
    跨午夜由 corpus._attribute 的 ±12h 启发式兜底。"""
    return _parse_decks_log(path, day)[1]
