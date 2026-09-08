"""GameStore 块与选择: PLAY 延迟/attack/trigger/fatigue/留牌/发现/to_dict。"""
from hearthstone.enums import BlockType, CardType, ChoiceType, GameTag, Zone
from hslog import packets

from hsbot.carddb import CardDB
from hsbot.store import GameStore

from .conftest import (TS, EventLog, _ref, mk_block, mk_choices_general,
                       mk_choices_mulligan, mk_create_game, mk_full, mk_hide,
                       mk_pm, mk_send_general, mk_send_mulligan, mk_show, mk_tag)


class _Tree:
    def __iter__(self):
        return iter(())


def _store():
    carddb = CardDB("/nonexistent/cards.json")
    st = GameStore(carddb=carddb, battletag="湫然#51704",
                   tree=_Tree(), player_manager=mk_pm())
    log = EventLog()
    st.subscribe(log)
    st.apply(mk_create_game())
    st.note_friendly(1)
    return st, log


def _heroes(st):
    st.apply(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    st.apply(mk_full(5, "HERO_02a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, HEALTH=30))


def test_play_deferred_until_block_settles():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(10, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    b = mk_block(BlockType.PLAY, 10)
    st.apply(b, depth=0)                    # 块开始: 身份未知, 挂起
    assert log.by_kind("play") == []
    st.apply(mk_show(10, "CS2_029", ZONE=Zone.HAND.value), depth=1)
    st.apply(mk_tag(10, GameTag.CARDTYPE, CardType.SPELL.value), depth=1)
    b.end()
    st.settle()
    plays = log.by_kind("play")
    assert plays and plays[0]["card_id"] == "CS2_029"
    assert plays[0]["is_power"] is False


def test_play_flushed_by_shallower_packet():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1))
    b = mk_block(BlockType.PLAY, 10)
    st.apply(b, depth=0)
    st.apply(mk_show(10, "CS2_029", ZONE=Zone.HAND.value), depth=1)
    st.apply(mk_tag(11, GameTag.ZONE, Zone.HAND.value), depth=0)   # 不更深 => 子树结束
    assert log.by_kind("play")


def test_attack_event():
    st, log = _store()
    _heroes(st)
    st.apply(mk_block(BlockType.ATTACK, 4, target=5))
    evt = log.by_kind("attack")[-1]
    assert evt["attacker_card_id"] == "HERO_01a" and evt["attacker_is_hero"]
    assert evt["target_is_hero"] and evt["actor"] == 1


def test_trigger_and_fatigue_events():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(30, "TTN_910", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2))
    st.apply(mk_block(BlockType.TRIGGER, 30))
    st.apply(mk_block(BlockType.FATIGUE, 3))
    assert log.by_kind("trigger")[-1]["card_id"] == "TTN_910"
    assert log.by_kind("fatigue")[-1]["actor"] == 2


def test_mulligan_flow():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "OG_048", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "GAME_005", ZONE=Zone.DECK.value, CONTROLLER=2))
    st.apply(mk_choices_mulligan(2, 7, [10]))              # 我方(entity2=pid1)起手
    st.apply(mk_send_mulligan(7, [10]))                    # 留下
    m = log.by_kind("mulligan")
    assert m and "留牌: OG_048" in m[0]["msg"]


def test_discover_flow():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(20, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_full(21, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_choices_general(2, 9, [20, 21]))
    st.apply(mk_send_general(9, [21]))                     # 选 21, 20 置底
    evt = log.by_kind("discover")[-1]
    # 未揭示实体 cid_of 为 None -> 事件里回退保留实体号
    assert evt["picked"] == [21] and evt["bottom"] == [20]
    assert log.kinds().count("discover") == 1


def test_raw_records_unhandled_packets():
    """全量收录原则: 未解释的包记 raw(不渲染, 入库可查)。"""
    from hslog import packets as P

    from .conftest import TS

    st, log = _store()
    _heroes(st)
    st.apply(P.MetaData(TS, "DAMAGE", 0, 1))               # 未解释 -> raw
    st.apply(mk_block(BlockType.POWER, 4))                 # 未解释块 -> raw
    st.apply(mk_block(BlockType.PLAY, 4))                  # 已解释路径 -> 非 raw
    types = [u["packet_type"] for u in st.unhandled]
    assert "MetaData" in types and "Block:POWER" in types
    assert "Block:PLAY" not in types
    assert log.kinds().count("raw") >= 2


def test_gain_event_with_creator():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(15, "CS2_013", ZONE=Zone.PLAY.value, CONTROLLER=1))
    st.apply(mk_full(20, "TTN_001", ZONE=Zone.HAND.value, CONTROLLER=1, CREATOR=15))
    evt = log.by_kind("gain")[-1]
    assert evt["card_id"] == "TTN_001" and evt["creator"] == "CS2_013"


def test_to_dict_shape():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1))
    d = st.to_dict("turn_end")
    assert d["reason"] == "turn_end"
    assert set(d) == {"reason", "turn", "friendly_turn", "my_turn", "players", "me", "opp"}
    assert set(d["me"]) == {"hp", "armor", "mana", "deck", "hand", "board"}
    assert set(d["opp"]) == {"hp", "armor", "deck", "hand_n", "board"}
    assert d["me"]["hand"][0]["id"] == "CS2_029" and d["me"]["hand"][0]["pos"] == 0


def test_mana_text_projection():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.RESOURCES, 2))
    st.apply(mk_tag(2, GameTag.OVERLOAD_OWED, 1))
    assert st.mana_text(1) == "水晶 2/2 (过载-1)"


def test_hide_entity_writes_zone_back():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1))
    st.apply(mk_full(11, None, ZONE=Zone.HAND.value, CONTROLLER=1))
    st.apply(mk_hide(10, Zone.DECK.value))
    # 库缺口: entity.hide() 只撤 revealed 不落 ZONE, _on_hide_entity 补写
    assert st.get(10).tags[GameTag.ZONE] == Zone.DECK.value
    st.apply(mk_hide(11, "not-a-zone"))       # 脏 zone 值: 不炸也不覆盖
    assert st.get(11).tags[GameTag.ZONE] == Zone.HAND.value


def test_hint_draw_dedup():
    st, log = _store()
    st.hint_draw(10, "CS2_029", 1)
    st.hint_draw(10, "CS2_029", 1)            # 0.5s 内同实体: 只报一次
    assert log.kinds().count("draw") == 1
    st.hint_draw(11, "CS2_029", 1)            # 不同实体: 正常发
    assert log.kinds().count("draw") == 2


def test_chosen_entities_mulligan_route():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "OG_048", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_choices_mulligan(2, 7, [10]))             # 我方(entity2=pid1)起手
    chosen = packets.ChosenEntities(TS, _ref(2), 7)       # hslog 不产 type, 显式标注路由
    chosen.type = ChoiceType.MULLIGAN
    chosen.choices = [10]
    st.apply(chosen)
    assert log.kinds().count("mulligan") == 1
    st.apply(chosen)                          # _mulligan_emitted 已发标: 不重复
    assert log.kinds().count("mulligan") == 1
