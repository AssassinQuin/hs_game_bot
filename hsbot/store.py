"""状态层 —— 每局一个 GameStore: 唯一可变状态权威(spec 2026-09-07 §3)。

实体/标签/区域由 adapter.StoreExporter(hslog EntityTreeExporter 容错子类)维护在
hearthstone.entities 上(不造轮子); store 只追踪库没有的部分(留牌/发现/PLAY延迟/
回合计数/括号线索), 并从状态迁移衍生链路事件。

变更入口(spec §3.2 细化): apply(p, depth) / settle() / note_friendly(pid)
/ hint_cid / hint_ctrl / hint_draw —— 之外无人可改状态。
契约: 查询返回活引用只读; 单线程(watcher 轮询线程独占);
订阅者同步调用、不得重入 apply。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from hearthstone.entities import Player
from hearthstone.enums import CardType, GameTag, PlayState, Zone

from .adapter import is_play_block, new_store_exporter, packet_payload
from .carddb import CardDB

PlayerKey = int  # PLAYER_ID: 1=先手, 2=后手(硬币); CONTROLLER 标签值同域

_TERMINAL_PLAYSTATE = {PlayState.WON.value, PlayState.LOST.value, PlayState.TIED.value}
_MANA_TAGS = {
    GameTag.RESOURCES: "res",
    GameTag.TEMP_RESOURCES: "temp",
    GameTag.RESOURCES_USED: "used",
    GameTag.OVERLOAD_OWED: "overload",
    GameTag.OVERLOAD_LOCKED: "overload",
}
_HAND_TYPES = (CardType.SPELL, CardType.MINION, CardType.WEAPON,
               CardType.HERO, CardType.ITEM, CardType.TOKEN, CardType.INVALID)

EventCb = Callable[[dict], None]


# ---------- 实体只读助手(库 Card 缺的计算属性; 全项目唯一定义处) ----------
def atk(e) -> int:
    return e.tags.get(GameTag.ATK, 0)


def hp_total(e) -> int:
    return max(0, e.tags.get(GameTag.HEALTH, 0) + e.tags.get(GameTag.ARMOR, 0)
               - e.tags.get(GameTag.DAMAGE, 0))


def is_taunt(e) -> bool:
    return bool(e.tags.get(GameTag.TAUNT))


def is_generated(e) -> bool:
    return GameTag.CREATOR in e.tags


def zone_pos(e) -> int:
    return e.tags.get(GameTag.ZONE_POSITION, 0)


def is_hero(e) -> bool:
    return e.tags.get(GameTag.CARDTYPE) == CardType.HERO


def is_hero_power(e) -> bool:
    return e.tags.get(GameTag.CARDTYPE) == CardType.HERO_POWER


def is_coin(cid: str | None) -> bool:
    return bool(cid) and ("COIN" in cid.upper() or cid.upper() == "GAME_005")


@dataclass
class MulliganState:
    offered: list = field(default_factory=list)
    kept: list = field(default_factory=list)


class GameStore:
    """每局一个; 由 Watcher._new_game 创建, 局终丢弃(spec §3.6)。"""

    def __init__(self, *, carddb: CardDB, battletag: str = "",
                 tree=None, player_manager=None) -> None:
        self.carddb = carddb
        self.battletag = battletag
        self.exporter = new_store_exporter(self, tree, player_manager)
        self.meta: dict[str, str] = {}
        self.friendly_key: PlayerKey | None = None
        self.lines_consumed = 0
        # ---- 库没有的选择/显示态 ----
        self.current: PlayerKey | None = None
        self.player_turn: dict[PlayerKey, int] = {}
        self.mulligan: dict[PlayerKey, MulliganState] = {}
        self.played_cids: set[str] = set()
        self._choice_pid: dict[int, PlayerKey] = {}
        self._mulligan_emitted: set[int] = set()
        self._discover: dict[int, dict] = {}
        self._discover_emitted: set[int] = set()
        self._pending_play: tuple[int, object] | None = None
        self._hint_cid: dict[int, str] = {}
        self._hint_ctrl: dict[int, PlayerKey] = {}
        self._draw_ts: dict[int, float] = {}
        self._ent2pid: dict[int, PlayerKey] = {}
        self._ended = False
        self._subs: list[EventCb] = []
        self.unhandled: list[dict] = []    # raw 台账(spec §3.4 全量收录, 上限 500)

    # ================= 基础 =================
    @property
    def game(self):
        return self.exporter.game

    @property
    def turn(self) -> int:
        g = self.game
        return g.tags.get(GameTag.TURN, 0) if g is not None else 0

    def entities(self) -> list:
        g = self.game
        return list(g.entities) if g is not None else []

    def get(self, eid: int):
        g = self.game
        return g.find_entity_by_id(eid) if g is not None else None

    def cid_of(self, eid: int) -> str | None:
        e = self.get(eid)
        cid = getattr(e, "card_id", None) if e is not None else None
        return cid or self._hint_cid.get(eid)

    def ctrl_key(self, e) -> PlayerKey | None:
        c = getattr(e, "controller", None)
        return c.player_id if c is not None else None

    def player_keys(self) -> list[PlayerKey]:
        g = self.game
        return sorted(p.player_id for p in g.players) if g is not None else []

    def opponent_key(self) -> PlayerKey | None:
        if self.friendly_key is None:
            return None
        for k in self.player_keys():
            if k != self.friendly_key:
                return k
        return None

    def name(self, key: PlayerKey | None) -> str:
        if key is None:
            return "?"
        g = self.game
        if g is not None:
            p = g.get_player(key)
            if p is not None and getattr(p, "name", None):
                return p.name
        return str(key)

    # ================= 查询(活引用, 只读) =================
    def _of(self, key: PlayerKey | None) -> list:
        return [e for e in self.entities()
                if key is None or self.ctrl_key(e) == key]

    def hero(self, key: PlayerKey):
        heroes = [e for e in self._of(key) if is_hero(e)]
        heroes.sort(key=lambda e: 0 if e.zone == Zone.PLAY else 1)
        return heroes[0] if heroes else None

    def hero_hp(self, key: PlayerKey) -> int:
        h = self.hero(key)
        if h is None:
            return 0
        return max(0, h.tags.get(GameTag.HEALTH, 0) - h.tags.get(GameTag.DAMAGE, 0))

    def hero_armor(self, key: PlayerKey) -> int:
        h = self.hero(key)
        return h.tags.get(GameTag.ARMOR, 0) if h else 0

    def hero_total_hp(self, key: PlayerKey) -> int:
        h = self.hero(key)
        return hp_total(h) if h else 0

    def hand(self, key: PlayerKey) -> list:
        cards = [e for e in self._of(key) if e.zone == Zone.HAND
                 and e.tags.get(GameTag.CARDTYPE, CardType.INVALID) in _HAND_TYPES]
        cards.sort(key=zone_pos)
        return cards

    def board(self, key: PlayerKey) -> list:
        minions = [e for e in self._of(key) if e.zone == Zone.PLAY
                   and e.tags.get(GameTag.CARDTYPE) == CardType.MINION]
        minions.sort(key=zone_pos)
        return minions

    def deck_entities(self, key: PlayerKey) -> list:
        return [e for e in self._of(key) if e.zone == Zone.DECK]

    def deck_count(self, key: PlayerKey) -> int:
        return len(self.deck_entities(key))

    def graveyard_count(self, key: PlayerKey) -> int:
        return sum(1 for e in self._of(key) if e.zone == Zone.GRAVEYARD)

    def secret_count(self, key: PlayerKey) -> int:
        return sum(1 for e in self._of(key) if e.zone == Zone.SECRET)

    def _player_entity(self, key: PlayerKey):
        g = self.game
        return g.get_player(key) if g is not None else None

    def mana_fields(self, key: PlayerKey) -> dict[str, int]:
        e = self._player_entity(key)
        t = e.tags if e is not None else {}
        return {
            "res": t.get(GameTag.RESOURCES, 0),
            "used": t.get(GameTag.RESOURCES_USED, 0),
            "temp": t.get(GameTag.TEMP_RESOURCES, 0),
            "overload": t.get(GameTag.OVERLOAD_OWED, 0) + t.get(GameTag.OVERLOAD_LOCKED, 0),
        }

    def mana_now(self, key: PlayerKey) -> int:
        f = self.mana_fields(key)
        return max(0, f["res"] + f["temp"] - f["used"])

    def mana_next_turn(self, key: PlayerKey) -> int:
        f = self.mana_fields(key)
        return max(0, min(10, f["res"] + 1 - f["overload"]))

    def current_key(self) -> PlayerKey | None:
        return self.current

    def is_my_turn(self) -> bool:
        return self.friendly_key is not None and self.current == self.friendly_key

    def playstate(self, key: PlayerKey) -> str:
        e = self._player_entity(key)
        v = e.tags.get(GameTag.PLAYSTATE, 0) if e is not None else 0
        try:
            return PlayState(v).name
        except Exception:  # noqa: BLE001
            return str(v)

    def friendly_turn_number(self) -> int:
        """我的第 N 回合(TURN 是半回合制, 按 FIRST_PLAYER 换算)。"""
        raw = self.turn
        g = self.game
        first = None
        if g is not None:
            fp = g.first_player
            first = fp.player_id if fp is not None else None
        mine_is_first = first is not None and first == self.friendly_key
        return (raw + 1) // 2 if mine_is_first else raw // 2

    # ================= 订阅(spec §3.4) =================
    def subscribe(self, cb: EventCb) -> None:
        self._subs.append(cb)

    def _emit_event(self, evt: dict) -> None:
        evt.setdefault("turn", self.turn)
        evt.setdefault("friendly", self.friendly_key)
        for cb in self._subs:
            cb(evt)

    # ================= 变更入口(spec §3.2 细化) =================
    def apply(self, p, depth: int = 0) -> None:
        """游标逐包调用: 先刷挂起 PLAY → 取旧标签 → 库应用 → 衍生。"""
        if self._pending_play is not None and depth <= self._pending_play[0]:
            self._flush_play()
        old = self._pre_tags(p)
        self.exporter.export_packet(p)
        self._derive(p, old)
        if is_play_block(p):
            self._pending_play = (depth, p)

    def settle(self) -> None:
        """批尾: 块已收口(ended)的挂起 PLAY 立即发出。"""
        if self._pending_play is not None and getattr(self._pending_play[1], "ended", False):
            self._flush_play()

    def note_friendly(self, pid: PlayerKey | None) -> None:
        if pid is not None:
            self.friendly_key = pid

    def hint_cid(self, eid: int, cid: str) -> None:
        self._hint_cid[eid] = cid

    def hint_ctrl(self, eid: int, pid: PlayerKey) -> None:
        self._hint_ctrl[eid] = pid

    def hint_draw(self, eid: int, cid: str, actor: PlayerKey) -> None:
        """行级括号 SHOW_ENTITY(抽牌揭示主形态)的直通口, 走同一去重。"""
        self._emit_draw(eid, cid, actor)

    def _pre_tags(self, p) -> dict | None:
        """受影响实体应用前的标签快照(推导 区域/费用/血甲 变化用)。"""
        eid = getattr(p, "entity", None)
        if not isinstance(eid, int):
            return None
        e = self.get(eid)
        return dict(e.tags) if e is not None else None

    # ================= 状态迁移 → 事件/自有字段 =================
    def _derive(self, p, old: dict | None) -> None:
        # 按类型名分发(避免 store import hslog, 铁律)
        name = type(p).__name__
        if name == "TagChange":
            self._on_tag_change(p, old)
        elif name == "CreateGame":
            self._ent2pid = {pl.entity.entity_id: pl.player_id
                             for pl in p.players
                             if hasattr(pl.entity, "entity_id") and pl.player_id}
        elif name == "FullEntity":
            self._on_full_entity(p)
        elif name == "ShowEntity":
            self._on_show_entity(p)
        elif name == "HideEntity":
            self._on_hide_entity(p)
        elif name == "Choices":
            self._on_choices(p)
        elif name == "SendChoices":
            self._on_send_choices(p)
        elif name == "ChosenEntities":
            self._on_chosen(p)
        elif name == "ShuffleDeck":
            if self.friendly_key is not None:   # 调度阶段的洗牌是噪音
                self._emit_event({"kind": "shuffle",
                                  "actor": getattr(p, "player_id", None)})
        elif name == "Block":
            self._on_block(p)
        else:
            # 全量收录原则: 未解释的包记 raw(MetaData/Options/SubSpell/ChangeEntity...)
            self._record_raw(name, p)

    def _record_raw(self, ptype: str, p) -> None:
        """raw 事件: 入库(store.unhandled -> JSONL), 照常分发订阅, 但渲染层跳过。"""
        evt = {"kind": "raw", "packet_type": ptype, "payload": packet_payload(p)}
        self.unhandled.append(evt)
        if len(self.unhandled) > 500:
            del self.unhandled[:len(self.unhandled) - 500]
        self._emit_event(evt)

    def _key_of(self, entity) -> PlayerKey | None:
        if hasattr(entity, "player_id") and not isinstance(entity, int):
            return entity.player_id or None
        if isinstance(entity, int):
            g = self.game
            if g is not None and entity == g.id:
                return None                       # GameEntity
            pid = self._ent2pid.get(entity)
            if pid is not None:
                return pid
            e = self.get(entity)
            if isinstance(e, Player):
                return e.player_id
        return None

    def _on_tag_change(self, p, old: dict | None) -> None:
        tag, value, entity = p.tag, p.value, p.entity
        key = self._key_of(entity)
        if key is not None:
            if tag == GameTag.CURRENT_PLAYER and value == 1:
                self._on_turn_start(key)
            elif tag == GameTag.PLAYSTATE and value in _TERMINAL_PLAYSTATE \
                    and not self._ended:
                self._ended = True
                self._emit_event({"kind": "game_end", "actor": key})
            elif tag == GameTag.TURN:
                self.player_turn[key] = int(value or 0)
            elif tag in _MANA_TAGS:
                nm = _MANA_TAGS[tag]
                prev = (old or {}).get(tag)
                cur = int(value or 0)
                if key == self.friendly_key and prev is not None and prev != cur:
                    if nm == "res" and cur > prev:
                        self._emit_event({"kind": "text", "actor": key,
                                          "msg": f"水晶上限 {prev}→{cur}"})
                    elif nm == "temp" and cur > prev:
                        self._emit_event({"kind": "text", "actor": key,
                                          "msg": f"临时水晶 +{cur - prev}"})
            return
        if not isinstance(entity, int):
            return
        e = self.get(entity)
        if e is None:
            return
        if tag == GameTag.ZONE:
            self._on_zone_change(e, old, value)
        elif tag == GameTag.COST:
            self._on_cost_change(e, old, value)
        elif tag in (GameTag.DAMAGE, GameTag.ARMOR, GameTag.HEALTH) and is_hero(e):
            self._on_hero_attr(e, old, tag)

    def _on_zone_change(self, e, old, value) -> None:
        old_zone = (old or {}).get(GameTag.ZONE)
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        cid = e.card_id or self._hint_cid.get(e.id)
        if (value == Zone.DECK.value and old_zone == Zone.SETASIDE.value
                and ctrl == self.friendly_key and cid):
            self._emit_event({"kind": "back_to_deck", "card_id": cid, "actor": ctrl})
        elif (value == Zone.HAND.value and old_zone == Zone.DECK.value
              and ctrl == self.friendly_key and cid):
            # 已揭示实体再次抽到(探底/置底过的牌)——无 SHOW_ENTITY, 只有 ZONE 变更
            self._emit_draw(e.id, cid, ctrl)
        elif (value == Zone.GRAVEYARD.value and old_zone == Zone.PLAY.value
              and e.tags.get(GameTag.CARDTYPE) == CardType.MINION and cid):
            self._emit_event({"kind": "death", "actor": ctrl, "card_id": cid})

    def _on_cost_change(self, e, old, value) -> None:
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        cid = e.card_id or self._hint_cid.get(e.id)
        if ctrl != self.friendly_key or not cid:
            return
        old_cost = (old or {}).get(GameTag.COST)
        base = self.carddb.cost(cid)
        ref = old_cost if old_cost is not None else base   # 首次变动以面板费为基准
        if ref is not None and value != ref:
            self._emit_event({"kind": "cost", "card_id": cid,
                              "old": ref, "new": value, "actor": ctrl})

    def _on_hero_attr(self, e, old, tag) -> None:
        if not old:
            return
        h0 = old.get(GameTag.HEALTH)
        base = h0 if h0 is not None else 30
        d0 = int(old.get(GameTag.DAMAGE, 0) or 0)
        a0 = int(old.get(GameTag.ARMOR, 0) or 0)
        hp_old = max(0, base - d0)
        hp_new = max(0, e.tags.get(GameTag.HEALTH, 30) - e.tags.get(GameTag.DAMAGE, 0))
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        who = "我方英雄" if ctrl == self.friendly_key else "敌方英雄"
        if tag == GameTag.DAMAGE and d0 != e.tags.get(GameTag.DAMAGE, 0):
            self._emit_event({"kind": "text", "actor": ctrl,
                              "msg": f"{who} {hp_old}血→{hp_new}血"
                                     f"(甲{e.tags.get(GameTag.ARMOR, 0)})"})
        elif tag == GameTag.ARMOR and a0 != e.tags.get(GameTag.ARMOR, 0):
            self._emit_event({"kind": "text", "actor": ctrl,
                              "msg": f"{who}护甲 {a0}→{e.tags.get(GameTag.ARMOR, 0)}"
                                     f"(血{hp_new})"})

    def _on_turn_start(self, key: PlayerKey) -> None:
        prev, self.current = self.current, key
        if prev is None:
            # 首个行动方(先手的留牌回合)
            self._emit_event({"kind": "turn_start", "actor": key, "prev": None,
                              "first": True, "my_turn_no": 1, "total_turn": 1})
            return
        if prev == key or self._ended:
            return
        self._emit_event({"kind": "text", "actor": prev, "msg": "结束回合"})
        n = self.player_turn.get(key, 0) + 1   # 玩家级 TURN 标签在切换之后才到
        g = self.game
        fp = g.first_player if g is not None else None
        first_key = fp.player_id if fp is not None else None
        total = 2 * n - (1 if first_key == key else 0)
        self._emit_event({"kind": "turn_start", "actor": key, "prev": prev,
                          "first": False, "my_turn_no": n, "total_turn": total})

    # ---- 实体/揭示挂钩(Task 4 实现块/选择; 这两个在本任务即有行为) ----
    def _on_full_entity(self, p) -> None:
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        if (e is not None and e.zone == Zone.HAND and p.card_id
                and self.ctrl_key(e) == self.friendly_key):
            creator = self.get(e.tags.get(GameTag.CREATOR, 0) or 0)
            self._emit_event({"kind": "gain", "card_id": p.card_id,
                              "actor": self.ctrl_key(e),
                              "creator": getattr(creator, "card_id", None)})

    def _on_show_entity(self, p) -> None:
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        if e is None:
            return
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(p.entity)
        if e.zone == Zone.HAND and ctrl == self.friendly_key and p.card_id:
            self._emit_draw(p.entity, p.card_id, ctrl)

    def _on_hide_entity(self, p) -> None:
        # 库缺口: entity.hide() 只撤 revealed, 不落 ZONE —— 换牌/洗回后
        # "再次抽到"的区域变更识别依赖这里补写(spec §2.2 自研项)
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        zone_val = getattr(p, "zone", None)
        if e is not None and zone_val is not None:
            try:
                e.tags[GameTag.ZONE] = int(zone_val)
            except (TypeError, ValueError):
                pass

    def _emit_draw(self, eid: int, cid: str, actor) -> None:
        now = time.monotonic()
        last = self._draw_ts.get(eid)
        if last is not None and now - last < 0.5:
            return   # 同一实体的多重揭示路径只报一次
        self._draw_ts[eid] = now
        self._emit_event({"kind": "draw", "card_id": cid, "actor": actor})
