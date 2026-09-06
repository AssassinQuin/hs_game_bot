"""适配层 —— 全项目唯一允许 import hslog/hearthstone 实体类的模块(DESIGN.md §3 铁律)。

职责: 实体树 → GameState 快照, 并统一玩家命名空间(CONTROLLER 标签值 = PLAYER_ID)。
"""
from __future__ import annotations

import logging

from hearthstone.enums import GameTag
from hslog import LogParser
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter
from hslog import packets

from .gamestate import Entity, GameState, PlayerInfo, PlayerKey

log = logging.getLogger("hsbot.adapter")


class _TolerantExporter(EntityTreeExporter):
    """按包容错的导出器: 单个脏包(如 FullEntity 引用玩家名)只跳过该包, 不炸整局快照。"""

    def handle_full_entity(self, packet):
        if not isinstance(packet.entity, int):
            log.debug("跳过脏 FullEntity: %r", packet.entity)
            return
        try:
            super().handle_full_entity(packet)
        except Exception as exc:  # noqa: BLE001
            log.debug("FullEntity 导出失败: %s", exc)

    def handle_show_entity(self, packet):
        try:
            super().handle_show_entity(packet)
        except Exception as exc:  # noqa: BLE001
            log.debug("ShowEntity 导出失败: %s", exc)

    def handle_tag_change(self, packet):
        try:
            super().handle_tag_change(packet)
        except Exception as exc:  # noqa: BLE001
            log.debug("TagChange 导出失败: %s", exc)

    def handle_hide_entity(self, packet):
        try:
            super().handle_hide_entity(packet)
        except Exception as exc:  # noqa: BLE001
            log.debug("HideEntity 导出失败: %s", exc)


def new_parser() -> LogParser:
    return LogParser()


def feed_line(parser: LogParser, line: str) -> None:
    """脏行逐行跳过, 不让单行异常打断整局解析(M1_MONITOR §3)。"""
    try:
        parser.read_line(line)
    except Exception as exc:  # noqa: BLE001
        log.debug("skip line: %.80r (%s)", line, exc)


def resolve_friendly(parser: LogParser, battletag: str = "") -> int | None:
    """链路级友方探测。

    战网名优先: exporter 的"第一条 SHOW_ENTITY 属于友方"启发式在镜像对局/
    对手手牌被提前揭示时会翻转(实测同一会话内不同局可能给出不同答案),
    而本机战网名是跨局稳定的硬事实。exporter 仅作未配置时的兜底。
    """
    if not parser.games:
        return None
    tree = parser.games[-1]
    if battletag:
        for p in tree.packets:
            if isinstance(p, packets.CreateGame):
                for pl in p.players:
                    ref = pl.entity
                    name = getattr(ref, "name", None)
                    if name and (name == battletag or name.split("#")[0] == battletag):
                        return pl.player_id
                break
    try:
        return FriendlyPlayerExporter(tree).export()
    except Exception as exc:  # noqa: BLE001
        log.debug("friendly 探测失败: %s", exc)
        return None


def export_game_state(parser: LogParser, lines_consumed: int = 0,
                      battletag: str = "") -> GameState | None:
    """当前最新一局的完整快照。导出失败返回 None(该局快照不可用, 链路不受影响)。"""
    if not parser.games:
        return None
    tree = parser.games[-1]

    friendly_raw: int | None = None
    try:
        friendly_raw = FriendlyPlayerExporter(tree).export()
    except Exception as exc:  # noqa: BLE001
        log.debug("friendly 探测失败: %s", exc)

    try:
        exporter = _TolerantExporter(
            tree, player_manager=parser.player_manager, tolerate_missing_entities=True)
        exporter.export()
        game = exporter.game
    except Exception as exc:  # noqa: BLE001  兜底: 单包容错后仍失败才放弃整局快照
        log.warning("实体树导出失败: %s", exc)
        return None

    # ---- 玩家表 (PLAYER_KEY = PLAYER_ID) ----
    players: dict[PlayerKey, PlayerInfo] = {}
    for p in game.players:
        key = p.player_id or p.tags.get(GameTag.PLAYER_ID)
        if key is None:
            continue
        pref = parser.player_manager.get_player_by_entity_id(p.id)
        name = (pref.name if pref and pref.name else "") or f"Player{key}"
        players[key] = PlayerInfo(key=key, entity_id=p.id, name=name)

    # ---- 友方判定: 战网名优先(跨局稳定), exporter 兜底 ----
    friendly: PlayerKey | None = None
    if battletag:
        for k, info in players.items():
            if info.name == battletag or info.name.split("#")[0] == battletag:
                friendly = k
                break
    if friendly is None and friendly_raw in players:
        friendly = friendly_raw

    entities: dict[int, Entity] = {}
    for e in game.entities:
        ctrl = getattr(e, "controller", None)
        entities[e.id] = Entity(
            id=e.id,
            card_id=getattr(e, "card_id", None),
            controller_key=(ctrl.player_id if ctrl is not None else None),
            tags=dict(e.tags),
        )

    meta = {str(k): str(getattr(v, "name", v))
            for k, v in (parser.game_meta or {}).items()}
    return GameState(
        entities=entities,
        game_tags=dict(game.tags),
        players=players,
        friendly_key=friendly,
        meta=meta,
        lines_consumed=lines_consumed,
    )


# ================= 语料导出用: packet 归一化(corpus.py 消费) =================

def jsonable(v):
    """枚举→名字, packet→递归字典, PlayerReference→字典, 其余原样; 兜底 str()。"""
    if v is None or isinstance(v, (bool, float, str)):
        return v
    if type(v).__module__ == "hslog.packets":              # 嵌套 packet(如 CreateGame.Player)
        return packet_payload(v)
    if hasattr(v, "entity_id") and hasattr(v, "player_id"):  # PlayerReference
        return {"name": getattr(v, "name", None), "entity_id": v.entity_id,
                "player_id": v.player_id}
    name = getattr(v, "name", None)                        # 枚举(IntEnum 含)
    if isinstance(name, str):
        return name
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    return str(v)


def walk_packets(tree):
    """DFS 前序平铺 packet 树, 产出 (packet, depth)。"""
    def rec(node, depth):
        for p in node.packets:
            yield p, depth
            if isinstance(p, packets.Block):
                yield from rec(p, depth + 1)
    yield from rec(tree, 0)


def packet_payload(p) -> dict:
    """packet 的全字段归一化(排除 ts 与子包列表), 子包由 walk_packets 负责。"""
    out = {}
    for k, v in vars(p).items():
        if k in ("ts", "packets"):
            continue
        out[k] = jsonable(v)
    return out
