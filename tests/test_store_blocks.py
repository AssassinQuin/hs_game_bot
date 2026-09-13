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


def test_play_cost_is_real_hand_price():
    """2026-09-13 实测: 联动减益(打出上一张后下一张 0 费)的回退(0→2)写在
    打出块内部, 块尾 flush 读 COST 拿到原费 → 输出 (2费) 而实付 0。
    真实价必须在块开始时锁定。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(10, "SC_753", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, COST=2))
    st.apply(mk_tag(10, GameTag.COST, 0))          # 联动减益: 手牌价 2→0
    b = mk_block(BlockType.PLAY, 10)
    st.apply(b, depth=0)                           # 块开始: 锁定真实价 0
    st.apply(mk_tag(10, GameTag.COST, 2), depth=1)  # 块内回退(噪音)
    b.end()
    st.settle()
    play = log.by_kind("play")[-1]
    assert play["cost_tag"] == 0                   # 锁定的是块开始价, 不是块尾回退价
    from hsbot.render import chain_line
    line = chain_line(dict(play, turn=9, friendly=1), st.carddb)
    assert "(实付0费)" in line                     # 卡表缺失: 只知实付价
    line2 = chain_line(dict(play, cost_base=2, turn=9, friendly=1), st.carddb)
    assert "(0费,原2费)" in line2                  # 卡表有基价: 真实价优先, 原价备注


def test_trigger_and_fatigue_events():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(30, "TTN_910", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2))
    st.apply(mk_block(BlockType.TRIGGER, 30))
    assert log.by_kind("trigger")[-1]["card_id"] == "TTN_910"
    # 疲劳: 新版日志 FATIGUE 块的 Entity 是英雄实体, 事件挂在块内
    # FATIGUE 标签变更上(玩家+次数); 块本身不再发事件
    st.apply(mk_block(BlockType.FATIGUE, 4))
    assert log.by_kind("fatigue") == []
    st.apply(mk_tag(3, GameTag.FATIGUE, 2))           # 玩家实体的疲劳计数
    fat = log.by_kind("fatigue")[-1]
    assert fat["actor"] == 2 and fat["count"] == 2
    st.apply(mk_tag(4, GameTag.DAMAGE, 2))            # 英雄掉血 → 文本行衔接
    assert any("我方英雄" in e.get("msg", "") for e in log.by_kind("text"))


def test_hidden_start_of_game_trigger_inferred_renathal():
    """隐藏开局触发可判定: 40 卡组局 + START_OF_GAME + EffectIndex=1 → 雷纳索尔王子;
    30 卡组局不推断。"""
    st, log = _store()
    _heroes(st)
    for i in range(100, 131):                      # 31 张牌库 → 40 卡组局
        st.apply(mk_full(i, None, ZONE=Zone.DECK.value, CONTROLLER=2))
    st.apply(mk_full(16, None, ZONE=Zone.DECK.value, CONTROLLER=2))
    # 雷纳索尔指纹: START_OF_GAME + EffectIndex=1
    b = packets.Block(TS, 16, BlockType.TRIGGER, None, None, 1, 0, None,
                      GameTag.START_OF_GAME_KEYWORD)
    st.apply(b)
    trig = log.by_kind("trigger")[-1]
    assert trig["card_id"] is None                      # store 不做推断
    assert trig["effect_index"] == 1 and trig["actor_deck_count"] == 32
    from hsbot.analysis import EffectAnalyzer
    from hsbot.render import chain_line
    enriched = EffectAnalyzer(st.carddb).enrich(dict(trig), st)
    assert enriched["card_id"] == "REV_018" and enriched.get("inferred") is True
    line = chain_line(enriched, st.carddb)
    assert "REV_018" in line and "(开局)" in line
    # 30 卡组局: 对手牌库只剩 ≤30 → 不推断
    st2, log2 = _store()
    _heroes(st2)
    for i in range(200, 226):
        st2.apply(mk_full(i, None, ZONE=Zone.DECK.value, CONTROLLER=2))
    st2.apply(mk_full(16, None, ZONE=Zone.DECK.value, CONTROLLER=2))
    b2 = packets.Block(TS, 16, BlockType.TRIGGER, None, None, 1, 0, None,
                       GameTag.START_OF_GAME_KEYWORD)
    st2.apply(b2)
    assert log2.by_kind("trigger")[-1]["card_id"] is None


def test_enchantment_chain_links():
    """连锁关联: 附魔(ENCHANTMENT+ATTACHED)挂到宿主牌 →
    费用变动带 via(谁减的费), 触发带 host(挂在哪张牌), to_dict 手牌带 effects。"""
    from hsbot.render import chain_line
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "SC_753", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, COST=2))
    st.apply(mk_full(90, "JAIL_430e", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     CARDTYPE=CardType.ENCHANTMENT.value, ATTACHED=10))
    st.apply(mk_tag(10, GameTag.COST, 0))
    cost = log.by_kind("cost")[-1]
    assert cost["eid"] == 10 and "via" not in cost     # store 只发事实
    from hsbot.analysis import EffectAnalyzer
    enriched = EffectAnalyzer(st.carddb).enrich(dict(cost), st)
    assert enriched["via"] == ["JAIL_430e"]            # 解析层富化归因
    assert st.enchantments_on(10) == ["JAIL_430e"]
    assert st.to_dict("t")["me"]["hand"][0]["effects"] == ["JAIL_430e"]
    line = chain_line(dict(enriched, turn=9, friendly=1), st.carddb)
    assert "〔JAIL_430e〕" in line
    # 触发的宿主链: 附魔 20 挂在随从 30 上
    st.apply(mk_full(30, "JAIL_718", ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.MINION.value))
    st.apply(mk_full(20, "JAIL_907e02", ZONE=Zone.SETASIDE.value, CONTROLLER=2,
                     CARDTYPE=CardType.ENCHANTMENT.value, ATTACHED=30))
    st.apply(mk_block(BlockType.TRIGGER, 20))
    trig = log.by_kind("trigger")[-1]
    assert trig["host"] == "JAIL_718"
    line = chain_line(dict(trig, turn=5, friendly=1), st.carddb)
    assert "〔JAIL_718〕" in line


def test_cosmetic_pet_triggers_ignored():
    """宠物(PET/COSMETIC)是装饰实体, 其触发不报事件、不入训练语料。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(74, "PET_12_4", ZONE=Zone.COSMETIC.value, CONTROLLER=2,
                     CARDTYPE=CardType.PET.value))
    n = len(log.events)
    st.apply(mk_block(BlockType.TRIGGER, 74))
    assert len(log.events) == n                  # 不发任何事件
    assert st.is_cosmetic_entity(st.get(74))
    # 训练导出剔除
    from hsbot.carddb import CardDB
    from hsbot.corpus import CorpusExporter
    from hsbot.config import Config
    import tempfile
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp),
                       "training_dir": str(tmp / "training")})
    ex = CorpusExporter(cfg, CardDB("/nonexistent.json"))

    class _T:
        ts = TS
        packets = [mk_create_game(), mk_full(74, "PET_12_4",
                   ZONE=Zone.COSMETIC.value, CONTROLLER=2,
                   CARDTYPE=CardType.PET.value),
                   mk_block(BlockType.TRIGGER, 74),
                   mk_full(30, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1,
                           CARDTYPE=CardType.SPELL.value, ZONE_POSITION=1)]

    path = ex.export_game(_T(), session="s", idx=1)
    text = path.read_text(encoding="utf-8")
    assert "PET_12_4" not in text
    assert "CS2_029" in text                     # 正常事件保留


