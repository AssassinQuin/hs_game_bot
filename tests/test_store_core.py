"""GameStore 核心: apply 管线 / TagChange 衍生 / 查询 API。"""
from hearthstone.enums import CardType, GameTag, Zone

from hsbot.store import is_hero

from .conftest import (mk_create_game, mk_full, mk_heroes as _heroes,
                       mk_show, mk_store as _store, mk_tag)


def test_turn_start_events():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))    # 首个行动方=我
    st.apply(mk_tag(1, GameTag.TURN, 1))
    st.apply(mk_tag(2, GameTag.TURN, 1))
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 0))
    st.apply(mk_tag(3, GameTag.CURRENT_PLAYER, 1))    # 切到对手
    ks = log.kinds()
    assert ks[0] == "turn_start" and log.events[0]["first"] is True
    assert "text" in ks and log.by_kind("text")[0]["msg"] == "结束回合"
    ts = [e for e in log.events if e["kind"] == "turn_start"][-1]
    assert ts["actor"] == 2 and ts["prev"] == 1 and ts["first"] is False


def test_mana_cap_event_only_friendly():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.RESOURCES, 1))
    st.apply(mk_tag(2, GameTag.RESOURCES, 2))         # 我方 1→2
    st.apply(mk_tag(3, GameTag.RESOURCES, 1))
    st.apply(mk_tag(3, GameTag.RESOURCES, 2))         # 对手: 不发
    msgs = [e["msg"] for e in log.by_kind("text") if "水晶上限" in e.get("msg", "")]
    assert msgs == ["水晶上限 1→2"]


def test_spellpower_events():
    """场上法强: 引擎在玩家实体上维护 CURRENT_SPELLPOWER_BASE, 变更即发事件。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "BT_724", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=1, SPELLPOWER=1))
    st.apply(mk_tag(2, GameTag.CURRENT_SPELLPOWER_BASE, 1))    # 首次点亮
    st.apply(mk_tag(2, GameTag.CURRENT_SPELLPOWER_BASE, 3))    # 增长
    st.apply(mk_tag(2, GameTag.CURRENT_SPELLPOWER_BASE, 3))    # 重算同值: 不发
    st.apply(mk_tag(3, GameTag.CURRENT_SPELLPOWER_BASE, 2))    # 对手
    evs = log.by_kind("spellpower")
    assert [(e["actor"], e["prev"], e["total"]) for e in evs] == [
        (1, None, 1), (1, 1, 3), (2, None, 2)]
    assert st.spellpower(1) == 3 and st.spellpower(2) == 2


def test_hero_damage_event():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(5, GameTag.DAMAGE, 3))            # 敌方英雄掉血
    msgs = [e["msg"] for e in log.by_kind("text")]
    assert any("敌方英雄 30血→27血" in m for m in msgs)


def test_zone_draw_and_back_to_deck_and_death():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_show(10, "CS2_029", ZONE=Zone.HAND.value))       # 揭示入手机牌
    st.apply(mk_tag(10, GameTag.ZONE, Zone.HAND.value))          # DECK→HAND
    draws = log.by_kind("draw")
    assert draws and draws[-1]["card_id"] == "CS2_029"
    # 置回牌库(SETASIDE→DECK)
    st.apply(mk_tag(10, GameTag.ZONE, Zone.SETASIDE.value))
    st.apply(mk_tag(10, GameTag.ZONE, Zone.DECK.value))
    assert log.by_kind("back_to_deck")[-1]["card_id"] == "CS2_029"
    # 随从死亡(PLAY→GRAVEYARD)
    st.apply(mk_full(11, "CS2_120", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2))
    st.apply(mk_tag(11, GameTag.ZONE, Zone.GRAVEYARD.value))
    assert log.by_kind("death")[-1]["card_id"] == "CS2_120"


def test_cost_event():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(12, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1))
    st.apply(mk_tag(12, GameTag.COST, 1))             # 面板2→实付1
    evt = log.by_kind("cost")[-1]
    assert (evt["old"], evt["new"]) == (2, 1)


def test_game_end_emitted_once():
    st, log = _store()
    _heroes(st)
    from hearthstone.enums import PlayState
    st.apply(mk_tag(2, GameTag.PLAYSTATE, PlayState.WON.value))
    st.apply(mk_tag(3, GameTag.PLAYSTATE, PlayState.LOST.value))
    assert len(log.by_kind("game_end")) == 1


def test_queries():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1,
                     ZONE_POSITION=2))
    st.apply(mk_full(11, "CS2_023t", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1))
    st.apply(mk_full(12, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_tag(2, GameTag.RESOURCES, 3))
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, 1))
    assert [e.card_id for e in st.hand(1)] == ["CS2_023t", "CS2_029"]   # 按位置序
    assert st.deck_count(1) == 1
    assert st.mana_now(1) == 2 and st.mana_next_turn(1) == 4
    assert st.hero_hp(1) == 30 and st.hero(2).card_id == "HERO_02a"
    assert st.playstate(1) == "INVALID"


def test_apply_hints_and_progress():
    """行级线索/进度推进: store 的显式入口(不再允许后门直写字段)。"""
    st, log = _store(battletag="")
    st.apply_hints({77: "TTN_001"}, {77: 1})
    assert st.cid_of(77) == "TTN_001"
    st.note_progress(42)
    assert st.lines_consumed == 42
