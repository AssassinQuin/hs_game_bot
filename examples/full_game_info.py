"""完整对局信息 demo —— 提取最新一局的所有关键信息

对照官方测试(tests/test_parser.py、tests/test_export.py)中的用法:
  packet_tree = parser.games[-1]
  friendly    = FriendlyPlayerExporter(packet_tree).export()   # "我"的实体ID
  game        = EntityTreeExporter(packet_tree, player_manager=...).export().game
  game.get_player(player_id) / game.find_entity_by_id(id) / game.in_zone(Zone)

覆盖的关键信息:
  [对局元信息] 游戏模式 / 构建号 / 时间跨度
  [玩家]       战网名 / 账号 / 是否本机玩家 / 先手 / 胜负
  [英雄]       英雄卡 / 血量 / 护甲 / 英雄技能
  [牌张]       手牌列表 / 牌库张数 / 场面随从(攻血+关键词) / 武器 / 奥秘
  [过程统计]   回合数 / 双方出牌序列 / 攻击事件 / 起手调度
  [结果]       胜者 / 败者英雄位置(PLAY/GRAVEYARD)

运行: python examples/full_game_info.py  [日志路径,默认 examples/Power.log]
"""
import sys

from hearthstone.enums import BlockType, CardType, GameTag, PlayState, Zone
from hslog import packets

from hslog_utils import build_game, iter_game_chunks, parse_game, resolve_player_name

# 关键词标签 -> 中文名
KEYWORDS = {
    GameTag.TAUNT: "嘲讽", GameTag.DIVINE_SHIELD: "圣盾", GameTag.WINDFURY: "风怒",
    GameTag.STEALTH: "潜行", GameTag.RUSH: "突袭", GameTag.CHARGE: "冲锋",
    GameTag.LIFESTEAL: "吸血", GameTag.REBORN: "复生", GameTag.FROZEN: "冻结",
    GameTag.STEALTH: "潜行",
}

GAME_TYPE_CN = {
    "GT_RANKED": "排名模式(天梯)", "GT_CASUAL": "休闲模式", "GT_VS_AI": "对抗AI",
    "GT_TAVERNBRAWL": "酒馆乱斗", "GT_BATTLEGROUNDS": "酒馆战棋",
}
FORMAT_TYPE_CN = {"FT_STANDARD": "标准", "FT_WILD": "狂野", "FT_CLASSIC": "经典"}


def walk_packets(node):
    """深度优先、按时间顺序遍历数据包(含块内)。"""
    for p in node.packets:
        yield p
        if isinstance(p, packets.Block):
            yield from walk_packets(p)


def card_id_of(game, entity_id):
    e = game.find_entity_by_id(entity_id)
    return getattr(e, "card_id", None) if e else None


def describe_minion(e):
    kw = " ".join(cn for tag, cn in KEYWORDS.items() if e.tags.get(tag))
    atk, hp = e.tags.get(GameTag.ATK, "?"), e.tags.get(GameTag.HEALTH, "?")
    return f"{e.card_id}({atk}/{hp}{' ' + kw if kw else ''})"


def player_block(game, parser, player, friendly_id):
    """单个玩家的完整状态。"""
    name = resolve_player_name(parser, player)
    ref = parser.player_manager.get_player_by_entity_id(player.id)
    playstate = PlayState(player.tags.get(GameTag.PLAYSTATE, 0)).name

    hero = player.hero  # hearthstone.entities.Player 自带 hero 属性
    if hero is None:
        hero = next((e for e in game.entities
                     if getattr(e, "controller", None) is not None
                     and e.controller.id == player.id
                     and e.tags.get(GameTag.CARDTYPE) == CardType.HERO), None)
    power = next((e for e in game.entities
                  if getattr(e, "controller", None) is not None
                  and e.controller.id == player.id
                  and e.tags.get(GameTag.CARDTYPE) == CardType.HERO_POWER), None)

    hand = [e for e in game.entities
            if getattr(e, "controller", None) is not None and e.controller.id == player.id
            and e.zone == Zone.HAND]
    deck = [e for e in game.entities
            if getattr(e, "controller", None) is not None and e.controller.id == player.id
            and e.zone == Zone.DECK]
    board = [e for e in game.in_zone(Zone.PLAY)
             if getattr(e, "controller", None) is not None and e.controller.id == player.id
             and e.tags.get(GameTag.CARDTYPE) == CardType.MINION]
    weapon = next((e for e in game.in_zone(Zone.PLAY)
                   if getattr(e, "controller", None) is not None and e.controller.id == player.id
                   and e.tags.get(GameTag.CARDTYPE) == CardType.WEAPON), None)
    secrets = [e for e in game.in_zone(Zone.SECRET)
               if getattr(e, "controller", None) is not None and e.controller.id == player.id]

    lines = [
        f"{'▶ 本机玩家' if player.tags.get(GameTag.PLAYER_ID) == friendly_id else '  对手    '} "
        f"{name}  (entity={player.id}, player_id={player.tags.get(GameTag.PLAYER_ID)}, "
        f"lo={getattr(ref, 'lo', None)})  结果={playstate}"
        f"{' | 先手' if player.tags.get(GameTag.FIRST_PLAYER) else ''}",
        f"    英雄: {hero.card_id if hero else '?'}"
        f"  血量={hero.tags.get(GameTag.HEALTH, '?') if hero else '?'}"
        f"  护甲={hero.tags.get(GameTag.ARMOR, 0) if hero else '?'}"
        f"  区域={hero.zone.name if hero else '?'}"
        f"  技能={power.card_id if power else '?'}",
        f"    牌库 {len(deck)} 张 | 手牌 {len(hand)} 张: "
        f"[{', '.join(e.card_id or '(未揭示)' for e in hand)}]",
        f"    场面 {len(board)} 个: " + " ".join(describe_minion(e) for e in board),
    ]
    if weapon:
        lines.append(f"    武器: {weapon.card_id}"
                     f"(攻{weapon.tags.get(GameTag.ATK, '?')}/耐久{weapon.tags.get(GameTag.HEALTH, '?')})")
    if secrets:
        lines.append(f"    奥秘 {len(secrets)} 个: " + " ".join(e.card_id or "?" for e in secrets))
    return "\n".join(lines), (name, playstate)


