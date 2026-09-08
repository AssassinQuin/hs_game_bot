"""fixture 必须真的被 hslog 解析成预期 packet(行格式护栏)。"""
from hslog import packets as P
from hslog import LogParser

from .conftest import TS, mk_create_game, mk_full, mk_pm


def _parse(path):
    parser = LogParser()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            parser.read_line(line + "\n")
        except Exception:
            pass
    return parser


def test_mini_game_parses(tmp_path=None):
    from pathlib import Path
    parser = _parse(Path(__file__).parent / "fixtures" / "mini_game.log")
    assert len(parser.games) == 1
    flat = list(parser.games[0].recursive_iter()) if hasattr(parser.games[0], "recursive_iter") else []
    # recursive_iter 在 1.18/1.20 均有: PacketTree.recursive_iter(cls=None) 递归产出
    kinds = [type(p).__name__ for p in flat]
    for expect in ("CreateGame", "FullEntity", "ShowEntity", "TagChange", "Block"):
        assert expect in kinds, f"fixture 缺 {expect}: {kinds}"
    assert kinds.count("Block") == 3          # PLAY + ATTACK + TRIGGER
    from hearthstone.enums import BlockType
    blocks = [p for p in flat if type(p).__name__ == "Block"]
    assert {b.type for b in blocks} >= {BlockType.PLAY, BlockType.ATTACK, BlockType.TRIGGER}


def test_constructed_packets_drive_exporter():
    """conftest 构造器喂 EntityTreeExporter 能建实体树(与库对齐的护栏)。"""
    from hearthstone.enums import CardType, GameTag, Zone
    from hslog.export import EntityTreeExporter

    pm = mk_pm()
    ex = EntityTreeExporter(_Tree(), player_manager=pm, tolerate_missing_entities=True)
    ex.export_packet(mk_create_game())
    ex.export_packet(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                             ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    hero = ex.game.find_entity_by_id(4)
    assert hero.card_id == "HERO_01a"
    assert hero.tags.get(GameTag.HEALTH) == 30
    assert hero.zone == Zone.PLAY


class _Tree:
    """exporter 只在 export() 时读 packet_tree, 手动驱动传占位即可。"""
    def __iter__(self):
        return iter(())
