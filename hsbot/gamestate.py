"""游戏状态抽象 —— 按 Gamestate Protocol 建模(忠实标签桶), 见 DESIGN.md §3.1。

命名空间铁律: PlayerKey = PLAYER_ID(1/2)。
hslog 的 FriendlyPlayerExporter 返回的就是 PLAYER_ID; 而实体树里
e.controller.id 是玩家实体 id(2/3), 两者混用会主客颠倒 —— adapter 负责统一翻译。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from hearthstone.enums import CardType, GameTag, Zone

PlayerKey = int  # PLAYER_ID: 1=先手, 2=后手(硬币)


@dataclass
class Entity:
    """单个实体的完整快照。zone/controller 都是协议标签, 这里只做只读视图。"""

    id: int
    card_id: str | None
    controller_key: PlayerKey | None
    tags: dict[GameTag, int] = field(default_factory=dict)

    @property
    def zone(self) -> Zone:
        return Zone(self.tags.get(GameTag.ZONE, Zone.INVALID.value))

    @property
    def cardtype(self) -> CardType:
        return CardType(self.tags.get(GameTag.CARDTYPE, CardType.INVALID.value))

    @property
    def atk(self) -> int:
        return self.tags.get(GameTag.ATK, 0)

    @property
    def hp_total(self) -> int:
        return max(0, self.tags.get(GameTag.HEALTH, 0)
                   + self.tags.get(GameTag.ARMOR, 0)
                   - self.tags.get(GameTag.DAMAGE, 0))

    @property
    def taunt(self) -> bool:
        return bool(self.tags.get(GameTag.TAUNT))

    @property
    def generated(self) -> bool:
        """衍生牌: 有 CREATOR 标签(不占牌库张数)。"""
        return GameTag.CREATOR in self.tags

    @property
    def zone_position(self) -> int:
        return self.tags.get(GameTag.ZONE_POSITION, 0)


@dataclass
class PlayerInfo:
    key: PlayerKey
    entity_id: int
    name: str


@dataclass
class GameState:
    """一局某一时刻的完整快照(可 deepcopy/导出, 见 to_dict)。"""

    entities: dict[int, Entity]
    game_tags: dict[GameTag, int]
    players: dict[PlayerKey, PlayerInfo]
    friendly_key: PlayerKey | None
    meta: dict[str, str] = field(default_factory=dict)
    lines_consumed: int = 0

    # ---------- 基础 ----------
    def get(self, entity_id: int) -> Entity | None:
        return self.entities.get(entity_id)

    def player_keys(self) -> list[PlayerKey]:
        return sorted(self.players)

    def opponent_key(self) -> PlayerKey | None:
        if self.friendly_key is None:
            return None
        for k in self.players:
            if k != self.friendly_key:
                return k
        return None

    def current_key(self) -> PlayerKey | None:
        for k, info in self.players.items():
            e = self.entities.get(info.entity_id)
            if e and e.tags.get(GameTag.CURRENT_PLAYER):
                return k
        return None

    @property
    def turn(self) -> int:
        return self.game_tags.get(GameTag.TURN, 0)

    def friendly_turn_number(self) -> int:
        """我的第 N 回合(TURN 是半回合制, 按 FIRST_PLAYER 换算)。"""
        raw = self.turn
        first = None
        for k, info in self.players.items():
            e = self.entities.get(info.entity_id)
            if e and e.tags.get(GameTag.FIRST_PLAYER):
                first = k
                break
        mine_is_first = first == self.friendly_key
        return (raw + 1) // 2 if mine_is_first else raw // 2

    def is_my_turn(self) -> bool:
        return self.friendly_key is not None and self.current_key() == self.friendly_key

    def name(self, key: PlayerKey | None) -> str:
        if key is None:
            return "?"
        return self.players[key].name if key in self.players else str(key)

    # ---------- 区域查询 ----------
    def _of(self, key: PlayerKey | None) -> Iterator[Entity]:
        for e in self.entities.values():
            if key is None or e.controller_key == key:
                yield e

    def hero(self, key: PlayerKey) -> Entity | None:
        heroes = [e for e in self._of(key) if e.cardtype == CardType.HERO]
        heroes.sort(key=lambda e: 0 if e.zone == Zone.PLAY else 1)
        return heroes[0] if heroes else None

    def hero_total_hp(self, key: PlayerKey) -> int:
        hero = self.hero(key)
        return hero.hp_total if hero else 0

    def hero_hp(self, key: PlayerKey) -> int:
        """当前血量(不含护甲) = 生命 − 已受伤害。"""
        hero = self.hero(key)
        if hero is None:
            return 0
        return max(0, hero.tags.get(GameTag.HEALTH, 0) - hero.tags.get(GameTag.DAMAGE, 0))

    def hero_armor(self, key: PlayerKey) -> int:
        hero = self.hero(key)
        return hero.tags.get(GameTag.ARMOR, 0) if hero else 0

    def hand(self, key: PlayerKey) -> list[Entity]:
        cards = [e for e in self._of(key) if e.zone == Zone.HAND
                 and e.cardtype in (CardType.SPELL, CardType.MINION, CardType.WEAPON,
                                    CardType.HERO, CardType.ITEM, CardType.TOKEN,
                                    CardType.INVALID)]
        cards.sort(key=lambda e: e.zone_position)
        return cards

    def board(self, key: PlayerKey) -> list[Entity]:
        minions = [e for e in self._of(key) if e.zone == Zone.PLAY
                   and e.cardtype == CardType.MINION]
        minions.sort(key=lambda e: e.zone_position)
        return minions

    def deck_entities(self, key: PlayerKey) -> list[Entity]:
        return [e for e in self._of(key) if e.zone == Zone.DECK]

    def deck_count(self, key: PlayerKey) -> int:
        return len(self.deck_entities(key))

    def graveyard_count(self, key: PlayerKey) -> int:
        return sum(1 for e in self._of(key) if e.zone == Zone.GRAVEYARD)

    def secret_count(self, key: PlayerKey) -> int:
        return sum(1 for e in self._of(key) if e.zone == Zone.SECRET)

    def mana_fields(self, key: PlayerKey) -> dict[str, int]:
        e = self.entities.get(self.players[key].entity_id)
        t = e.tags if e else {}
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

    def playstate(self, key: PlayerKey) -> str:
        e = self.entities.get(self.players[key].entity_id)
        v = e.tags.get(GameTag.PLAYSTATE, 0) if e else 0
        try:
            return __import__("hearthstone.enums", fromlist=["PlayState"]).PlayState(v).name
        except Exception:  # noqa: BLE001
            return str(v)

    # ---------- 导出 ----------
    def to_dict(self) -> dict:
        def tn(k: GameTag) -> str:
            return k.name if hasattr(k, "name") else str(k)

        me, opp = self.friendly_key, self.opponent_key()
        hand = [{"id": e.card_id, "name": None, "pos": e.zone_position,
                 "cost": e.tags.get(GameTag.COST), "generated": e.generated}
                for e in (self.hand(me) if me is not None else [])]
        return {
            "turn": self.turn,
            "friendly_turn": self.friendly_turn_number(),
            "my_turn": self.is_my_turn(),
            "players": {k: {"name": i.name} for k, i in self.players.items()},
            "me": {
                "hp": self.hero_total_hp(me) if me is not None else 0,
                "armor": self.hero_armor(me) if me is not None else 0,
                "mana": self.mana_fields(me) if me is not None else {},
                "deck": self.deck_count(me) if me is not None else 0,
                "hand": hand,
                "board": [{"card_id": e.card_id, "atk": e.atk, "hp": e.hp_total,
                           "taunt": e.taunt} for e in (self.board(me) if me is not None else [])],
            },
            "opp": {
                "hp": self.hero_total_hp(opp) if opp is not None else 0,
                "armor": self.hero_armor(opp) if opp is not None else 0,
                "deck": self.deck_count(opp) if opp is not None else 0,
                "hand_n": len(self.hand(opp)) if opp is not None else 0,
                "board": [{"card_id": e.card_id, "atk": e.atk, "hp": e.hp_total,
                           "taunt": e.taunt} for e in (self.board(opp) if opp is not None else [])],
            },
        }
