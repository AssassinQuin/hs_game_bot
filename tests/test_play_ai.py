"""T2 出牌推理层: 候选枚举 / 特征差分 / 价值模型排序 / 开口门槛 / 降级与热更新,
以及 play_offer 责任链接线(store 时机 / enrich 富化 / render 措辞 / overlay 优先级)。

零变化铁律: play_ai 关闭或静默降级时整局回放输出逐字节一致。
"""
import json
import os
import pickle
from pathlib import Path
from types import SimpleNamespace

from hearthstone.enums import BlockType, CardType, GameTag, Zone

from hsbot.carddb import CardDB

from .conftest import (mk_block, mk_full, mk_heroes as _heroes,
                       mk_show, mk_store as _store, mk_tag)

NO_DB = CardDB("/nonexistent/cards.json")

# 测试卡表(语义卡牌, 无网络): 法术伤害牌/法强随从/抽牌法术/大随从/拍卖师
CARDS = [
    {"id": "MOON_1", "name": "月火术", "type": "SPELL", "cost": 1,
     "text": "造成$2点伤害。"},
    {"id": "SP_IMP", "name": "法强随从", "type": "MINION", "cost": 2,
     "attack": 1, "health": 3, "mechanics": ["TAUNT"],
     "text": "法术伤害+2。"},
    {"id": "DRAW_2", "name": "抽两张", "type": "SPELL", "cost": 2,
     "text": "从你的牌库中抽两张牌。"},
    {"id": "AUCTION", "name": "拍卖师", "type": "MINION", "cost": 5,
     "attack": 4, "health": 4,
     "text": "每当你施放一个法术，抽一张牌。"},
    {"id": "BIG_MIN", "name": "大随从", "type": "MINION", "cost": 6,
     "attack": 6, "health": 6},
]


def _db(tmp_path):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(CARDS, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


def _analyzer(tmp_path):
    from hsbot.analysis import EffectAnalyzer
    return EffectAnalyzer(_db(tmp_path))


def _scene(tmp_path, mana=5):
    """我方回合局面: 手牌 月火(1)/法强随从(2)/抽两张(2)/大随从(6),
    场上拍卖师(施法抽牌引擎), 牌库 5 张暗牌, 法力 5。"""
    from hsbot.analysis import EffectAnalyzer
    a = EffectAnalyzer(_db(tmp_path))
    st, _log = _store()
    _heroes(st)
    for i in range(50, 55):                       # 牌库 5 张暗牌
        st.apply(mk_full(i, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_full(20, "AUCTION", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1))
    for i, cid in [(10, "MOON_1"), (11, "SP_IMP"), (12, "DRAW_2"),
                   (13, "BIG_MIN")]:
        st.apply(mk_full(i, cid, ZONE=Zone.HAND.value, CONTROLLER=1,
                         ZONE_POSITION=i - 9))
    st.apply(mk_tag(2, GameTag.RESOURCES, mana))
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    return st, a


# ================= 候选枚举(深度 1 + 关键二连) =================

def test_candidates_payable_singles_and_combos(tmp_path):
    """候选 = 每张可支付手牌(费≤法力) + 关键二连"法强牌→法术";
    超费大随从不入候选; 抽牌量 = 自身抽牌 + 场上施法抽牌引擎(法术)。"""
    from hsbot.play_ai import candidate_actions

    st, a = _scene(tmp_path)
    cands = candidate_actions(st, a)
    singles = {c["actions"][0][0]: c for c in cands if len(c["actions"]) == 1}
    combos = [c for c in cands if len(c["actions"]) == 2]
    # 可支付单打(月火1/法强2/抽两张2); 6 费大随从 > 5 费法力: 不入
    assert set(singles) == {"MOON_1", "SP_IMP", "DRAW_2"}
    assert singles["MOON_1"]["cost"] == 1
    assert singles["SP_IMP"]["board_add"] == {
        "cid": "SP_IMP", "atk": 1, "hp": 3, "taunt": True}
    # 关键二连: 法强随从→两张法术(2+1 / 2+2 均可支付), 顺序=法强在前
    assert {(c["actions"][0][0], c["actions"][1][0]) for c in combos} == {
        ("SP_IMP", "MOON_1"), ("SP_IMP", "DRAW_2")}
    combo = next(c for c in combos if c["actions"][1][0] == "MOON_1")
    assert combo["cost"] == 3 and combo["spellpower"] == 2
    # 抽两张: 自身 2 + 拍卖师引擎 1 = 3; 拍卖师自己(cast_draw)的抽牌是触发句
    # 而非战吼, 不计自身抽牌
    assert singles["DRAW_2"]["draws"] == 3
    assert singles["AUCTION" if "AUCTION" in singles else "MOON_1"] is not None


def test_candidates_no_candidates_when_nothing_payable_or_opp_turn(tmp_path):
    from hsbot.play_ai import candidate_actions

    st, a = _scene(tmp_path, mana=0)
    assert candidate_actions(st, a) == []        # 0 费法力: 无可支付
    st2, a2 = _scene(tmp_path)
    st2.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 0))
    st2.apply(mk_tag(3, GameTag.CURRENT_PLAYER, 1))
    assert candidate_actions(st2, a2) == []      # 对手回合: 不出候选


