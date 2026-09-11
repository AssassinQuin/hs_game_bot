"""渲染与知识层换源: snapshot_block/game_end_line/rebuild 吃 GameStore。"""
from collections import Counter

from hearthstone.enums import CardType, GameTag, Zone

from hsbot.carddb import CardDB
from hsbot.knowledge import DeckKnowledge
from hsbot.render import chain_line, game_end_line, snapshot_block, snapshot_line
from hsbot.store import GameStore

from .conftest import EventLog, mk_create_game, mk_full, mk_pm, mk_tag


class _Tree:
    def __iter__(self):
        return iter(())


def _store():
    carddb = CardDB("/nonexistent/cards.json")
    st = GameStore(carddb=carddb, battletag="湫然#51704",
                   tree=_Tree(), player_manager=mk_pm())
    st.subscribe(EventLog())
    st.apply(mk_create_game())
    st.note_friendly(1)
    return st, carddb


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
    st, carddb = _store()
    _board(st)
    block = snapshot_block(st, None, knowledge=None, deck_name="奇迹德",
                           generic=True, game_no=1, chain_lines=[],
                           chain_summary=[], carddb=carddb, reason="turn_end")
    assert "完整快照" in block and "回合T0" in block
    assert "我  " in block and "CS2_029(2费)" in block
    assert "对面" in block and "CS2_120(2/3)" in block


def test_game_end_line_and_chain_line_new_kinds():
    st, carddb = _store()
    _board(st)
    assert "对局结束" in game_end_line(st)
    assert "触发 CS2_029" in chain_line(
        {"kind": "trigger", "card_id": "CS2_029", "turn": 3, "friendly": 1}, carddb)
    assert "疲劳" in chain_line(
        {"kind": "fatigue", "actor": 2, "turn": 9, "friendly": 1}, carddb)
    assert "阵亡" in chain_line(
        {"kind": "death", "card_id": "CS2_120", "turn": 5, "friendly": 1}, carddb)


def test_knowledge_rebuild_from_store():
    st, _ = _store()
    _board(st)
    k = DeckKnowledge({"CS2_029": 2}, CardDB("/nonexistent/cards.json"), "测试")
    led = k.rebuild(st)
    assert led.in_hand == Counter({"CS2_029": 1})
    assert led.remaining == Counter({"CS2_029": 1})
    assert led.deck_actual == 0


def test_snapshot_line_compact_single_line():
    st, _ = _store()
    _board(st)
    line = snapshot_line(st, 1, "turn_end")
    assert line.startswith("── T")          # 回合头
    assert "水晶" in line                    # 我方水晶
    assert "手牌1" in line                   # _board 构造了 1 张手牌 CS2_029
    assert "牌库" in line
    assert "场上0 │ 对面场上1" in line        # 我方场上 0, 对面场面 CS2_120 ×1
    assert "\n" not in line                 # 必须单行


def test_snapshot_line_friendly_unknown_fallback():
    st = GameStore(carddb=CardDB("/nonexistent/cards.json"), battletag="湫然#51704",
                   tree=_Tree(), player_manager=mk_pm())   # 不 apply/note_friendly
    line = snapshot_line(st, 7, "unknown_reason")
    assert line == "── 第7局快照(unknown_reason) ──"
