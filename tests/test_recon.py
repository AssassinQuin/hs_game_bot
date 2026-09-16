"""预测-实测对账(spec §5): reconcile 纯函数 + AuditExporter 落盘。
fixture 三类分歧(伤害/回费/抽牌)+一致零输出(spec §8)。"""
import json

from hearthstone.enums import BlockType, CardType, GameTag, Zone

from hsbot.carddb import CardDB
from planner.pieces import Piece
from planner.simstate import initial_state

from .conftest import mk_block, mk_full, mk_heroes, mk_show, mk_store, mk_tag

_CARDS = [
    {"id": "TST_FIRE", "name": "测试火球", "type": "SPELL", "cost": 4,
     "text": "造成$6点伤害。"},
    {"id": "TST_RAMP", "name": "测试激活", "type": "SPELL", "cost": 0,
     "text": "在本回合中，获得一个 法力水晶。"},
    {"id": "TST_MIN", "name": "测试随从", "type": "MINION", "cost": 5},
]


def _db(tmp_path):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(_CARDS, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


_PIECES = {
    ("TST_FIRE", 4): Piece("TST_FIRE", 4, segments=(6,), spell_scaled=True,
                           is_spell=True),
    ("TST_RAMP", 0): Piece("TST_RAMP", 0, mana_gain=1, is_spell=True),
}
_EVT = {"card_id": "TST_FIRE", "cost_tag": 4, "eid": 10, "is_power": False}


def test_reconcile_consistent_returns_none(tmp_path):
    db = _db(tmp_path)
    base = initial_state(5, (("TST_FIRE", 4),), 0, enemy_total=30)
    from hsbot.recon import reconcile
    after = initial_state(1, (), 0, enemy_total=24)      # 5-4费; 敌 -6; 手空
    assert reconcile(base, _PIECES, _EVT, after, db) is None


def test_reconcile_damage_divergence(tmp_path):
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    base = initial_state(5, (("TST_FIRE", 4),), 0, enemy_total=30)
    after = initial_state(1, (), 0, enemy_total=25)      # 实测只打了 5
    rec = reconcile(base, _PIECES, _EVT, after, db)
    assert rec is not None
    assert rec["card_id"] == "TST_FIRE"
    d = {x["field"]: x for x in rec["diffs"]}
    assert d["face"] == {"field": "face", "predicted": 6, "actual": 5}
    assert rec["predicted"]["face"] == 6 and rec["actual"]["face"] == 5
    assert rec["piece"]["segments"] == (6,)
    assert rec["ir_source_hash"] and rec["ir_source_hash"] != "missing"
    assert {"ts", "game_id", "ir_source_hash"} <= set(rec)


def test_reconcile_mana_gain_divergence(tmp_path):
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    evt = {"card_id": "TST_RAMP", "cost_tag": 0, "eid": 11, "is_power": False}
    base = initial_state(5, (("TST_RAMP", 0),), 0)
    after = initial_state(5, (), 0)                      # 实测没回水晶
    rec = reconcile(base, _PIECES, evt, after, db)
    assert rec is not None
    d = {x["field"]: x for x in rec["diffs"]}
    assert d["mana"]["predicted"] == 6 and d["mana"]["actual"] == 5


def test_reconcile_draw_divergence_hand_fields(tmp_path):
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    # 引擎在场施法: 预测抽 1 张已知牌 MOON; 实测没抽到
    base = initial_state(5, (("TST_FIRE", 4),), 0, engines=1,
                         known_draws=(("MOON", 1),))
    after = initial_state(1, (), 0)
    rec = reconcile(base, _PIECES, _EVT, after, db)
    d = {x["field"]: x for x in rec["diffs"]}
    assert d["hand_n"]["predicted"] == 1 and d["hand_n"]["actual"] == 0
    assert d["hand_cards"]["predicted"] == ["MOON"] and d["hand_cards"]["actual"] == []


def test_reconcile_missing_card_in_base_returns_none(tmp_path):
    from hsbot.recon import reconcile
    base = initial_state(5, (("OTHER", 1),), 0)
    assert reconcile(base, _PIECES, _EVT, initial_state(5, (), 0),
                     _db(tmp_path)) is None


# ---------------- 审计 2026-09-17: 信号豁免(免噪声淹没判据) ----------------

def test_reconcile_face_skipped_when_target_not_enemy_hero(tmp_path):
    """高#1: 打随从/解场(目标非敌方英雄)的 face 差不是分歧。"""
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    base = initial_state(5, (("TST_FIRE", 4),), 0, enemy_total=30)
    after = initial_state(1, (), 0, enemy_total=30)      # 打随从: 敌方没掉血
    assert reconcile(base, _PIECES, _EVT, after, db,
                     face_comparable=False) is None


def test_reconcile_unknown_draw_downgrade(tmp_path):
    """中#2: 实测多出的已知池外牌 = 未知抽入手(预测只计数), 不记 hand 分歧。"""
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    base = initial_state(5, (("TST_FIRE", 4),), 0, engines=1,
                         known_draws=())
    after = initial_state(1, (("TST_RAMP", 0),), 0, engines=1)   # 实测抽到
    assert reconcile(base, _PIECES, _EVT, after, db,
                     face_comparable=False) is None


def test_reconcile_known_pool_extra_still_flags(tmp_path):
    """实测多出的牌含已知池内(不是未知抽)保守记真分歧: 实测 2 张 MOON,
    预测 1 张 —— 二轮审计后该用例语义=纯池内 extra 不豁免。"""
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    base = initial_state(5, (("TST_FIRE", 4),), 0, engines=1,
                         known_draws=(("MOON", 1),))
    after = initial_state(1, (("MOON", 1), ("MOON", 1)), 0, engines=1)
    rec = reconcile(base, _PIECES, _EVT, after, db, face_comparable=False)
    d = {x["field"]: x for x in rec["diffs"]}
    assert d["hand_n"]["predicted"] == 1 and d["hand_n"]["actual"] == 2


def test_reconcile_missing_predicted_card_still_flags(tmp_path):
    """二轮审计 高#1: 预测抽到已知 MOON 但实测抽到池外 ZZZ(已知抽牌通道
    错/弃牌效应) —— 预测侧有实测缺失的牌, 恒记真分歧, 不得被豁免吞掉。"""
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    base = initial_state(5, (("TST_FIRE", 4),), 0, engines=1,
                         known_draws=(("MOON", 1),))
    after = initial_state(1, (("ZZZ", 1),), 0, engines=1)   # MOON 没来, 来了 ZZZ
    rec = reconcile(base, _PIECES, _EVT, after, db, face_comparable=False)
    d = {x["field"]: x for x in rec["diffs"]}
    assert d["hand_cards"]["predicted"] == ["MOON"]
    assert d["hand_cards"]["actual"] == ["ZZZ"]


def test_reconcile_mana_compared_capped_at_ten(tmp_path):
    """低#4: 实测侧临时水晶可瞬时 >10(mana_now 不封顶), 对比双侧按 10 封顶。"""
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    evt = {"card_id": "TST_RAMP", "cost_tag": 0, "eid": 11, "is_power": False}
    base = initial_state(10, (("TST_RAMP", 0),), 0)
    after = initial_state(11, (), 0)                     # mana_now 实测 11
    assert reconcile(base, _PIECES, evt, after, db) is None


def test_reconcile_cost_none_prefers_carddb_cost_copy(tmp_path):
    """低#5: cost_tag=None 时取卡表基础费副本(同牌不同减费多副本防错位)。"""
    db = _db(tmp_path)
    from hsbot.recon import reconcile
    base = initial_state(5, (("TST_FIRE", 2), ("TST_FIRE", 4)), 0)   # 升序
    after = initial_state(1, (("TST_FIRE", 2),), 0)      # 打的是 4 费那张
    evt = {"card_id": "TST_FIRE", "cost_tag": None, "eid": 10}
    assert reconcile(base, _PIECES, evt, after, db) is None


# ---------------- AuditExporter 端到端(真 store + 真 PLAY 块) ----------------

def _playable_scene(tmp_path, *, fire_cost=4):
    from hsbot.analysis import EffectAnalyzer
    st, log = mk_store()
    mk_heroes(st)
    st.apply(mk_tag(2, GameTag.RESOURCES, 5))
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, 0))
    st.apply(mk_full(10, "TST_FIRE", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, COST=fire_cost))
    return st, log, EffectAnalyzer(_db(tmp_path))


