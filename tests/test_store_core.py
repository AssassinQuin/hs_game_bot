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


def test_prepare_tag_constants_match_official_enum():
    """PREPARE 族标签兜底常量: 官方 9.20.12 数值, 旧版包无枚举名也能走通。

    store 的 TagChange 分支引用 PREPARING/PREPARE, 而 requirements 下限
    hearthstone>=9.20(如 9.20.2)无此枚举名——常量按数值兜底(先例
    TAG_START_OF_GAME_KEYWORD); 装了新版时必须与枚举等值。
    """
    from hsbot.consts import TAG_PREPARING, TAG_PREPARE
    assert (TAG_PREPARING, TAG_PREPARE) == (4726, 4354)
    if hasattr(GameTag, "PREPARING"):          # 新版包: 常量与枚举等值
        assert GameTag.PREPARING == TAG_PREPARING
        assert GameTag.PREPARE == TAG_PREPARE


def test_board_face_attack_legality():
    """斩杀口径场攻(审计 2026-09-14 高#2, 宁漏勿错): 敌方嘲讽在场 → 0;
    我方剔除 冻结/本回合已攻击/本回合进战场且无冲锋(标签与区转双信号)。"""
    st, _ = _store()
    _heroes(st)
    M = dict(CARDTYPE=CardType.MINION.value, ZONE=Zone.PLAY.value)
    st.apply(mk_full(60, "M_OK", CONTROLLER=1, ZONE_POSITION=1, ATK=5, **M))
    st.apply(mk_full(61, "M_FROZEN", CONTROLLER=1, ZONE_POSITION=2, ATK=4,
                     FROZEN=1, **M))
    st.apply(mk_full(62, "M_TIRED", CONTROLLER=1, ZONE_POSITION=3, ATK=3,
                     NUM_ATTACKS_THIS_TURN=1, **M))
    st.apply(mk_full(63, "M_SICK", CONTROLLER=1, ZONE_POSITION=4, ATK=3, **M))
    st.apply(mk_tag(63, GameTag.ZONE, Zone.PLAY.value))   # 本回合入场(区转信号)
    st.apply(mk_full(64, "M_CHARGE", CONTROLLER=1, ZONE_POSITION=5, ATK=2,
                     CHARGE=1, **M))
    st.apply(mk_tag(64, GameTag.ZONE, Zone.PLAY.value))
    assert st.board_face_attack(1) == 5 + 2              # 健康5 + 冲锋2
    st.apply(mk_full(65, "T_TAUNT", CONTROLLER=2, ZONE_POSITION=1, ATK=2,
                     TAUNT=1, **M))
    assert st.board_face_attack(1) == 0                   # 敌方嘲讽挡脸
