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


def test_two_segment_spell_damage(tmp_path):
    """多段伤害公式(2026-09-13 用户定版): (基础+法强)×段数 —— 每段都含
    基础值与法强伤害。旧公式 基础+法强×段数 在法强0 时把月光射线的第二段
    整段丢掉(实测开局斩杀少 2: 14 ≠ 16)。"""
    db = _db(tmp_path, [
        {"id": "TID_001", "name": "月光射线", "type": "SPELL",
         "text": "对一个敌人造成$1点伤害两次。"},
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
        {"id": "SC_753", "name": "光子炮台", "type": "MINION",
         "text": "<b>战吼：</b>造成$3点伤害。"},
        {"id": "BT_724", "name": "虚灵改装师", "type": "MINION",
         "text": "<b>战吼：</b>对一个随从造成1点伤害，并使其获得<b>法术伤害+1</b>。"},
    ])
    a = EffectAnalyzer(db)
    assert a.burst_damage("TID_001", 0) == 2          # 法强0: 1×2 段(曾错算 1)
    assert a.burst_damage("TID_001", 4) == 10         # (1+4)×2
    assert a.predict_damage("TID_001", 2) == {"total": 6, "hits": 2}
    assert a.burst_damage("CS2_008", 5) == 6          # 单段: (1+5)×1
    assert a.burst_damage("SC_753", 5) == 3           # 战吼不吃法强: 3×1
    # 裸数字=固定伤害: 解析器如实报告事实, 斩杀口径不计入(目标受限打脸不了)
    assert a.text_damage("BT_724") == (1, 1)
    assert a.burst_damage("BT_724", 5) is None


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


def test_mana_ramp_and_burst_damage(tmp_path):
    """信息区事实查询: 回费(获得/复原法力水晶, 措辞含空格换行)与
    当前法强下的单牌伤害潜力(法术/技能吃法强, 战吼按基础值)。"""
    from hsbot.effects import ManaGain, compile_card
    db = _db(tmp_path, [
        {"id": "EX1_169", "name": "激活", "type": "SPELL", "cost": 0,
         "text": "在本回合中，获得一个 法力水晶。"},          # 真实卡表带空格
        {"id": "GAME_005", "name": "幸运币", "type": "SPELL", "cost": 0,
         "text": "在本回合中，获得一个\n法力水晶。"},          # 换行容错
        {"id": "RLK_042", "name": "复原", "type": "SPELL", "cost": 1,
         "text": "复原两个法力水晶。"},
        {"id": "CS2_008", "name": "月火术", "type": "SPELL", "text": "造成$1点伤害。"},
        {"id": "MINION_DMG", "name": "战吼伤随从", "type": "MINION",
         "text": "战吼:造成$2点伤害。"},
    ])
    a = EffectAnalyzer(db)
    assert a.mana_ramp("EX1_169") == 1
    assert a.mana_ramp("GAME_005") == 1
    assert a.mana_ramp("RLK_042") == 2
    assert a.mana_ramp("CS2_008") is None
    assert a.mana_ramp(None) is None
    assert a.burst_damage("CS2_008", 3) == 4        # 法术: (基础+法强)×段数
    assert a.burst_damage("CS2_008", 0) == 1
    assert a.burst_damage("MINION_DMG", 3) == 2     # 战吼不吃法强
    assert a.burst_damage("EX1_169", 5) is None     # 无伤害效果
    ir = compile_card({"id": "X", "type": "SPELL", "text": "获得两个空的法力水晶。"})
    assert ManaGain(2) in ir.effects


