"""hslog 公共工具:切局 / 解析 / 实体树构建 / 玩家名解析"""
import logging

from hslog import LogParser
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter
from hearthstone.enums import GameTag


def iter_game_chunks(path):
    """把整份日志按 CREATE_GAME 切成单局片段,CREATE_GAME 行归入新段。

    首局之前可能还有 PowerList 等杂项行,不属于任何对局,直接丢弃。
    """
    chunk = None
    with open(path, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            if "GameState.DebugPrintPower() - CREATE_GAME" in line:
                if chunk:
                    yield chunk
                chunk = [line]
            elif chunk is not None:
                chunk.append(line)
    if chunk:
        yield chunk


def parse_lines(lines, parser=None):
    """逐行喂给 LogParser。真实日志偶尔有单行异常,跳过不中断。"""
    parser = parser or LogParser()
    for line in lines:
        try:
            parser.read_line(line)
        except Exception as exc:  # noqa: BLE001
            logging.debug("skip: %r (%s)", line.strip()[:80], exc)
    return parser


def parse_game(lines):
    """解析单局片段,返回 LogParser。"""
    return parse_lines(lines)


def build_game(parser):
    """数据包树 -> 实体树。返回 (game 实体树, 友方玩家实体ID)。"""
    packet_tree = parser.games[-1]
    friendly = FriendlyPlayerExporter(packet_tree).export()
    exporter = EntityTreeExporter(
        packet_tree,
        player_manager=parser.player_manager,
        tolerate_missing_entities=True,  # 脏行跳过后,引用缺失实体时不再炸
    )
    exporter.export()
    return exporter.game, friendly


def resolve_player_name(parser, player_entity):
    """玩家名:优先 PlayerManager 里注册的战网名,否则用 PlayerID 占位。"""
    ref = parser.player_manager.get_player_by_entity_id(player_entity.id)
    if ref and ref.name:
        return ref.name
    return f"Player{player_entity.tags.get(GameTag.PLAYER_ID, '?')}"
