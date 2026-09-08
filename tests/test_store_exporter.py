"""StoreExporter: 库维护状态 + 子包不连带应用(衍生在 GameStore.apply, 本类不回调)。"""
from hearthstone.enums import BlockType, GameTag, Zone

from hsbot.adapter import StoreExporter

from .conftest import mk_block, mk_create_game, mk_full, mk_pm, mk_show, mk_tag


class _Tree:
    def __iter__(self):
        return iter(())


def _exporter() -> StoreExporter:
    ex = StoreExporter(_Tree(), mk_pm())
    ex.export_packet(mk_create_game())
    return ex


def test_state_applied_by_export_packet():
    ex = _exporter()
    ex.export_packet(mk_full(64, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    ex.export_packet(mk_tag(64, GameTag.ZONE, Zone.HAND.value))
    assert ex.game.find_entity_by_id(64).zone == Zone.HAND


def test_reveal_by_show_entity():
    ex = _exporter()
    ex.export_packet(mk_full(64, None, ZONE=Zone.HAND.value, CONTROLLER=1))
    assert ex.game.find_entity_by_id(64).card_id is None
    ex.export_packet(mk_show(64, "CS2_029", ZONE=Zone.HAND.value))
    assert ex.game.find_entity_by_id(64).card_id == "CS2_029"


def test_block_does_not_apply_children():
    ex = _exporter()
    ex.export_packet(mk_full(64, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1))
    b = mk_block(BlockType.PLAY, 64)
    b.packets = [mk_tag(64, GameTag.ZONE, Zone.PLAY.value)]
    ex.export_packet(b)                       # 只登记块, 不应用子包
    # 若回归成基类的"迭代子包"行为, 这里会变成 PLAY
    assert ex.game.find_entity_by_id(64).zone == Zone.HAND
    ex.export_packet(b.packets[0])            # 子包由游标单独驱动
    assert ex.game.find_entity_by_id(64).zone == Zone.PLAY


def test_dirty_packets_tolerated_no_game():
    ex = StoreExporter(_Tree(), mk_pm())      # 尚无 game
    ex.export_packet(mk_tag(999, GameTag.ZONE, 1))  # 找不到实体: 不炸