def test_candidates_real_cost_prefers_cost_tag(tmp_path):
    """可支付费 = 手牌在身实际费(COST 标签优先, 减费在身), 缺卡表回退 0。"""
    from hsbot.play_ai import candidate_actions

    st, a = _scene(tmp_path, mana=2)
    st.apply(mk_full(14, "BIG_MIN", ZONE=Zone.HAND.value, CONTROLLER=1,
                     ZONE_POSITION=5))
    st.apply(mk_tag(14, GameTag.COST, 0))        # 6 费大随从被减到 0
    cands = candidate_actions(st, a)
    assert any(c["actions"][0][0] == "BIG_MIN" and c["actions"][0][1] == 0
               for c in cands)


# ================= 特征差分向量(trainer.states.flatten 布局) =================

def _feat(snap):
    from trainer.states import flatten
    vec, names = flatten(snap)
    return dict(zip(names, vec))


def test_apply_candidate_feature_deltas(tmp_path):
    """特征差分 = trainer/states.flatten 向量上的: 费−、手牌数−1、场面攻血+、
    法强+、牌库−1 并入手牌期望(匿名牌不计费)。布局单点复用训练侧 flatten。"""
    from hsbot.play_ai import apply_candidate, candidate_actions
    from trainer.states import snapshot as make_snapshot

    st, a = _scene(tmp_path)
    snap = make_snapshot(st, a.carddb)
    base = _feat(snap)
    cands = {c["actions"][0][0] if len(c["actions"]) == 1
             else (c["actions"][0][0], c["actions"][1][0]): c
             for c in candidate_actions(st, a)}

    d = _feat(apply_candidate(snap, cands["MOON_1"]))
    assert d["mana"] == base["mana"] - 1
    assert d["me_deck"] == base["me_deck"] - 1            # 引擎抽: 牌库 −1
    assert d["me_hand_0"] == base["me_hand_0"]            # −1 打出 +1 期望抽
    assert d["me_hand_1"] == base["me_hand_1"] - 1        # 月火费离手; 匿名抽不计费
    assert d["me_board_1"] == base["me_board_1"]          # 法术不动场面

    d = _feat(apply_candidate(snap, cands["SP_IMP"]))
    assert d["me_hand_0"] == base["me_hand_0"] - 1
    assert d["me_board_0"] == base["me_board_0"] + 1      # 随从数
    assert d["me_board_1"] == base["me_board_1"] + 1      # 总攻 +1
    assert d["me_board_2"] == base["me_board_2"] + 3      # 总血 +3
    assert d["me_board_3"] == base["me_board_3"] + 1      # 嘲讽 +1
    assert d["mana"] == base["mana"] - 2

    d = _feat(apply_candidate(snap, cands[("SP_IMP", "MOON_1")]))
    assert d["me_spellpower"] == base["me_spellpower"] + 2   # 法强 +
    assert d["me_hand_0"] == base["me_hand_0"] - 1           # −2 打出 +1 期望抽
    assert d["me_deck"] == base["me_deck"] - 1
    assert d["mana"] == base["mana"] - 3

    d = _feat(apply_candidate(snap, cands["DRAW_2"]))
    assert d["me_deck"] == base["me_deck"] - 3            # 牌库 −(2+引擎1)
    assert d["me_hand_0"] == base["me_hand_0"] + 2        # −1 打出 +3 期望抽
    assert d["me_hand_1"] == base["me_hand_1"] - 2        # 抽两张费离手; 匿名抽不计费