def test_cost_down_and_multi_effect(tmp_path):
    """减费族(手牌/下一张, 类目词由文本直接提取)与多效果编译(异族各取一条):
    建造水晶塔=下一张星灵牌-2; 生命缚誓者的礼物=手牌法术-1; 伺机待发=下一个
    法术-2; 法力虹吸=伤害+减费并存; 抽牌减费(侦察)不入族; 月光射线=两段。"""
    from hsbot.effects import CostDown, Damage, compile_card

    db = _db(tmp_path, [
        {"id": "SC_755", "name": "建造水晶塔", "type": "SPELL", "cost": 0,
         "text": "在本回合中，你的下一张星灵牌法力值消耗减少（2）点。"},
        {"id": "TTN_955", "name": "生命缚誓者的礼物", "type": "SPELL", "cost": 2,
         "text": "<b>抉择：</b>随机获取2张自然法术牌；或者使你手牌中法术牌的"
                 "法力值消耗减少（1）点。"},
        {"id": "CORE_EX1_145", "name": "伺机待发", "type": "SPELL", "cost": 0,
         "text": "在本回合中，你所施放的下一个法术的法力值消耗减少（2）点。"},
        {"id": "AV_212", "name": "法力虹吸", "type": "SPELL", "cost": 2,
         "text": "造成$2点伤害。<b>荣誉消灭：</b>使你手牌中所有法术牌的"
                 "法力值消耗减少（1）点。"},
        {"id": "AV_710", "name": "侦察", "type": "SPELL", "cost": 2,
         "text": "<b>发现</b>一张另一职业的<b>亡语</b>随从牌，\n"
                 "其法力值消耗减少（2）点。"},
    ])
    a = EffectAnalyzer(db)
    assert a.cost_downs("SC_755") == [CostDown(2, "next:星灵")]
    assert a.mana_ramp("SC_755") is None            # 水晶口径不含减费
    assert a.mana_ramp_value("SC_755") == 2         # 等效回费: 减费按面值
    assert a.mana_ramp_value("TTN_955") == 1
    assert a.cost_downs("CORE_EX1_145") == [CostDown(2, "next:法术")]
    ir = a._compiled("AV_212")                      # 多效果: 伤害与减费并存
    assert Damage(2) in ir.effects
    assert CostDown(1, "hand:法术") in ir.effects
    assert a.mana_ramp_value("AV_710") is None      # 抽牌减费不属手牌/下一张族
    assert a.mana_ramp_value(None) is None
    ir2 = compile_card({"id": "TID_001", "type": "SPELL",
                        "text": "对一个敌人造成$1点伤害两次。"})
    assert Damage(1, 2) in ir2.effects              # 月光射线: 两段伤害


def test_trigger_enrich_does_not_invent_card_id():
    """2026-09-13 实测: 指纹猜名已下线 —— "开局+Idx=1+多卡库"在新环境会把
    阿扎莉娜/伊瑟拉等开局牌误标成雷纳索尔王子。命名只认日志揭示(SHOW_ENTITY)
    回填的 card_id; 未揭示时保持 None, 渲染层按开局窗口判弃。"""
    a = EffectAnalyzer(CardDB("/nonexistent.json"))
    evt = a.enrich({"kind": "trigger", "card_id": None,
                    "keyword": "START_OF_GAME_KEYWORD", "effect_index": 1,
                    "actor_deck_count": 36}, store=None)
    assert evt["card_id"] is None and "inferred" not in evt


def test_mechanic_marks_from_data_and_text(tmp_path):
    """机制标记(通用处理器): 数据级(HsJson mechanics[] 引擎标签)与文本级
    (语法表措辞)两通道统一编译为 IR Mechanic 标记, 与主效果正交叠加;
    消费方只查标记 kind, 不接触文本措辞/卡牌号。"""
    from hsbot.effects import Damage, Mechanic, compile_card

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
                        "mechanics": ["AURA"]})
    assert all(not isinstance(e, Mechanic) for e in ir4.effects)  # 未映射机制不打标
    # 2026-09-13 扩表: DISCOVER 已入机制映射表(引擎关键词级), 空文本也打标
    ir5 = compile_card({"id": "Z", "type": "SPELL", "text": "",
                        "mechanics": ["DISCOVER"]})
    assert Mechanic("discover") in ir5.effects


def test_spellpower_ir(tmp_path):
    """切片1(T1/PLAY_ADVICE §6.1): 法术伤害增益入 IR —— 虚灵改装师"使其获得
    法术伤害+1"是打脸 DFS 的法强增量事实源; <b> 标签与"获得"前缀容错;
    序列化往返。消费方(planner.piece)在切片2 接线。"""
    from hsbot.effects import Damage, SpellPower, compile_card
    ir = compile_card({"id": "BT_724", "type": "MINION",
                       "text": "<b>战吼：</b>对一个随从造成1点伤害，"
                               "并使其获得<b>法术伤害+1</b>。"})
    assert SpellPower(1) in ir.effects
    assert Damage(1, 1, "single", False) in ir.effects      # 原有事实不丢
    ir2 = compile_card({"id": "S1", "type": "SPELL", "text": "你的法术伤害+2。"})
    assert SpellPower(2) in ir2.effects