def _run_play(st, *, used=4, enemy_dmg=6, target=0):
    b = mk_block(BlockType.PLAY, 10, target=target)
    st.apply(b, depth=0)
    st.apply(mk_show(10, "TST_FIRE", ZONE=Zone.GRAVEYARD.value), depth=1)
    st.apply(mk_tag(10, GameTag.ZONE, Zone.GRAVEYARD.value), depth=1)
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, used), depth=1)
    st.apply(mk_tag(5, GameTag.DAMAGE, enemy_dmg), depth=1)   # 敌方英雄 30-x
    b.end()
    st.settle()
    return b


def test_audit_exporter_consistent_game_writes_nothing(tmp_path):
    from hsbot.config import Config
    from hsbot.watcher import AuditExporter
    cfg = Config.load({"data_dir": str(tmp_path), "overlay_enabled": False,
                       "auto_training": False})
    st, log, a = _playable_scene(tmp_path)
    aud = AuditExporter(cfg)
    aud.capture_base(st, None, a, 10)                 # PLAY 块开始(前态)
    _run_play(st, used=4, enemy_dmg=6)
    evt = log.by_kind("play")[0]
    aud.on_play(evt, st, None, a)
    f = tmp_path / "logs" / "sim_divergence.jsonl"
    assert not f.exists() or f.read_text(encoding="utf-8").strip() == ""