def test_apply_candidate_draw_capped_by_deck(tmp_path):
    """牌库余量不足时抽牌期望按余牌封顶(不产生负牌库)。"""
    from hsbot.play_ai import apply_candidate, candidate_actions
    from trainer.states import snapshot as make_snapshot

    st, a = _scene(tmp_path)
    for i in range(50, 55):
        st.apply(mk_tag(i, GameTag.ZONE, Zone.SETASIDE.value))  # 清空牌库
    snap = make_snapshot(st, a.carddb)
    cand = next(c for c in candidate_actions(st, a)
                if c["actions"][0][0] == "DRAW_2")
    d = _feat(apply_candidate(snap, cand))
    assert d["me_deck"] == 0
    assert d["me_hand_0"] == _feat(snap)["me_hand_0"] - 1   # 抽不到: 无期望入手


# ================= PlayAdvisor: 打分排序 / 门槛 / 降级 / 热更新 =================

def _hand_count_idx() -> int:
    """flatten 布局里"我手牌数"特征下标(布局单点 = trainer.states)。"""
    from trainer.states import flatten
    side = {"hp": 0, "armor": 0, "hand": [], "board": [], "deck": 0,
            "secrets": 0, "mana": 0, "mana_cap": 0, "overload": 0,
            "fatigue": 0, "spellpower": 0, "class": None}
    snap = {"turn": 2, "my_turn": True, "me": dict(side), "opp": dict(side)}
    _, names = flatten(snap)
    return names.index("me_hand_0")


class _StubModel:
    """可 pickle 的价值模型桩: 对"我手牌数"特征线性打分(手牌越少 P(胜) 越高),
    使"多出牌"的候选天然靠前 —— 排序断言不依赖真实模型。"""

    def __init__(self, idx=None, w=-0.5, b=0.0):
        self.idx = _hand_count_idx() if idx is None else idx
        self.w, self.b = w, b

    def predict_proba(self, X):
        import math
        out = []
        for v in X:
            z = max(-30.0, min(30.0, self.b + self.w * float(v[self.idx])))
            p = 1.0 / (1.0 + math.exp(-z))
            out.append([1.0 - p, p])
        return out


def _mk_play_model(tmp_path, n_games=300, model=None, version="v001",
                   deck="奇迹德"):
    """直接落一版最小出牌模型产物(不经训练器): value.pkl + value_meta.json +
    LATEST.json —— 与 trainer 产物形态同构。"""
    from hsbot.play_ai import models_root_for
    root = models_root_for(tmp_path, deck)
    vdir = root / version
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "value.pkl").write_bytes(pickle.dumps(
        {"model": model or _StubModel(), "names": []}))
    (vdir / "value_meta.json").write_text(
        json.dumps({"n_games": n_games}), encoding="utf-8")
    (root / "LATEST.json").write_text(json.dumps({"version": version}),
                                      encoding="utf-8")
    return root