def match_stats(game, packet_tree, parser):
    """过程统计:出牌序列 + 攻击事件(按官方测试的 packets.Block 遍历方式)。"""
    turn = 0
    plays, attacks = [], []
    for p in walk_packets(packet_tree):
        if isinstance(p, packets.TagChange) and p.tag == GameTag.TURN:
            turn = p.value
        elif isinstance(p, packets.Block) and p.type == BlockType.PLAY:
            e = game.find_entity_by_id(p.entity) if p.entity else None
            who = resolve_player_name(parser, e.controller) if getattr(e, "controller", None) else "?"
            plays.append((turn, who, getattr(e, "card_id", None) or "?"))
        elif isinstance(p, packets.Block) and p.type == BlockType.ATTACK:
            atk = card_id_of(game, p.entity) or "?"
            dfn = card_id_of(game, p.target) or "?"
            attacks.append((turn, atk, dfn))
    return plays, attacks


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "examples/Power.log"
    chunks = list(iter_game_chunks(path))
    if not chunks:
        sys.exit(f"{path} 中没有找到对局")
    latest = chunks[-1]  # ← 最新开始的一局

    parser = parse_game(latest)
    game, friendly_id = build_game(parser)

    meta = parser.game_meta  # GameState.DebugPrintGame() 行写入的元信息
    gt = getattr(meta.get("GameType"), "name", meta.get("GameType"))
    ft = getattr(meta.get("FormatType"), "name", meta.get("FormatType"))
    print("=" * 64)
    print("最新一局完整对局信息")
    print("=" * 64)
    print(f"模式: {GAME_TYPE_CN.get(gt, gt)}"
          f" / {FORMAT_TYPE_CN.get(ft, ft)}"
          f"  |  客户端 BuildNumber={meta.get('BuildNumber')}"
          f"  |  ScenarioID={meta.get('ScenarioID')}")

    pkts = list(parser.games[-1].packets)
    if pkts:
        def secs(t):  # datetime.time -> 当日秒数
            return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
        start, end = secs(pkts[0].ts), secs(pkts[-1].ts)
        if end < start:  # 跨午夜
            end += 86400
        print(f"对局时长: ~{end - start:.0f} 秒")

    print(f"回合数: {game.tags.get(GameTag.TURN)}(含双方出牌回合)")
    print(f"当前行动方: {resolve_player_name(parser, game.current_player) if game.current_player else '?'}")
    print()

    results = {}
    for player in game.players:
        text, res = player_block(game, parser, player, friendly_id)
        print(text)
        print()
        results[res[0]] = res[1]

    plays, attacks = match_stats(game, parser.games[-1], parser)
    print(f"过程统计: 出牌 {len(plays)} 次, 攻击 {len(attacks)} 次")
    print("出牌序列(回合 / 玩家 / 卡牌):")
    for t, who, cid in plays:
        print(f"  T{t}  {who:<16} {cid}")

    mulligan = getattr(parser, "mulligan_choices", {})
    if mulligan:
        print("起手调度:")
        for pid, choice in mulligan.items():
            print(f"  player_id={pid}: 提供 {len(choice.choices)} 张, 保留 "
                  f"{choice.chosen if hasattr(choice, 'chosen') else '?'}")

    winners = [n for n, s in results.items() if s == "WON"]
    print(f"\n>>> 胜者: {', '.join(winners) or '?'}")


if __name__ == "__main__":
    main()
