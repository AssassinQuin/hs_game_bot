"""测试基建 —— 直接构造 hslog packet 喂 store(不经日志行, 格式零风险)。

构造器签名与 hslog 1.20 packets.py 对齐; tags 一律 [(GameTag, int)] 列表。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hearthstone.enums import BlockType, CardType, ChoiceType, GameTag, Zone
from hslog import packets
from hslog.player import PlayerManager

TS = "2026-09-08 20:00:00.0000000"


def mk_pm() -> PlayerManager:
    """两个玩家: entity 2=pid1(湫然#51704), entity 3=pid2(对手)。"""
    pm = PlayerManager()
    pm.create_or_update_player(entity_id=2, player_id=1, is_ai=False)
    pm.create_or_update_player(entity_id=3, player_id=2, is_ai=False)
    return pm


def mk_create_game() -> packets.CreateGame:
    pm = mk_pm()
    p = packets.CreateGame(TS, 1)
    p.tags = []
    p.players = [
        packets.CreateGame.Player(TS, pm.get_player_by_entity_id(2), 1, 2, 1),
        packets.CreateGame.Player(TS, pm.get_player_by_entity_id(3), 2, 2, 2),
    ]
    return p


def mk_full(eid: int, cid: str | None, **tags: int) -> packets.FullEntity:
    """tags 关键字 = GameTag 成员名(如 ZONE=1 / CARDTYPE=4), 值为裸 int。"""
    p = packets.FullEntity(TS, eid, cid)
    p.tags = [(GameTag[k], v) for k, v in tags.items()]
    return p


def mk_show(eid: int, cid: str, **tags: int) -> packets.ShowEntity:
    p = packets.ShowEntity(TS, eid, cid)
    p.tags = [(GameTag[k], v) for k, v in tags.items()]
    return p


def mk_tag(entity, tag: GameTag, value: int) -> packets.TagChange:
    return packets.TagChange(TS, entity, tag, value)


def mk_block(btype: BlockType, entity, target=0) -> packets.Block:
    b = packets.Block(TS, entity, btype, None, None, None, target, None, None)
    return b


def mk_choices_mulligan(pid_entity: int, cid: int, eids: list[int]) -> packets.Choices:
    p = packets.Choices(TS, pid_entity, cid, 1, ChoiceType.MULLIGAN, 0, len(eids))
    p.choices = list(eids)
    return p


def mk_send_mulligan(cid: int, eids: list[int]) -> packets.SendChoices:
    p = packets.SendChoices(TS, cid, ChoiceType.MULLIGAN)
    p.choices = list(eids)
    return p


def mk_choices_general(pid_entity: int, cid: int, eids: list[int]) -> packets.Choices:
    p = packets.Choices(TS, pid_entity, cid, 2, ChoiceType.GENERAL, 1, len(eids))
    p.choices = list(eids)
    return p


def mk_send_general(cid: int, eids: list[int]) -> packets.SendChoices:
    p = packets.SendChoices(TS, cid, ChoiceType.GENERAL)
    p.choices = list(eids)
    return p


class EventLog:
    """store.subscribe 的收集器。"""

    def __init__(self):
        self.events: list[dict] = []

    def __call__(self, evt: dict) -> None:
        self.events.append(dict(evt))

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def by_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]


def hero_packets(eid: int, cid: str, ctrl: int) -> list:
    return [mk_full(eid, cid, CARDTYPE=CardType.HERO.value, ZONE=Zone.PLAY.value,
                    CONTROLLER=ctrl, HEALTH=30)]