def test_advise_ranks_candidates_by_model(tmp_path):
    from hsbot.play_ai import PlayAdvisor

    st, a = _scene(tmp_path)
    st.apply(mk_tag(20, GameTag.ZONE, Zone.SETASIDE.value))   # 摘掉拍卖师引擎:
    # 引擎抽会把手牌数补回(−1 打出 +1 期望), 桩模型只看手牌数 → 无引擎场面
    # 排序语义最清晰: 手牌降得最多的候选 P 最高。
    root = _mk_play_model(tmp_path)
    r = PlayAdvisor(root, a.carddb).advise(st, a)
    assert r["kind"] == "play_offer" and r["version"] == "v001"
    assert r["n_games"] == 300 and r["baseline"] is not None
    cs = r["candidates"]
    assert 1 < len(cs) <= 3                          # 候选 5 个 → 封顶 top3
    ps = [c["p"] for c in cs]
    assert ps == sorted(ps, reverse=True)            # 按 P(胜) 降序
    assert all(abs(c["delta_p"] - (c["p"] - r["baseline"])) < 1e-12
               for c in cs)
    # 桩模型=手牌越少越好: 二连(手牌−2)居首; 单打平分取更省(月火1费 < 法强2费)
    assert cs[0]["actions"] == [("SP_IMP", 2), ("MOON_1", 1)]
    assert cs[0]["names"] == ["法强随从", "月火术"]   # 机读名(卡表缺 → card_id)
    assert cs[1]["names"] == ["月火术"] and cs[1]["cost"] == 1


def test_gate_corpus_below_300_stays_silent(tmp_path):
    """开口门槛: 语料 <300 局不出排序(83 局 = 当前 data/training 实际局数,
    实跑即静默); 达线自动开口, 无需配置。"""
    from hsbot.play_ai import PlayAdvisor

    st, a = _scene(tmp_path)
    root = _mk_play_model(tmp_path, n_games=83)
    assert PlayAdvisor(root, a.carddb).advise(st, a) is None
    root = _mk_play_model(tmp_path, n_games=299, version="v002")
    assert PlayAdvisor(root, a.carddb).advise(st, a) is None
    root = _mk_play_model(tmp_path, n_games=300, version="v003")
    assert PlayAdvisor(root, a.carddb).advise(st, a) is not None
    # 产物缺 n_games 键(旧形态) → 按 0 计, 诚实静默
    root = _mk_play_model(tmp_path, n_games=None, version="v004")
    assert PlayAdvisor(root, a.carddb).advise(st, a) is None


def test_advise_degrades_silently_without_model_or_on_bad_artifact(tmp_path):
    """三路降级之无模型/坏产物: advise 恒 None(渲染层不出行, 整环静默)。"""
    from hsbot.play_ai import PlayAdvisor, models_root_for

    st, a = _scene(tmp_path)
    assert PlayAdvisor(models_root_for(tmp_path, "奇迹德"), a.carddb) \
        .advise(st, a) is None                       # 无产物
    root = _mk_play_model(tmp_path)
    (root / "v001" / "value.pkl").write_bytes(b"junk-not-pickle")
    assert PlayAdvisor(root, a.carddb).advise(st, a) is None   # 坏 pickle


def test_advise_silent_when_not_my_turn(tmp_path):
    """对手回合静默(store 只在我方时机发 play_offer, advise 再自守一层)。"""
    from hsbot.play_ai import PlayAdvisor

    st, a = _scene(tmp_path)
    root = _mk_play_model(tmp_path)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 0))
    st.apply(mk_tag(3, GameTag.CURRENT_PLAYER, 1))
    assert PlayAdvisor(root, a.carddb).advise(st, a) is None


def test_read_latest_hot_reload_on_mtime(tmp_path):
    """mtime 热更新: 训练器产出新版本后无需重启自动切换(打分与门槛元数据
    同走一套刷新); LATEST 指回达标版本即重新开口。"""
    from hsbot.play_ai import PlayAdvisor

    st, a = _scene(tmp_path)
    root = _mk_play_model(tmp_path, n_games=300, version="v001")
    adv = PlayAdvisor(root, a.carddb)
    assert adv.advise(st, a)["version"] == "v001"
    _mk_play_model(tmp_path, n_games=83, version="v002")
    os.utime(root / "LATEST.json", None)
    assert adv.advise(st, a) is None                 # 新版未达门槛: 静默
    _mk_play_model(tmp_path, n_games=300, version="v003")
    os.utime(root / "LATEST.json", None)
    r = adv.advise(st, a)
    assert r["version"] == "v003" and r["candidates"]


# ================= 责任链: store 发 play_offer 的时机 =================

