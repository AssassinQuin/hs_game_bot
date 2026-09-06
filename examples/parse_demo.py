"""hslog 解析演示 —— 基于真实 Power.log

炉石客户端会把每局对局以结构化文本写进 Power.log,hslog(HearthSim 出品,
hsreplay.net 同款解析器)负责把它还原成对局对象树。

使用流程:
  1. 按 GameState.DebugPrintPower() - CREATE_GAME 把日志切成"每局一段"
  2. 每段用独立的 LogParser 解析(PlayerManager 全局共享,同一玩家在多局中
     PlayerID 可能不同,混着解析会抛 InconsistentPlayerIdError)
  3. EntityTreeExporter 把数据包树重建为实体树(玩家/英雄/随从/牌,含最终标签)
  4. 从实体树读:胜负 / 英雄血量 / 场面随从 / 手牌数 / 牌库数 / 回合数
     (如需 HSReplay XML,用官方 hsreplay 包,本演示不含)

运行: python examples/parse_demo.py
"""
import logging

from hslog import LogParser
from hslog.export import EntityTreeExporter
from hearthstone.enums import CardType, GameTag, PlayState, Zone

LOG_FILE = "examples/Power.log"


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


def parse_game(lines):
    """解析单局,返回 LogParser。真实日志偶尔有单行异常,跳过不中断。"""
    parser = LogParser()
    for line in lines:
        try:
            parser.read_line(line)
        except Exception as exc:  # noqa: BLE001
            logging.debug("skip: %r (%s)", line.strip()[:80], exc)
    return parser


def build_game(parser):
    """把数据包树重建为实体树,返回 (game 实体树, 玩家名映射 PlayerManager)。"""
    exporter = EntityTreeExporter(
        parser.games[-1],
        player_manager=parser.player_manager,
        tolerate_missing_entities=True,  # 脏行跳过后,引用缺失实体时不再炸
    )
    exporter.export()
    return exporter.game


def describe_player(game, pm, player):
    pid = player.id
    ref = pm.get_player_by_entity_id(pid)
    name = ref.name if ref and ref.name else f"Player{player.tags.get(GameTag.PLAYER_ID)}"
    result = PlayState(player.tags[GameTag.PLAYSTATE]).name

    board, hand, deck = [], 0, 0
    for e in game.entities:
        ctl = getattr(e, "controller", None)
        if ctl is None or ctl.id != pid:  # controller 是 Player 实体对象,不是 ID
            continue
        ctype = e.tags.get(GameTag.CARDTYPE)
        if e.zone == Zone.PLAY and ctype == CardType.MINION:
            board.append(f"{e.card_id}({e.tags.get(GameTag.ATK, 0)}/{e.tags.get(GameTag.HEALTH, 0)})")
        elif e.zone == Zone.HAND:
            hand += 1
        elif e.zone == Zone.DECK:
            deck += 1

    hero = next(
        (e for e in game.entities
         if getattr(e, "controller", None) is not None
         and e.controller.id == pid
         and e.tags.get(GameTag.CARDTYPE) == CardType.HERO),
        None,
    )
    hero_info = (f"英雄={hero.card_id} 血量={hero.tags.get(GameTag.HEALTH)} "
                 f"区域={hero.zone.name}") if hero else "英雄=?"
    return name, result, hero_info, board, hand, deck


def main():
    for idx, chunk in enumerate(iter_game_chunks(LOG_FILE), 1):
        parser = parse_game(chunk)
        game = build_game(parser)
        pm = parser.player_manager

        print(f"================ 对局 {idx} ================")
        print(f"回合数: {game.tags.get(GameTag.TURN)}(含双方出牌回合)")

        for player in game.players:
            name, result, hero_info, board, hand, deck = describe_player(game, pm, player)
            print(f"  玩家 {name:<16} 结果={result:<5} {hero_info}")
            print(f"    手牌 {hand} 张 | 牌库 {deck} 张 | 场面: {' '.join(board) if board else '(空)'}")
        print()


if __name__ == "__main__":
    main()
