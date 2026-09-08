"""StoreExporter: 库维护状态 + 挂钩后置触发 + 子包不连带应用。"""
from hearthstone.enums import BlockType, GameTag, Zone
from hslog import packets

from hsbot.adapter import StoreExporter

from .conftest import TS, mk_block, mk_create_game, mk_full, mk_pm, mk_tag


class _Tree:
    def __iter__(self):
        return iter(())


def _exporter(hooks) -> StoreExporter:
    ex = StoreExporter(_Tree(), mk_pm(), hooks)
    ex.export_packet(mk_create_game())
    return ex


def test_hook_fires_after_state_applied():
    seen = []

    class Hooks:
        def on_tag_change(self, p):
            seen.append(p.entity and ex.game.find_entity_by_id(64).zone)

    ex = _exporter(Hooks())
    ex.export_packet(mk_full(64, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    ex.export_packet(mk_tag(64, GameTag.ZONE, Zone.HAND.value))
    assert seen == [Zone.HAND]          # 挂钩里读到的已是新状态


def test_full_entity_and_show_entity_hooks():
    seen = []

    class Hooks:
        def on_full_entity(self, p):
            seen.append(("full", p.card_id))

        def on_show_entity(self, p):
            seen.append(("show", p.card_id))

    ex = _exporter(Hooks())
    ex.export_packet(mk_full(64, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    from .conftest import mk_show
    ex.export_packet(mk_show(64, "CS2_029", ZONE=Zone.HAND.value))
    assert seen == [("full", None), ("show", "CS2_029")]


def test_block_does_not_apply_children():
    class Hooks:
        pass

    ex = _exporter(Hooks())
    b = mk_block(BlockType.PLAY, 64)
    b.packets = [mk_tag(64, GameTag.ZONE, Zone.PLAY.value)]
    ex.export_packet(b)                       # 只登记块, 不应用子包
    assert ex.game.find_entity_by_id(64) is None
    ex.export_packet(mk_full(64, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1))
    ex.export_packet(b.packets[0])            # 子包由游标单独驱动
    assert ex.game.find_entity_by_id(64).zone == Zone.PLAY


def test_dirty_packets_tolerated_and_still_notify():
    hits = []

    class Hooks:
        def on_tag_change(self, p):
            hits.append(1)

    ex = StoreExporter(_Tree(), mk_pm(), Hooks())   # 尚无 game
    ex.export_packet(mk_tag(999, GameTag.ZONE, 1))  # 找不到实体: 不炸, 仍通知
    assert hits == [1]