def test_store_emits_play_offer_on_friendly_turn_start_only():
    """我方 turn_start → play_offer(主建议时机, 在 turn_start 之后);
    对手回合静默。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "MOON_1", ZONE=Zone.HAND.value, CONTROLLER=1, COST=1))
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    offs = log.by_kind("play_offer")
    assert len(offs) == 1 and offs[0]["actor"] == 1
    kinds = log.kinds()
    assert kinds.index("turn_start") < kinds.index("play_offer")
    # 对手回合开始: 不发
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 0))
    st.apply(mk_tag(3, GameTag.CURRENT_PLAYER, 1))
    assert log.kinds().count("play_offer") == 1


def test_store_reemits_play_offer_after_friendly_play_batch_tail():
    """每次我方打出后, 块尾(抽牌/回费事件到齐)重发 play_offer ——
    顺序 打出 → 块内抽牌 → play_offer; 对手打出不触发。"""
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(10, "DRAW_2", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, COST=2))
    st.apply(mk_full(83, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    assert log.kinds().count("play_offer") == 1        # turn_start 那次
    b = mk_block(BlockType.PLAY, 10)
    st.apply(b, depth=0)
    st.apply(mk_show(83, "MOON_1", ZONE=Zone.HAND.value), depth=1)  # 块内抽牌
    b.end()
    st.settle()
    kinds = log.kinds()
    assert kinds.count("play_offer") == 2
    assert kinds.index("play", kinds.index("play_offer") + 1) \
        < kinds.index("draw", kinds.index("play_offer") + 1) \
        < kinds.index("play_offer", kinds.index("play_offer") + 1)
    # 对手打出: 不重发
    st.apply(mk_full(20, "BIG_MIN", ZONE=Zone.HAND.value, CONTROLLER=2, COST=6))
    ob = mk_block(BlockType.PLAY, 20)
    st.apply(ob, depth=0)
    ob.end()
    st.settle()
    assert log.kinds().count("play_offer") == 2


# ================= 责任链: analysis.enrich 富化(同 mulligan 模式) =================

class _StubAdvisor:
    def __init__(self, result="x"):
        self.calls = []
        self.result = result

    def advise(self, st, analyzer):
        self.calls.append(st)
        return {"kind": "play_offer", "candidates": [self.result]}


def test_enrich_attaches_play_advice_only_for_friendly_live():
    from hsbot.analysis import EffectAnalyzer

    store = SimpleNamespace(friendly_key=1, is_my_turn=lambda: True)
    evt = {"kind": "play_offer", "actor": 1}
    # 无建议器 → 事件原样通过(无模型/开关关=整环静默的接缝)
    assert EffectAnalyzer(NO_DB).enrich(dict(evt), store).get("advice") is None
    # 我方 + live → 富化 advice(只富化不改事件事实)
    stub = _StubAdvisor()
    out = EffectAnalyzer(NO_DB, play=stub).enrich(dict(evt), store)
    assert out["advice"]["candidates"] == ["x"] and out["actor"] == 1
    assert len(stub.calls) == 1
    # 对手的事件 / 历史追平(live=False) → 不建议
    opp = dict(evt, actor=2)
    assert EffectAnalyzer(NO_DB, play=_StubAdvisor()).enrich(opp, store) \
        .get("advice") is None
    assert EffectAnalyzer(NO_DB, play=_StubAdvisor()) \
        .enrich(dict(evt), store, live=False).get("advice") is None


# ================= 责任链: render 措辞(结论词归 render) =================

_ADV = {"kind": "play_offer", "version": "v003", "n_games": 312, "turn": 5,
        "baseline": 0.5,
        "candidates": [
            {"actions": [("SP_IMP", 2), ("MOON_1", 1)], "cost": 3,
             "names": ["法强随从", "月火术"], "p": 0.523, "delta_p": 0.023},
            {"actions": [("MOON_1", 1)], "cost": 1,
             "names": ["月火术"], "p": 0.508, "delta_p": 0.008},
        ]}


def test_render_play_offer_line_wording():
    """出牌建议行(docs §2 统计最优级措辞): 推荐: 名A(ΔP +x.x%) > 名B > 不动;
    二连名按出牌序用 → 相连; ΔP 相对"不动"基线。"""
    from hsbot.render import chain_line

    line = chain_line({"kind": "play_offer", "advice": _ADV, "actor": 1,
                       "friendly": 1, "turn": 5}, NO_DB)
    assert "推荐: 法强随从→月火术(ΔP +2.3%) > 月火术(ΔP +0.8%) > 不动" in line


def test_render_play_offer_no_advice_is_dropped():
    """无建议(无模型/门槛未达/无候选)→ 渲染层判弃不出行: 事件事实已入
    rich_events, 显示整环静默 —— 实跑语料 83 局时输出零变化的关键。"""
    from hsbot.render import chain_line

    assert chain_line({"kind": "play_offer", "actor": 1, "friendly": 1,
                       "turn": 5}, NO_DB) is None
    assert chain_line({"kind": "play_offer", "actor": 1, "friendly": 1,
                       "turn": 5, "advice": None}, NO_DB) is None


def test_play_offer_rows_for_overlay():
    """推荐区行: 主行(advice 色, 与链路行同源) + 证据行(dim, 语料局数+版本);
    缺 n_games → 只出主行; 无建议 → []。"""
    from hsbot.render import play_offer_rows

    rows = play_offer_rows(_ADV)
    assert rows[0][0].startswith("推荐: 法强随从→月火术") and rows[0][1] == "advice"
    assert rows[1] == ("语料312局 · 依据 价值模型 v003", "dim")
    bare = {k: v for k, v in _ADV.items() if k != "n_games"}
    assert len(play_offer_rows(bare)) == 1
    assert play_offer_rows(None) == []
    assert play_offer_rows({"kind": "play_offer", "candidates": []}) == []


def test_play_offer_rows_negative_delta_wording():
    """负 ΔP 照实显示(负号), 绝不粉饰 —— 结论由用户自取。"""
    from hsbot.render import play_offer_text

    adv = {"kind": "play_offer", "baseline": 0.5, "candidates": [
        {"actions": [("MOON_1", 1)], "cost": 1, "names": ["月火术"],
         "p": 0.48, "delta_p": -0.02}]}
    assert "月火术(ΔP -2.0%) > 不动" in play_offer_text(adv)


# ================= 责任链: watcher 路由 + e2e 时机对齐 + 零变化钉子 =================

import queue as _queue

import pytest

from hsbot.config import Config  # noqa: E402
from hsbot.watcher import Watcher  # noqa: E402

FIXTURE_PLAY = Path(__file__).parent / "fixtures" / "play_offer.log"


def _replay(data_dir, extra=None, fixture=FIXTURE_PLAY):
    """整局回放(顶部子目录同名, 保证 game_id hash 一致, 供逐字节对比)。"""
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(data_dir), **(extra or {})})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    msgs: list = []
    w = Watcher(cfg, carddb, out=msgs.append)
    w.run_replay(fixture)
    return cfg, w, msgs


def _stage(tmp_path, name):
    """把 fixture 复制到 <tmp>/<name>/replay/ 下(父目录同名 → hash 一致)。"""
    d = tmp_path / name / "replay"
    d.mkdir(parents=True, exist_ok=True)
    (d / FIXTURE_PLAY.name).write_text(FIXTURE_PLAY.read_text(encoding="utf-8"),
                                       encoding="utf-8")
    return d


def test_watcher_routes_play_offer_as_advice_with_data(tmp_path):
    """watcher 层: play_offer 走 KIND_ADVICE 金色通道, Msg.data = 建议机读
    字段(悬浮窗推荐区从机读字段渲染); 真实模型链(桩产物)出建议。"""
    from hsbot.overlay import KIND_ADVICE, KIND_CHAIN

    staged = _stage(tmp_path, "solo")
    _mk_play_model(staged, n_games=300)            # 门槛达标 → 开口
    _cfg, _w, msgs = _replay(staged)
    advice = [m for m in msgs if m.kind == KIND_ADVICE and m.data
              and m.data.get("kind") == "play_offer"]
    assert advice, "play_offer 未走 KIND_ADVICE 通道"
    assert advice[0].data["candidates"], "advice Msg 未携带候选机读字段"
    assert all(m.kind != KIND_CHAIN or "推荐" not in m.ui for m in msgs)


def test_watcher_play_offer_aligned_with_real_action_moment(tmp_path):
    """回放切片验收(docs §1-T2): 建议行与真实操作时刻对齐 —— 我方回合标题
    之后、实际打出之前出主建议; 打出后(手牌空, 无候选)不再出。"""
    from hsbot.overlay import KIND_ADVICE

    staged = _stage(tmp_path, "solo")
    _mk_play_model(staged, n_games=300)
    _cfg, _w, msgs = _replay(staged)
    ui = [m.ui for m in msgs]

    def _adv_indexes():
        return [i for i, m in enumerate(msgs)
                if m.kind == KIND_ADVICE and m.data
                and m.data.get("kind") == "play_offer"]

    title = next(i for i, t in enumerate(ui) if "我的第1回合开始" in t)
    adv_i = _adv_indexes()[0]
    play_i = next(i for i, t in enumerate(ui) if "打出 CS2_029" in t)
    assert title < adv_i < play_i
    assert len(_adv_indexes()) == 1


def test_play_ai_switch_off_replay_byte_identical(tmp_path):
    """零变化铁律(整局回放级): play_ai=false 时即便达标模型在位, 整局输出与
    无模型基线逐字节一致; 同模型开开关 → 出"推荐"行(证明钉子非空转)。"""
    msgs_base = _replay(_stage(tmp_path, "base"))            # 无模型基线
    assert not any("推荐" in m.ui for m in msgs_base[2])
    on_dir = _stage(tmp_path, "on")
    _mk_play_model(on_dir, n_games=300)
    _cfg, _w, msgs_off = _replay(on_dir, {"play_ai": False})  # 有模型, 关
    assert "\n".join(m.full for m in msgs_base[2]) == \
        "\n".join(m.full for m in msgs_off)
    _cfg2, _w2, msgs_on = _replay(on_dir)                    # 同模型, 开
    assert "\n".join(m.full for m in msgs_on) != \
        "\n".join(m.full for m in msgs_off)
    assert sum(1 for m in msgs_on if "推荐" in m.ui) == 1
    assert any("(ΔP " in m.ui for m in msgs_on)


def test_msg_kind_for_play_offer_friendly_only():
    """金色 advice 语义专属"我方建议"(与留牌同款); 对手事件走普通链路色。"""
    from hsbot.overlay import KIND_ADVICE, KIND_CHAIN
    from hsbot.watcher import _msg_kind_for

    assert _msg_kind_for({"kind": "play_offer", "actor": 1, "friendly": 1}) \
        == KIND_ADVICE
    assert _msg_kind_for({"kind": "play_offer", "actor": 2, "friendly": 1}) \
        == KIND_CHAIN
    assert _msg_kind_for({"kind": "play", "actor": 1, "friendly": 1}) \
        == KIND_CHAIN


def test_config_play_ai_switch_wires_watcher(tmp_path):
    """config play_ai(默认开): false → watcher 不建建议器, analyzer 无 play
    环(整条环静默的单一入口); 开 → 建议器就位。"""
    carddb = CardDB("/nonexistent/cards.json")
    cfg_on = Config.load({"data_dir": str(tmp_path)})
    assert cfg_on.play_ai is True and "play_ai" in Config._BOOL
    w_on = Watcher(cfg_on, carddb, out=lambda m: None)
    assert w_on.play_ai is not None and w_on.analyzer.play is w_on.play_ai
    cfg_off = Config.load({"data_dir": str(tmp_path), "play_ai": False})
    w_off = Watcher(cfg_off, carddb, out=lambda m: None)
    assert w_off.play_ai is None and w_off.analyzer.play is None


# ================= 责任链: overlay 推荐区(优先级定版) =================

tk = None
try:
    import tkinter as _tk
    tk = _tk
except Exception:  # noqa: BLE001  无显示环境
    tk = None


@pytest.fixture()
def ensure_display():
    """探测显示可用性; 不置顶不抢前台(与 test_overlay_layout 同款约定)。"""
    if tk is None:
        pytest.skip("无 tkinter")
    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无可用显示: {exc}")


def _panel():
    if tk is None:
        import pytest
        pytest.skip("无 tkinter")
    try:
        root = tk.Tk()
        root.withdraw()                        # 测试不显示 UI, 不抢前台
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无可用显示: {exc}")
    from hsbot.overlay import _AdvicePanel, _DEFAULT_COLORS
    return root, _AdvicePanel(tk, root, dict(_DEFAULT_COLORS), 10)


def test_advice_panel_play_offer_row_and_priority():
    """推荐区优先级定版(四源三行): 可斩/最优线行 > play_offer 主行 > 留牌行
    > 数据行。依据写 _AdvicePanel docstring; 此处钉行为。"""
    root, p = _panel()
    try:
        p.set_plan([("最优: 火球术 伤6 vs 敌22", "stat"), ("剩费3", "dim")])
        p.set_play([("推荐: 月火术(ΔP +1.2%) > 不动", "advice"),
                    ("语料312局 · 依据 价值模型 v003", "dim")])
        p.set_advice([("留 好牌", "advice")])
        texts = [l.cget("text") for l in p._labels]
        assert texts == ["最优: 火球术 伤6 vs 敌22",
                         "推荐: 月火术(ΔP +1.2%) > 不动",
                         "留 好牌"]                    # 数据行让位(三行封顶)
        assert p._labels[1].cget("foreground") == \
            dict(p._colors)["advice"]
        # 无线行的新面板: play 主行 + 留牌行 + play 证据行(dim 数据层恒殿后)
        root2, p2 = _panel()
        p2.set_play([("推荐: 月火术(ΔP +1.2%) > 不动", "advice"),
                     ("语料312局 · 依据 价值模型 v003", "dim")])
        p2.set_advice([("留 好牌", "advice")])
        assert [l.cget("text") for l in p2._labels] == \
            ["推荐: 月火术(ΔP +1.2%) > 不动", "留 好牌",
             "语料312局 · 依据 价值模型 v003"]
        root2.destroy()
        # 值驻留: play 空更新不清旧值(与线行同款常驻语义); 线行同理(set_plan([]))
        p.set_play([])
        assert p._labels[1].cget("text").startswith("推荐: ")
        p.set_plan([])
        assert p._labels[0].cget("text").startswith("最优: ")
        p.reset()
        assert all(l.cget("text") == "" for l in p._labels)
    finally:
        root.destroy()


def test_overlay_dispatches_play_offer_data_to_play_row(ensure_display,
                                                        monkeypatch,
                                                        tmp_path):
    """整窗分流: KIND_ADVICE 按 data.kind 分流 —— play_offer dict 进 play 行,
    留牌 dict 进留牌行; 互不串行; 局终 reset 全清。"""
    from hsbot.config import Config
    from hsbot.overlay import OverlayWindow

    q = _queue.Queue()
    q.put(("advice", "【留牌建议】留 X", "advice",
           {"opp_class": "PRIEST", "coin": False, "keep": ["X"],
            "drop": [], "per_card": {}}))
    q.put(("advice", "推荐: 月火术(ΔP +1.2%) > 不动", "advice", dict(_ADV)))

    cfg = Config.load({"overlay_enabled": True, "overlay_topmost": False,
                       "data_dir": str(tmp_path)})
    orig = tk.Tk.mainloop
    holder: dict = {}

    def mainloop_with_close(self, n=0):
        self.withdraw()

        def _close():
            win = holder.get("win")
            if win is not None:
                holder["labels"] = [l.cget("text") for l in win._advice._labels]
            self.destroy()

        self.after(800, _close)
        orig(self, n)

    monkeypatch.setattr(tk.Tk, "mainloop", mainloop_with_close)
    win = OverlayWindow(cfg, q)
    holder["win"] = win
    win.run()
    labels = holder["labels"]
    assert labels[0].startswith("推荐: 法强随从→月火术"), labels  # play 分流
    assert labels[1].startswith("【留牌建议"), labels             # 留牌分流
    assert labels[2] == "语料312局 · 依据 价值模型 v003"          # 证据行殿后