def test_cast_draw_mechanic(tmp_path):
    """切片1(T1/PLAY_ADVICE §6.1): 施法抽牌触发入机制标记 —— 黑市拍卖师
    "每当你施放一个法术后，抽一张牌" 是奇迹德 OTK 引擎; 触发句不入既有
    抽牌族(那里 (?<!每) 镜像明确挡它), 走 Mechanic("cast_draw") 文本通道。"""
    from hsbot.effects import (Damage, Mechanic, compile_card)
    ir = compile_card({"id": "EX1_095", "type": "MINION",
                       "text": "每当你施放一个法术后，抽一张牌。"})
    assert Mechanic("cast_draw") in ir.effects
    assert not any(isinstance(e, Damage) for e in ir.effects)  # 触发句不误入伤害/抽牌族
    # 普通抽牌句不受影响(抽牌族照旧, 不打 cast_draw 标)
    ir2 = compile_card({"id": "S2", "type": "SPELL", "text": "抽两张牌。"})
    assert Mechanic("cast_draw") not in ir2.effects


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


def test_grammar_expansion_seen_cards():
    """2026-09-13 扩表(出现牌驱动, COMPILER_VERSION=6): 英雄技能占位(#N 固定值/
    $d 护甲/$a 攻击模板)、抽牌/属性增益/召唤/护甲/加费族、附慕自减(zh+EN)。
    抽牌规则用否定镜挡掉"每抽一张牌"(动态减费条件)与"对手抽牌"两种非本牌
    抽牌事实; 回归红线: 水栖形态/月光射线/法力虹吸的既有 IR 不变。"""
    from hsbot.effects import (Armor, Buff, CostDown, CostUp, Damage, Draw,
                               Heal, ManaGain, Mechanic, Summon, compile_card)

    def ir(cid, text, **kw):
        return compile_card({"id": cid, "type": kw.pop("type", "SPELL"),
                             "text": text, **kw}).effects

    # 英雄技能族(占位符: #N=固定值, $d=护甲, $a=攻击)
    assert Armor(2) in ir("H1", "<b>英雄技能</b>\n获得$d2点护甲值。")
    assert Heal(2) in ir("H2", "<b>英雄技能</b>\n恢复#2点生命值。")
    fx = ir("H3", "<b>英雄技能</b>\n本回合+$a1攻击力。+$d1护甲值。")
    assert Buff(1, 0) in fx and Armor(1) in fx
    # 抽牌族
    assert Draw(1) in ir("A", "抽一张牌。获得5点护甲值。") \
        and Armor(5) in ir("A", "抽一张牌。获得5点护甲值。")
    assert Draw(2, "both") in ir("B", "每个玩家抽两张牌。")
    assert Draw(1, "opponent") in ir("C", "<b>亡语：</b>你的对手抽一张牌。")
    assert Draw(2, "deck") in ir("D", "从你的牌库中抽两张攻击力为1的随从牌。")
    assert Draw(1, "lowest") in ir("E", "抽取你法力值消耗最低的牌。")
    fx = ir("F", "每当一张牌被抽到，使用或摧毁时，本牌的法力值消耗便减少（1）点。",
            mechanics=["BATTLECRY"])
    assert CostDown(1, "self") in fx and not any(isinstance(e, Draw) for e in fx)
    fx = ir("G", "在你的对手抽一张牌后，使其法力值消耗增加（1）点。")
    assert not any(isinstance(e, Draw) for e in fx) and CostUp(1) in fx
    # 增益/召唤/护甲/加费
    assert Buff(1, 1) in ir("I", "+1/+1。")
    assert Summon(2, 1, 1) in ir("J", "召唤两个1/1的树苗。")
    assert Summon(3, 1, 1, scope="opponent") in ir("K", "为你的对手召唤三个1/1的女猎手。")
    assert ManaGain(3) in ir("L", "获得3个法力水晶。")
    assert ManaGain(1) in ir("M", "在本回合中你每施放过一个法术，复原一个空的法力水晶。")
    assert CostUp(5) in ir("N", "在本回合中，你的法术法力值消耗增加（5）点。")
    # 减费族补充: 附慕裸句/EN/抽到的牌/target(不入回费口径)
    assert CostDown(1, "self") in ir("O", "法力值消耗减少（1）点。")
    assert CostDown(1, "self") in ir("P", "Costs (1) less.")
    fx = ir("Q", "<b>任务线：</b>在一回合中抽四张牌。<b>奖励：</b>使抽到的牌"
                "法力值消耗减少（1）点。")
    assert CostDown(1, "drawn") in fx and Draw(4) in fx
    assert CostDown(2, "target") in ir("R", "<b>发现</b>一张随从牌，其法力值消耗"
                                            "减少（2）点。")
    # 伤害族补口: 对所有随从 = 随从-only AoE(scope 区分, 打不了脸;
    # 2026-09-14 修正规则序后不再需要手工换行绕过裸规则截胡)
    assert Damage(2, scope="all_minions") in ir("S", "对所有随从造成$2点伤害。")
    # 机制数据通道: 关键词卡(mechanics[])自动打标
    assert Mechanic("charge") in ir("T", "<b>冲锋</b>", mechanics=["CHARGE"])


