"""解析层: $N 文本伤害、伤害预估、隐藏触发指纹判定、批量增量编译。"""
import json

import pytest

from hsbot.analysis import EffectAnalyzer, collect_card_ids
from hsbot.carddb import CardDB
from hsbot.effects import Damage, EffectCache, Heal


def _db(tmp_path, cards):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


def test_text_damage_parsing(tmp_path):
    db = _db(tmp_path, [
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
        {"id": "TID_001", "name": "月光射线", "type": "SPELL",
         "text": "对一个敌人造成$1点伤害两次。"},
        {"id": "HEAL", "name": "回春术", "type": "SPELL", "text": "恢复$4点生命。"},
    ])
    a = EffectAnalyzer(db)
    assert a.text_damage("CS2_008") == (1, 1)
    assert a.text_damage("TID_001") == (1, 2)
    assert a.text_damage("HEAL") is None
    assert a.text_damage(None) is None


def test_predict_damage_gated_by_type(tmp_path):
    db = _db(tmp_path, [
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
        {"id": "MINION_DMG", "name": "战吼伤随从", "type": "MINION",
         "text": "战吼:造成$2点伤害。"},
    ])
    a = EffectAnalyzer(db)
    assert a.predict_damage("CS2_008", 4) == {"total": 5, "hits": 1}
    assert a.predict_damage("CS2_008", 0) == {"total": 1, "hits": 1}
    assert a.predict_damage("MINION_DMG", 4) is None      # 随从战吼不吃法强


def test_infer_renathal_fingerprint():
    a = EffectAnalyzer(CardDB("/nonexistent.json"))
    assert a.infer_trigger_card("START_OF_GAME_KEYWORD", 1, 36) == "REV_018"
    assert a.infer_trigger_card("START_OF_GAME_KEYWORD", 1, 26) is None   # 30 卡组
    assert a.infer_trigger_card("TAG_NOT_SET", 1, 36) is None
    assert a.infer_trigger_card("START_OF_GAME_KEYWORD", 2, 36) is None


def test_mechanic_marks_from_data_and_text(tmp_path):
    """机制标记(通用处理器): 数据级(HsJson mechanics[] 引擎标签)与文本级
    (语法表措辞)两通道统一编译为 IR Mechanic 标记, 与主效果正交叠加;
    消费方只查标记 kind, 不接触文本措辞/卡牌号。"""
    from hsbot.effects import (Damage, Mechanic, _effects_from_json,
                               _effects_to_json, compile_card)

    ir = compile_card({"id": "TSC_654", "type": "SPELL", "text": "抽取这张牌。",
                       "mechanics": ["DREDGE"]})
    assert Mechanic("deck_bottom") in ir.effects            # 数据通道(引擎标签)
    ir2 = compile_card({"id": "TIME_701", "type": "SPELL",
                        "text": "从你的牌库中发现一张牌。将其余选项置于牌库底。",
                        "mechanics": ["DISCOVER"]})
    assert Mechanic("deck_bottom") in ir2.effects           # 文本通道(引擎无置底标签)
    ir3 = compile_card({"id": "X", "type": "SPELL",
                        "text": "造成$2点伤害。将其余选项置于牌库底。"})
    assert Damage(2) in ir3.effects and Mechanic("deck_bottom") in ir3.effects
    ir4 = compile_card({"id": "Y", "type": "SPELL", "text": "",
                        "mechanics": ["DISCOVER"]})
    assert all(not isinstance(e, Mechanic) for e in ir4.effects)  # 未映射机制不打标
    rt = _effects_from_json(_effects_to_json(ir.effects))          # 缓存序列化往返
    assert Mechanic("deck_bottom") in rt


def test_has_mechanic_query(tmp_path):
    db = _db(tmp_path, [
        {"id": "TSC_654", "name": "水栖形态", "type": "SPELL", "text": "抽取这张牌。",
         "mechanics": ["DREDGE"]},
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
    ])
    a = EffectAnalyzer(db)
    assert a.discover_bottom_mechanic("TSC_654") is True
    assert a.discover_bottom_mechanic("CS2_008") is False
    assert a.discover_bottom_mechanic(None) is False
    assert a.has_mechanic("CS2_008", "deck_bottom") is False