def test_hero_power_change_event():
    """灌注/替换: 新技能实体进 PLAY → hero_power 事件; 初始技能登记不发声。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(20, "HERO_05bp", ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.HERO_POWER.value))
    assert log.by_kind("hero_power") == []          # 开局登记静默
    st.apply(mk_full(21, "EDR_449p", ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.HERO_POWER.value))
    evs = log.by_kind("hero_power")
    assert len(evs) == 1 and evs[0]["actor"] == 2 and evs[0]["card_id"] == "EDR_449p"
    assert st.hero_power_cid[2] == "EDR_449p"
    from hsbot.render import chain_line
    line = chain_line(dict(evs[0], turn=5, friendly=1), st.carddb)
    assert "英雄技能 → EDR_449p" in line
    d = st.to_dict("t")
    assert d["players"][2]["hero_power"] == "EDR_449p"


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


def test_subspell_children_register_and_attack_side():
    """2026-09-13 实测回归: SUB_SPELL 里创建的 token(FULL_ENTITY) 从未进游标平铺,
    实体注册不上 → 攻击方名字靠括号兜底、阵营变 '?'(JAIL_877t 案例)。"""
    st, log = _store()
    _heroes(st)
    sub = packets.SubSpell(TS, "ReuseFX_SpawnToHand", 15, 0)
    st.apply(sub, 0)
    st.apply(mk_full(110, "JAIL_877t", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, ATK=2, HEALTH=1), 1)
    assert st.get(110) is not None
    st.apply(mk_block(BlockType.ATTACK, 110, target=4), 1)
    atk = log.by_kind("attack")[-1]
    assert atk["attacker_card_id"] == "JAIL_877t"
    assert atk["actor"] == 2                       # controller 标签已随实体注册


def test_trigger_actor_hint_ctrl_fallback():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(20, None, ZONE=Zone.SETASIDE.value, CONTROLLER=2))
    st.hint_ctrl(20, 2)
    st.apply(mk_show(20, "JAIL_907e02", ZONE=Zone.PLAY.value))
    st.apply(mk_block(BlockType.TRIGGER, 20))
    trig = log.by_kind("trigger")[-1]
    assert trig["actor"] == 2 and trig["eid"] == 20
    assert trig["card_id"] == "JAIL_907e02"


def test_walk_descends_into_subspell():
    from hsbot.adapter import walk_packets
    sub = packets.SubSpell(TS, "ReuseFX_SpawnToHand", 15, 0)
    child = mk_full(110, "JAIL_877t", CARDTYPE=CardType.MINION.value,
                    ZONE=Zone.PLAY.value, CONTROLLER=2)
    sub.packets = [child]

    class _T:
        packets = [mk_block(BlockType.POWER, 1), sub]

    flat = [type(p).__name__ for p, _ in walk_packets(_T())]
    assert flat == ["Block", "SubSpell", "FullEntity"]
