"""适配层 —— hslog/hearthstone 解析类唯一入口(DESIGN.md §3 铁律, 2026-09-07 spec 修订)。

行→packet(LogParser); StoreExporter = 库状态机(容错, GameStore 用;
不回调, 衍生在 store.apply 内完成); 友方探测; 语料导出用的 packet 归一化。
"""
from __future__ import annotations

import logging

from hearthstone.enums import BlockType
from hslog import LogParser
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter
from hslog import packets

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


class StoreExporter(_TolerantExporter):
    """库状态机(容错): 逐包 export_packet 维护 hearthstone.entities;块不迭代子包
    (游标逐包驱动)。状态衍生在 GameStore.apply 内完成,本类不回调。

    铁律: hslog 导出类不出本模块。
    """

    def __init__(self, packet_tree, player_manager) -> None:
        super().__init__(packet_tree, player_manager=player_manager,
                         tolerate_missing_entities=True)

    def handle_change_entity(self, packet):
        try:
            super().handle_change_entity(packet)
        except Exception as exc:  # noqa: BLE001
            log.debug("ChangeEntity 导出失败: %s", exc)

    def handle_block(self, packet):
        if packet.type == BlockType.GAME_RESET and self.game is not None:
            self.game.reset()
        # 不调 super(): 不迭代子包(子包由游标逐包驱动)

    def handle_sub_spell(self, packet):
        pass  # 同上, 子包由游标驱动


def new_store_exporter(packet_tree, player_manager) -> StoreExporter:
    return StoreExporter(packet_tree, player_manager)


def is_block(p) -> bool:
    return isinstance(p, packets.Block)


def is_play_block(p) -> bool:
    return isinstance(p, packets.Block) and p.type == BlockType.PLAY


def game_meta(parser) -> dict[str, str]:
    """parser.game_meta 的字符串化(快照 meta 用, 原 export_game_state 内联逻辑)。"""
    return {str(k): str(getattr(v, "name", v))
            for k, v in (parser.game_meta or {}).items()}


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
