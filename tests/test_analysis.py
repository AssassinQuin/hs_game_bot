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