def test_audit_exporter_damage_divergence_writes_jsonl(tmp_path):
    from hsbot.config import Config
    from hsbot.watcher import AuditExporter
    cfg = Config.load({"data_dir": str(tmp_path), "overlay_enabled": False,
                       "auto_training": False})
    st, log, a = _playable_scene(tmp_path)
    aud = AuditExporter(cfg)
    aud.capture_base(st, None, a, 10, target=5)       # 5=敌方英雄: face 可比
    _run_play(st, used=4, enemy_dmg=4, target=5)      # 实测只打 4(卡牌改版? )
    evt = log.by_kind("play")[0]
    evt["game_id"] = "deadbeef"
    aud.on_play(evt, st, None, a)
    f = tmp_path / "logs" / "sim_divergence.jsonl"
    rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    assert rec["game_id"] == "deadbeef" and rec["card_id"] == "TST_FIRE"
    assert {x["field"] for x in rec["diffs"]} == {"face"}
    # JSON round-trip: tuple 落盘后读回是 list(库 json.loads 不还原 tuple)
    assert rec["piece"]["segments"] == [6] and rec["piece"]["cost"] == 4


def test_audit_exporter_survives_missing_base_and_reset(tmp_path):
    from hsbot.config import Config
    from hsbot.watcher import AuditExporter
    cfg = Config.load({"data_dir": str(tmp_path), "overlay_enabled": False,
                       "auto_training": False})
    st, log, a = _playable_scene(tmp_path)
    aud = AuditExporter(cfg)
    _run_play(st)
    aud.on_play(log.by_kind("play")[0], st, None, a)  # 无基态: 静默
    aud.capture_base(st, None, a, 99)
    aud.reset()                                       # 新局: 基态作废
    assert not (tmp_path / "logs" / "sim_divergence.jsonl").exists()


# ---------------- CLI 汇总(spec §5 消费入口 v1) ----------------

def test_summarize_counts_by_card_and_field():
    from hsbot.recon import summarize
    recs = [
        {"card_id": "A", "diffs": [{"field": "face"}]},
        {"card_id": "A", "diffs": [{"field": "face"}, {"field": "mana"}]},
        {"card_id": "B", "diffs": [{"field": "mana"}]},
    ]
    s = summarize(recs)
    assert s["n"] == 3
    assert s["by_card"] == [("A", 2), ("B", 1)]
    assert s["by_field"] == {"face": 2, "mana": 2}


def test_recon_cli_main_reads_jsonl(tmp_path, capsys):
    from hsbot.recon import main
    f = tmp_path / "d.jsonl"
    f.write_text(json.dumps({"card_id": "A", "diffs": [{"field": "face"}]},
                            ensure_ascii=False) + "\n", encoding="utf-8")
    assert main([str(f)]) == 0
    out = capsys.readouterr().out
    assert "A" in out and "face" in out
    assert main([]) == 2                        # 用法错误
