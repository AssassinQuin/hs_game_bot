"""渲染与知识层换源: snapshot_block/game_end_line/rebuild 吃 GameStore。"""
from collections import Counter

from hearthstone.enums import CardType, GameTag, Zone

from hsbot.carddb import CardDB
from hsbot.knowledge import DeckKnowledge
from hsbot.render import chain_line, game_end_line, snapshot_block, snapshot_line
from hsbot.store import GameStore

from .conftest import (EmptyTree, mk_full, mk_heroes as _heroes,
                       mk_pm, mk_store as _store, mk_tag)


def _board(st):
    st.apply(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    st.apply(mk_full(5, "HERO_02a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, HEALTH=30))
    st.apply(mk_full(10, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, ZONE_POSITION=1))
    st.apply(mk_full(30, "CS2_120", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, ATK=2, HEALTH=3,
                     ZONE_POSITION=1))
    st.apply(mk_tag(2, GameTag.RESOURCES, 3))
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, 1))


def test_snapshot_block_renders_from_store():
    st, _ = _store()
    _board(st)
    block = snapshot_block(st, None, knowledge=None, deck_name="奇迹德",
                           generic=True, game_no=1, chain_lines=[],
                           chain_summary=[], carddb=st.carddb, reason="turn_end")
    assert "完整快照" in block and "回合T0" in block
    assert "我  " in block and "CS2_029(2费)" in block
    assert "对面" in block and "CS2_120(2/3)" in block


def test_game_end_line_and_chain_line_new_kinds():
    st, _ = _store()
    _board(st)
    assert "对局结束" in game_end_line(st)
    assert "触发 CS2_029" in chain_line(
        {"kind": "trigger", "card_id": "CS2_029", "turn": 3, "friendly": 1}, st.carddb)
    assert "疲劳" in chain_line(
        {"kind": "fatigue", "actor": 2, "turn": 9, "friendly": 1}, st.carddb)
    assert "疲劳 第2抽(-2血)" in chain_line(
        {"kind": "fatigue", "actor": 2, "count": 2, "turn": 9, "friendly": 1}, st.carddb)
    assert "场上法强 2→3" in chain_line(
        {"kind": "spellpower", "actor": 2, "prev": 2, "total": 3,
         "turn": 9, "friendly": 1}, st.carddb)
    assert "场上法强 3" in chain_line(
        {"kind": "spellpower", "actor": 2, "total": 3,
         "turn": 9, "friendly": 1}, st.carddb)
    assert "阵亡" in chain_line(
        {"kind": "death", "card_id": "CS2_120", "turn": 5, "friendly": 1}, st.carddb)


def test_knowledge_rebuild_from_store():
    st, _ = _store()
    _board(st)
    k = DeckKnowledge({"CS2_029": 2}, CardDB("/nonexistent/cards.json"), "测试")
    led = k.rebuild(st)
    assert led.in_hand == Counter({"CS2_029": 1})
    assert led.remaining == Counter({"CS2_029": 1})
    assert led.deck_actual == 0


def test_knowledge_rebuild_survives_player_entity():
    """2026-09-13 实测回归: 我方 Player 实体带 CONTROLLER 标签(真实日志 CREATE_GAME
    就写)后, rebuild 对它取 card_id 直接 AttributeError —— 每次回合末快照全灭。"""
    st, _ = _store()
    _board(st)
    st.apply(mk_tag(2, GameTag.CONTROLLER, 1))    # 实体2 = 我方 Player 实体
    k = DeckKnowledge({"CS2_029": 2}, CardDB("/nonexistent/cards.json"), "测试")
    led = k.rebuild(st)
    assert led.in_hand == Counter({"CS2_029": 1})   # 台账不受玩家实体影响


def test_discover_bottom_facts_enter_ledger(tmp_path):
    """2026-09-13 实测: 发现(水栖形态/波涛形塑)置底的牌是隐藏实体, 台账曾
    看不见 → "底部已知: 无"。发现事实并入牌底: 未选项按浅→深 = 牌库最底。
    门槛=来源卡机制文本; 非置底类发现的未选项会消失, 不入牌底。"""
    import json

    from hslog import packets as P

    from .conftest import TS, mk_choices_general, mk_send_general

    p = tmp_path / "cards.json"
    p.write_text(json.dumps([
        # 无"探底"措辞: 门槛必须走数据级 mechanics 标签(解析器 IR), 不是文本匹配
        {"id": "TSC_654", "name": "水栖形态", "type": "SPELL",
         "text": "如果你在本回合中有足够的法力值使用选中的牌，则抽取这张牌。",
         "mechanics": ["DREDGE"]},
        {"id": "TIME_999", "name": "普通发现", "type": "SPELL", "text": "发现一张牌。",
         "mechanics": ["DISCOVER"]},
    ], ensure_ascii=False), encoding="utf-8")
    db = CardDB(p)

    st, _ = _store()
    _heroes(st)
    for i in range(20, 28):                     # 牌库 8 张隐藏实体
        st.apply(mk_full(i, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_full(45, "TSC_654", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value))
    st.apply(mk_full(40, "TID_001", ZONE=Zone.SETASIDE.value, CONTROLLER=1))
    st.apply(mk_full(41, "SC_755", ZONE=Zone.SETASIDE.value, CONTROLLER=1))
    st.apply(mk_full(42, "VAC_428", ZONE=Zone.SETASIDE.value, CONTROLLER=1))
    ch = mk_choices_general(2, 9, [40, 41, 42])
    ch.source = 45                              # hslog 真实包带 Source=
    st.apply(ch)
    st.apply(mk_send_general(9, [42]))          # 取 42, 其余置底
    assert st.last_discover == {"source": "TSC_654",
                                "picked": ["VAC_428"],
                                "unpicked": ["TID_001", "SC_755"]}
    k = DeckKnowledge({"TID_001": 2, "SC_755": 2, "VAC_428": 2}, db, "测试")
    led = k.rebuild(st)
    assert led.known_bottom == [("SC_755", 8), ("TID_001", 7)]   # 浅→深
    assert led.unknown_middle == 6
    # 洗牌作废牌底事实
    st.apply(P.ShuffleDeck(TS, 1))
    assert st.last_discover is None
    assert k.rebuild(st).known_bottom == []
    # 非置底机制: 未选项消失, 不入牌底
    st2, _ = _store()
    _heroes(st2)
    for i in range(30, 34):
        st2.apply(mk_full(i, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st2.apply(mk_full(46, "TIME_999", ZONE=Zone.PLAY.value, CONTROLLER=1,
                      CARDTYPE=CardType.SPELL.value))
    st2.apply(mk_full(50, "AAA", ZONE=Zone.SETASIDE.value, CONTROLLER=1))
    st2.apply(mk_full(51, "BBB", ZONE=Zone.SETASIDE.value, CONTROLLER=1))
    ch2 = mk_choices_general(2, 9, [50, 51])
    ch2.source = 46
    st2.apply(ch2)
    st2.apply(mk_send_general(9, [50]))
    k2 = DeckKnowledge({"AAA": 1}, db, "测试")
    assert k2.rebuild(st2).known_bottom == []


def test_trigger_line_eid_fallback_for_unrevealed():
    """未揭示实体(对面暗牌附魔)触发: card_id 未知时报实体号而非裸 '?'。"""
    st, _ = _store()
    assert "触发 #113" in chain_line(
        {"kind": "trigger", "eid": 113, "card_id": None,
         "turn": 3, "friendly": 1}, st.carddb)
    assert "触发 CS2_029" in chain_line(
        {"kind": "trigger", "eid": 10, "card_id": "CS2_029",
         "turn": 3, "friendly": 1}, st.carddb)
    assert "触发 #84(开局)" in chain_line(
        {"kind": "trigger", "eid": 84, "card_id": None,
         "keyword": "START_OF_GAME_KEYWORD", "turn": 1, "friendly": 1}, st.carddb)


def test_play_render_formats_prediction():
    """渲染层只格式化解析层产物 pred_dmg, 不做任何游戏计算。"""
    db = CardDB("/nonexistent/cards.json")
    line = chain_line({"kind": "play", "card_id": "CS2_008", "actor": 1, "friendly": 1,
                       "cost_base": 0, "mana_left": 4,
                       "pred_dmg": {"total": 5, "hits": 1}, "turn": 9}, db)
    assert "打出 CS2_008 (0费) 剩4费 → 预计5伤" in line
    line2 = chain_line({"kind": "play", "card_id": "TID_001", "actor": 1, "friendly": 1,
                        "cost_base": 1, "mana_left": 3,
                        "pred_dmg": {"total": 5, "hits": 2}, "turn": 9}, db)
    assert "预计5伤(2段)" in line2
    line3 = chain_line({"kind": "play", "card_id": "CS2_008", "actor": 1, "friendly": 1,
                        "cost_base": 0, "mana_left": 4, "turn": 9}, db)
    assert "预计" not in line3


def test_snapshot_line_compact_single_line():
    st, _ = _store()
    _board(st)
    line = snapshot_line(st, 1, "turn_end")
    assert line.startswith("── T")          # 回合头
    assert "水晶" in line                    # 我方水晶
    assert "手牌1" in line                   # _board 构造了 1 张手牌 CS2_029
    assert "牌库" in line
    assert "场上0 │ 对面场攻2" in line        # 我方场上 0; 对面 CS2_120 ×1 总攻 2
    assert "对面场上" not in line            # 是场攻, 不是随从数量(2026-09-13 用户要求)
    assert "\n" not in line                 # 必须单行


def test_snapshot_line_friendly_unknown_fallback():
    st = GameStore(carddb=CardDB("/nonexistent/cards.json"), battletag="湫然#51704",
                   tree=EmptyTree(), player_manager=mk_pm())   # 不 apply/note_friendly
    line = snapshot_line(st, 7, "unknown_reason")
    assert line == "── 第7局快照(unknown_reason) ──"