def test_chosen_suboption_card_id(tmp_path):
    """抉择子卡解析(解析层富化): 按钮实体(PARENT_CARD)按下标取; 未揭示时按
    a/b 命名惯例兜底(卡表可证才采用); 无 suboption 不富化。"""
    from hearthstone.enums import CardType, Zone

    from .conftest import mk_full, mk_heroes, mk_store

    st, _ = mk_store()
    mk_heroes(st)
    st.apply(mk_full(53, "AV_295", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value))
    st.apply(mk_full(54, "AV_295a", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     PARENT_CARD=53))
    st.apply(mk_full(55, "AV_295b", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     PARENT_CARD=53))
    a = EffectAnalyzer(st.carddb)
    evt = {"kind": "play", "card_id": "AV_295", "eid": 53, "suboption": 1}
    assert a.enrich(dict(evt), st)["suboption_card_id"] == "AV_295b"
    # 按钮未揭示: a/b 惯例兜底 —— 真卡表可证才采用
    db = _db(tmp_path, [
        {"id": "AV_295", "name": "占领冷齿矿洞", "type": "SPELL", "cost": 2},
        {"id": "AV_295b", "name": "更多补给", "type": "SPELL", "cost": 2},
    ])
    st2, _ = mk_store()                    # 无按钮实体
    a2 = EffectAnalyzer(db)
    assert a2.enrich(dict(evt), st2)["suboption_card_id"] == "AV_295b"
    a3 = EffectAnalyzer(CardDB("/nonexistent.json"))   # 卡表缺失: 不编造 id
    assert "suboption_card_id" not in a3.enrich(dict(evt), st2)
    # 非抉择打出(suboption 缺失/负值): 不富化
    assert "suboption_card_id" not in a2.enrich(
        {"kind": "play", "card_id": "AV_295", "eid": 53}, st2)
    assert "suboption_card_id" not in a2.enrich(
        {"kind": "play", "card_id": "AV_295", "eid": 53, "suboption": -1}, st2)


def test_cache_incremental_and_persistence(tmp_path, monkeypatch):
    """增量: 未变化复用(零重编译); 变化重编译; 落盘后新实例零重编译。"""
    import hsbot.effects as eff
    db = _db(tmp_path, [
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
    ])
    cache_path = tmp_path / "effects.json"

    calls = {"n": 0}
    orig = eff.compile_card

    def counting(card):
        calls["n"] += 1
        return orig(card)

    monkeypatch.setattr(eff, "compile_card", counting)
    a1 = EffectAnalyzer(db, EffectCache(cache_path))
    assert a1.predict_damage("CS2_008", 2) == {"total": 3, "hits": 1}
    assert calls["n"] == 1
    a1.predict_damage("CS2_008", 4)               # 命中缓存: 不再编译
    assert calls["n"] == 1
    a1.cache.save()

    a2 = EffectAnalyzer(db, EffectCache(cache_path))   # 新实例: 磁盘增量
    assert a2.predict_damage("CS2_008", 2) == {"total": 3, "hits": 1}
    assert calls["n"] == 1                        # 磁盘命中, 未重编译


def test_compile_seen_cards_batch(tmp_path):
    """批量增量编译: 日志采集→编译→统计; 二次运行全量复用。"""
    from hsbot.analysis import collect_card_ids
    db = _db(tmp_path, [
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
        {"id": "HEAL", "name": "回春", "type": "SPELL", "text": "恢复$4点生命。"},
    ])
    log = tmp_path / "Power.log"
    log.write_text("cardId=CS2_008" + chr(10) + "cardId=PET_1" + chr(10),
                   encoding="utf-8")
    cache = EffectCache(tmp_path / "effects.json")
    a = EffectAnalyzer(db, cache)
    ids = collect_card_ids([log]) | {"HEAL", "NOT_IN_DB"}
    s1 = a.compile_seen_cards(cache, ids)
    assert (s1["compiled"], s1["reused"], s1["missing"]) == (2, 0, 2)
    assert (s1["damage"], s1["heal"]) == (1, 1)
    s2 = a.compile_seen_cards(cache, ids)
    assert (s2["compiled"], s2["reused"]) == (0, 2)   # 增量: 全部复用