def test_expansion_json_roundtrip():
    """新 IR kind 的缓存序列化往返。"""
    from hsbot.effects import (Armor, Buff, CostUp, Draw, Summon, _effects_from_json,
                               _effects_to_json, compile_card)
    fx = compile_card({"id": "X", "type": "MINION",
                       "text": "召唤两个1/1的树苗。抽一张牌。获得5点护甲值。"
                               "获得+1/+1。法力值消耗增加（2）点。"}).effects
    assert Summon(2, 1, 1) in fx and Draw(1) in fx and Armor(5) in fx
    assert Buff(1, 1) in fx and CostUp(2) in fx
    rt = _effects_from_json(_effects_to_json(fx))
    for e in fx:
        assert e in rt


def test_compile_seen_cards_coverage_semantics(tmp_path):
    """新统计口径: covered=至少一条已知效果; notext=无文本; uncovered=有文本
    未覆盖; 旧键 damage/heal/unknown 保持原语义。"""
    from hsbot.effects import EffectCache
    db = _db(tmp_path, [
        {"id": "TAUNT", "name": "嘲讽墙", "type": "MINION", "text": "<b>嘲讽</b>",
         "mechanics": ["TAUNT"]},
        {"id": "TOKEN", "name": "树苗", "type": "MINION", "text": ""},
        {"id": "ODD", "name": "怪话", "type": "MINION", "text": "变成最近使用过的随从牌。"},
    ])
    cache = EffectCache(tmp_path / "effects.json")
    s = EffectAnalyzer(db, cache).compile_seen_cards(cache, {"TAUNT", "TOKEN", "ODD"})
    assert (s["covered"], s["notext"], s["uncovered"]) == (1, 1, 1)
    assert s["kinds"] == {"Mechanic": 1}
    assert s["unknown"] == 3        # 旧口径: 均无伤害/治疗


# ---------------- 审计 2026-09-14: 语法规则序与法强守卫 ----------------

def test_aoe_damage_scope_not_hijacked_by_bare_rule():
    """审计 中#5: 裸"造成N点伤害"规则排在 AoE 规则之前, "对所有敌方随从
    造成$3点伤害"被截胡成 scope=single(随从-only AoE 当直伤打脸 → 误报)。
    AoE 措辞必须先命中; 随从-only 与 全角色 分立 scope。"""
    from hsbot.effects import compile_card
    ir = compile_card({"id": "X1", "text": "对所有敌方随从造成$3点伤害",
                       "type": "SPELL"})
    dmg = [e for e in ir.effects if isinstance(e, Damage)][0]
    assert dmg.scope == "all_minions"
    ir2 = compile_card({"id": "X2", "text": "对所有角色造成$2点伤害",
                        "type": "SPELL"})
    dmg2 = [e for e in ir2.effects if isinstance(e, Damage)][0]
    assert dmg2.scope == "all"           # 角色含英雄 → 脸在范围内


def test_spellpower_temp_and_both_players_guards():
    """审计 中#6: 临时法强("下一个法术伤害+N")与"双方玩家的法术伤害+N"被
    编译为永久我方法强 → 线内后续法术全多算(误报可斩)。宁漏勿错: 不编译;
    常规永久法强照常编译。"""
    from hsbot.effects import SpellPower, compile_card
    for text in ("使你的下一个法术伤害+2", "双方玩家的法术伤害+1"):
        ir = compile_card({"id": "Y", "text": text, "type": "SPELL"})
        assert not any(isinstance(e, SpellPower) for e in ir.effects), text
    ir = compile_card({"id": "Y2", "text": "<b>法术伤害+1</b>", "type": "MINION"})
    assert any(isinstance(e, SpellPower) for e in ir.effects)
