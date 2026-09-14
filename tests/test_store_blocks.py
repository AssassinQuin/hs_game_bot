"""GameStore 块与选择: PLAY 延迟/attack/trigger/fatigue/留牌/发现/to_dict。"""
from hearthstone.enums import BlockType, CardType, ChoiceType, GameTag, Zone
from hslog import packets

from hsbot.consts import TAG_PREPARING

from .conftest import (TS, _ref, mk_block, mk_choices_general,
                       mk_choices_mulligan, mk_create_game, mk_full, mk_hide,
                       mk_heroes as _heroes, mk_send_general, mk_send_mulligan,
                       mk_show, mk_store as _store, mk_tag)


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


def test_play_precedes_its_draw_and_choice_fact():
    """2026-09-13 实测: 抉择打出引发的抽牌曾排在打出之前(PLAY 块延迟发的副作用)。
    顺序必须 打出→抽到; store 只发原始事实(suboption 下标+eid), 子卡解析归 analysis。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(53, "AV_295", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, COST=2))
    # 抉择按钮实体: PARENT_CARD=主卡, 实体号序即抉择顺序
    st.apply(mk_full(54, "AV_295a", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     PARENT_CARD=53))
    st.apply(mk_full(55, "AV_295b", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     PARENT_CARD=53))
    st.apply(mk_full(83, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    b = packets.Block(TS, 53, BlockType.PLAY, None, None, None, 0, 1, None)
    st.apply(b, depth=0)
    st.apply(mk_show(83, "JAIL_718", ZONE=Zone.HAND.value), depth=1)  # 块内抽牌
    b.end()
    st.settle()
    kinds = log.kinds()
    assert kinds.index("play") < kinds.index("draw")
    play = log.by_kind("play")[0]
    assert play["suboption"] == 1 and play["eid"] == 53
    assert "suboption_card_id" not in play
    # 解析层富化: 下标1 → 按钮实体55 = AV_295b; 渲染带子卡名
    from hsbot.analysis import EffectAnalyzer
    from hsbot.render import chain_line
    enriched = EffectAnalyzer(st.carddb).enrich(dict(play), st)
    assert enriched["suboption_card_id"] == "AV_295b"
    line = chain_line(dict(enriched, turn=3, friendly=1), st.carddb)
    assert "抉择2·AV_295b" in line


def test_hint_draw_inside_pending_play_ordered():
    """行级抽牌 hint(括号 SHOW_ENTITY, hslog 不解析)发生在挂起 PLAY 期间,
    同样扣留到 play 事件之后冲出 —— 与包路径同序。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(53, "AV_295", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value))
    b = mk_block(BlockType.PLAY, 53)
    st.apply(b, depth=0)
    st.hint_draw(83, "JAIL_718", 1)
    b.end()
    st.settle()
    kinds = log.kinds()
    assert kinds.index("play") < kinds.index("draw")


def test_deferred_drained_even_when_play_bails():
    """play 事件发不出(实体未知→早退)时, 块内扣留的事件仍须冲出, 不得吞掉。"""
    st, log = _store()
    _heroes(st)
    b = mk_block(BlockType.PLAY, 999)      # 实体不存在 → flush 早退
    st.apply(b, depth=0)
    st.hint_draw(83, "JAIL_718", 1)
    b.end()
    st.settle()
    assert log.kinds().count("draw") == 1
    assert log.kinds().count("play") == 0


def _prepare_sequence(st):
    """按 20_48_57_g03 实测的 DECK_ACTION 预备块原始结构搭一局:
    块开始(暂存预备完成) → 挂"正在预备"附魔 → 宿主 PREPARING=1 →
    挂"预备完毕"附魔 → 块内减费 9→4 → 收口。"""
    st.apply(mk_full(83, "JAIL_718", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, COST=9, PREPARE=1))
    b = mk_block(BlockType.DECK_ACTION, 83)
    st.apply(b, depth=0)
    st.apply(mk_full(84, "JAIL_907e02", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     CARDTYPE=CardType.ENCHANTMENT.value, ATTACHED=83), depth=1)
    st.apply(mk_tag(84, GameTag.ZONE, Zone.PLAY.value), depth=1)
    st.apply(mk_tag(83, TAG_PREPARING, 1), depth=1)   # 触发+预备完成在此发出
    st.apply(mk_full(85, "JAIL_907e", ZONE=Zone.SETASIDE.value, CONTROLLER=1,
                     CARDTYPE=CardType.ENCHANTMENT.value, ATTACHED=83), depth=1)
    st.apply(mk_tag(83, GameTag.COST, 4), depth=1)   # 块内: 预备减费 9→4
    return b


def test_prepare_trigger_precedes_prepare_and_cost():
    """2026-09-13 实测(游戏 4409fd9a): 尾随约1秒的"正在预备"TRIGGER 块按日志
    原位垫底, 因果序全反 —— 触发是预备的起点, 必须最前; 预备完成先于其费用
    变化不变。真身触发块随后按实体号去重, 不再重复出链路行。"""
    st, log = _store()
    _heroes(st)
    b = _prepare_sequence(st)
    b.end()
    st.settle()
    kinds = log.kinds()
    assert kinds.index("trigger") < kinds.index("prepare") < kinds.index("cost")
    trig = [e for e in log.by_kind("trigger") if e["eid"] == 84][0]
    assert trig["card_id"] == "JAIL_907e02" and trig["host"] == "JAIL_718"
    assert trig["actor"] == 1
    from hsbot.render import chain_line
    line = chain_line(dict(trig, turn=7, friendly=1), st.carddb)
    assert "触发 JAIL_907e02〔JAIL_718〕" in line
    prep = log.by_kind("prepare")[0]
    assert prep["card_id"] == "JAIL_718" and prep["actor"] == 1
    assert "预备完成 JAIL_718" in chain_line(dict(prep, turn=7, friendly=1),
                                            st.carddb)


def test_prepare_trailing_real_trigger_deduped():
    st, log = _store()
    _heroes(st)
    b = _prepare_sequence(st)
    b.end()
    st.settle()
    n_before = len(log.by_kind("trigger"))
    t = mk_block(BlockType.TRIGGER, 84)          # 尾随约1秒的真身触发块
    st.apply(t, depth=0)
    t.end()
    st.settle()
    assert len(log.by_kind("trigger")) == n_before


def test_prepare_variant_without_preparing_flip_still_reported():
    """变体形态: 预备块内无 PREPARING 翻转(亦无附魔可归因)时, 块尾按原行为
    补发预备完成; 其后无关触发动不受去重影响。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(83, "JAIL_718", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, COST=9, PREPARE=1))
    b = mk_block(BlockType.DECK_ACTION, 83)
    st.apply(b, depth=0)
    st.apply(mk_tag(83, GameTag.COST, 4), depth=1)   # 块内: 预备减费 9→4
    b.end()
    st.settle()
    prep = log.by_kind("prepare")
    assert prep and prep[0]["card_id"] == "JAIL_718" and prep[0]["actor"] == 1
    from hsbot.render import chain_line
    assert "预备完成 JAIL_718" in chain_line(dict(prep[0], turn=7, friendly=1),
                                            st.carddb)
    st.apply(mk_full(90, "JAIL_907e02", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.ENCHANTMENT.value, ATTACHED=83))
    t = mk_block(BlockType.TRIGGER, 90)
    st.apply(t, depth=0)
    t.end()
    st.settle()
    assert any(e["eid"] == 90 for e in log.by_kind("trigger"))


def test_deck_action_without_prepare_not_reported():
    """PREPARE=1 才认定预备完成: 其他 DECK_ACTION 块不误报。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(20, "CS2_008", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value))
    st.apply(mk_block(BlockType.DECK_ACTION, 20))
    assert log.by_kind("prepare") == []


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


def test_hidden_start_of_game_trigger_stays_unnamed():
    """2026-09-13: 指纹猜名已下线 —— 隐藏开局触发只记机读事实(card_id=None、
    effect_index/actor_deck_count), 不硬编码猜雷纳索尔; 命名只认日志揭示
    (SHOW_ENTITY)回填的 card_id, 开局窗口渲染判弃(见 test_render_knowledge)。"""
    st, log = _store()
    _heroes(st)
    for i in range(100, 131):                      # 31 张牌库 → 40 卡组局
        st.apply(mk_full(i, None, ZONE=Zone.DECK.value, CONTROLLER=2))
    st.apply(mk_full(16, None, ZONE=Zone.DECK.value, CONTROLLER=2))
    # 旧指纹特征: START_OF_GAME + EffectIndex=1(阿扎莉娜/伊瑟拉等同会误中)
    b = packets.Block(TS, 16, BlockType.TRIGGER, None, None, 1, 0, None,
                      GameTag.START_OF_GAME_KEYWORD)
    st.apply(b)
    trig = log.by_kind("trigger")[-1]
    assert trig["card_id"] is None and "inferred" not in trig
    assert trig["effect_index"] == 1 and trig["actor_deck_count"] == 32
    from hsbot.analysis import EffectAnalyzer
    from hsbot.render import chain_line
    enriched = EffectAnalyzer(st.carddb).enrich(dict(trig), st)
    assert enriched["card_id"] is None             # 不给事件流编造 id
    assert chain_line(enriched, st.carddb) is None  # 开局未揭示: 渲染判弃


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
    # 联动卡效果台账(对局存档持久化): 附魔 → 宿主 + 来源卡, 含 PLAY 区在身
    st.apply(mk_full(91, "SC_755e", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.ENCHANTMENT.value, ATTACHED=10, CREATOR=18))
    st.apply(mk_full(18, "SC_755", ZONE=Zone.GRAVEYARD.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value))
    links = {l["cid"]: l for l in st.to_dict("t")["linked_effects"]}
    assert links["JAIL_430e"]["host"] == 10
    assert links["JAIL_430e"]["host_cid"] == "SC_753"
    assert links["JAIL_907e02"]["host_cid"] == "JAIL_718"
    assert links["SC_755e"]["creator"] == "SC_755"   # 归因到打出的来源卡
    assert links["SC_755e"]["creator_eid"] == 18
    assert links["SC_755e"]["zone"] == "PLAY"        # 附魔在身时的实际区
    assert links["JAIL_430e"]["creator"] is None     # 无 CREATOR: 诚实留空


def test_cosmetic_pet_triggers_ignored(tmp_path):
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
    tmp = tmp_path
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


def test_hero_power_hidden_then_revealed_emits_once():
    """审计 2026-09-13 中#4: 技能实体先无名进 PLAY 再揭示时,
    不得连发 "#22" 与真名两条 —— 未知名静默登记, 揭示后才报一条。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(20, "HERO_05bp", ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.HERO_POWER.value))
    st.apply(mk_full(21, "EDR_449p", ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.HERO_POWER.value))
    assert len(log.by_kind("hero_power")) == 1
    # 换成未知名实体: 只登记(旧实现此处会发一条只有实体号的 "#22")
    st.apply(mk_full(22, None, ZONE=Zone.PLAY.value, CONTROLLER=2,
                     CARDTYPE=CardType.HERO_POWER.value))
    assert st.hero_power[2] == 22
    assert len(log.by_kind("hero_power")) == 1
    # 揭示后: 恰好一条, 带真名
    st.apply(mk_show(22, "HERO_05bp", ZONE=Zone.PLAY.value))
    evs = log.by_kind("hero_power")
    assert len(evs) == 2 and evs[-1]["card_id"] == "HERO_05bp" and evs[-1]["eid"] == 22
    assert st.hero_power_cid[2] == "HERO_05bp"


def test_mulligan_flow():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "OG_048", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "GAME_005", ZONE=Zone.DECK.value, CONTROLLER=2))
    st.apply(mk_choices_mulligan(2, 7, [10]))              # 我方(entity2=pid1)起手
    st.apply(mk_send_mulligan(7, [10]))                    # 留下
    m = log.by_kind("mulligan")
    assert m and "留牌: OG_048" in m[0]["msg"]


def test_stolen_original_leaves_ledger():
    """嫉妒收割者类"偷取本体"(我方实体控制权转对手): 台账扣除被偷卡 ——
    牌库剩余期望/库侧斩杀/费用不再多算(2026-09-13 用户要求)。"""
    from hsbot.knowledge import DeckKnowledge

    st, _log = _store()
    _heroes(st)
    st.note_friendly(1)
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.DECK.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, COST=1))
    k = DeckKnowledge({"CS2_029": 2, "EX1_169": 2}, st.carddb, "测试")
    assert k.rebuild(st).remaining["CS2_029"] == 2
    st.apply(mk_tag(10, GameTag.CONTROLLER, 2))            # 本体被偷(暗牌未揭示)
    assert 10 in st.stolen_eids
    led = k.rebuild(st)
    assert led.remaining["CS2_029"] == 1 and led.lost["CS2_029"] == 1
    assert led.remaining["EX1_169"] == 2                   # 其余台账不动


def test_mulligan_replacement_draws_recorded():
    """换牌换入(决定后、首回合前的抽牌)记入 MulliganState.replaced_in,
    随 mulligan_facts 落盘进留牌训练特征(2026-09-13 用户要求)。"""
    st, _log = _store()
    _heroes(st)
    st.apply(mk_full(10, "OG_048", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "CS2_029", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_choices_mulligan(2, 7, [10, 11]))          # 我方起手两张
    st.apply(mk_send_mulligan(7, [10]))                    # 留 10, 换 11
    st.hint_draw(12, "EX1_169", 1)                         # 换入: 决定后的抽牌
    facts = st.mulligan_facts()[1]
    assert facts["replaced_in"] == ["EX1_169"]
    st._on_turn_start(1)                                   # 首回合开始: 窗口关闭
    st.hint_draw(13, "OG_048", 1)                          # T1 常规抽牌不误记
    assert st.mulligan_facts()[1]["replaced_in"] == ["EX1_169"]


def test_mulligan_replaced_in_first_player_count_gate():
    """先手局 gotcha 40: 我方 T1 turn_start(CURRENT_PLAYER 翻转驱动)先于留牌
    决定发生, "决定后首回合开始"关闸失效 —— 换入窗口整个 T1 敞开, T1 常规抽/
    效果抽被误记进 replaced_in(g12 实证 5 张, 应 3)。数量闸: 换几张补几张,
    决定后补牌紧随 SendChoices 成批到达, 凑满应补数即关窗。"""
    st, _log = _store()
    _heroes(st)
    for eid, cid in [(10, "TIME_701"), (11, "VAC_519"), (12, "AV_295"),
                     (13, "EX1_169")]:
        st.apply(mk_full(eid, cid, ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st._on_turn_start(1)                                   # 先手 T1: 在决定之前!
    st.apply(mk_choices_mulligan(2, 7, [10, 11, 12]))      # 起手三张
    st.apply(mk_send_mulligan(7, []))                      # 全换 → 应补 3 张
    st.hint_draw(20, "TIME_701", 1)                        # 补牌紧随决定成批到达
    st.hint_draw(21, "VAC_519", 1)
    st.hint_draw(22, "AV_295", 1)                          # 第 3 张: 凑满 → 关窗
    facts = st.mulligan_facts()[1]
    assert facts["replaced_in"] == ["TIME_701", "VAC_519", "AV_295"]
    st.hint_draw(13, "EX1_169", 1)                         # T1 回合抽: 不入
    st.hint_draw(23, "SCH_427", 1)                         # T1 效果抽: 不入
    assert st.mulligan_facts()[1]["replaced_in"] == ["TIME_701", "VAC_519", "AV_295"]


def test_mulligan_replaced_in_gate_skips_coin():
    """后手局数量闸: 硬币不属于补牌(gotcha 9 换掉=offered−kept−硬币),
    不计应补数也不入 replaced_in; 补牌凑满即关窗, 早于首回合 turn_start。"""
    st, _log = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "EX1_169", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(12, "GAME_005", ZONE=Zone.HAND.value, CONTROLLER=1))
    st.apply(mk_choices_mulligan(2, 7, [10, 11, 12]))      # 后手: 可留含硬币
    st.apply(mk_send_mulligan(7, [10, 12]))                # 留 10+硬币, 换 11
    st.hint_draw(13, "VAC_519", 1)                         # 唯一补牌 → 凑满关窗
    st.hint_draw(14, "GAME_005", 1)                        # 硬币换入路径: 不入账
    st.hint_draw(15, "OG_048", 1)                          # 其后抽牌: 已关
    assert st.mulligan_facts()[1]["replaced_in"] == ["VAC_519"]


def test_mulligan_replaced_in_keep_all_closes_at_decide():
    """全留(换 0 张): 决定即关窗, 其后任何抽牌(含 T1)不入 replaced_in。"""
    st, _log = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "EX1_169", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_choices_mulligan(2, 7, [10, 11]))
    st.apply(mk_send_mulligan(7, [10, 11]))                # 全留
    st.hint_draw(12, "OG_048", 1)                          # T1 抽牌
    assert st.mulligan_facts()[1]["replaced_in"] == []


def test_mulligan_replaced_in_underfill_falls_back_to_turn_start():
    """补牌缺额(隐藏抽牌 cid 空 / 引擎异常, 计不满应补数): 窗口不提前关,
    turn_start 兜底关窗 —— 不死锁, 缺额局的 T1 抽牌仍被关闸挡住。"""
    st, _log = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "EX1_169", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_choices_mulligan(2, 7, [10, 11]))
    st.apply(mk_send_mulligan(7, [10]))                    # 换 1 张, 补牌未揭示
    st.hint_draw(12, "", 1)                                # cid 空: 不计数不死锁
    assert st.mulligan_facts()[1]["replaced_in"] == []
    st._on_turn_start(1)                                   # 兜底关窗
    st.hint_draw(13, "OG_048", 1)
    assert st.mulligan_facts()[1]["replaced_in"] == []


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


def test_unhandled_bounded_and_listable():
    """deque(maxlen) 自动裁剪; 快照 payload 走 list() 转换
    (真实日志回归: deque 不可切片, unhandled[-50:] 曾炸掉终局快照)。"""
    st, _ = _store()
    _heroes(st)
    for i in range(520):
        st._record_raw(f"X{i}", mk_tag(2, GameTag.TURN, i))
    assert len(st.unhandled) == 500
    assert st.unhandled[0]["packet_type"] == "X20"         # 最老的被挤出
    tail = list(st.unhandled)[-50:]
    assert len(tail) == 50 and tail[-1]["packet_type"] == "X519"


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
    assert set(d) == {"reason", "turn", "friendly_turn", "my_turn",
                      "linked_effects", "players", "me", "opp"}
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
    st.apply_hints(ctrl={20: 2})
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
