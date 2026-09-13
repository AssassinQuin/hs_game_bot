"""留牌推理层与实时链路: MulliganAdvisor 装载/建议/先验热更新 + 富化 + 渲染。"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hsbot.carddb import CardDB
from hsbot.mulligan_ai import MulliganAdvisor, models_root_for
from hsbot.render import chain_line

NO_DB = CardDB("/nonexistent/cards.json")      # 降级: name=card_id, cost=None


def _mk_model(data_dir: Path, cards: dict, deck="奇迹德", lr=None, digest=None):
    """直接落一版最小模型产物(不经训练器)。"""
    root = models_root_for(data_dir, deck)
    vdir = root / "v001"
    vdir.mkdir(parents=True)
    (vdir / "stats.json").write_text(
        json.dumps({"deck_wr": 0.55, "cards": cards}, ensure_ascii=False),
        encoding="utf-8")
    (vdir / "meta.json").write_text("{}", encoding="utf-8")
    (vdir / "games_seen.json").write_text("{}", encoding="utf-8")
    if lr is not None:
        (vdir / "model.json").write_text(
            json.dumps(lr, ensure_ascii=False), encoding="utf-8")
    if digest is not None:
        (vdir / "games_digest.json").write_text(
            json.dumps(digest, ensure_ascii=False), encoding="utf-8")
    (root / "LATEST.json").write_text('{"version": "v001"}', encoding="utf-8")
    return root


def _cell(kw, kl, dw, dl):
    return {"keep": {"w": kw, "l": kl}, "drop": {"w": dw, "l": dl}}


def test_advise_none_without_model(tmp_path):
    adv = MulliganAdvisor(models_root_for(tmp_path / "data", "奇迹德"),
                          "奇迹德", NO_DB)
    assert adv.advise(["GOOD"], "PALADIN", True) is None


def test_advise_table_path_with_prior(tmp_path):
    data = tmp_path / "data"
    stats = {"GOOD": {"PALADIN|1": _cell(4, 0, 1, 1), "*|*": _cell(4, 0, 1, 1)},
             "BAD": {"PALADIN|1": _cell(0, 0, 0, 3), "*|*": _cell(0, 1, 0, 3)}}
    root = _mk_model(data, stats)
    (data / "mulligan_prior.yaml").write_text(
        "奇迹德:\n  cards:\n    BAD: {keep: -0.10}\n", encoding="utf-8")
    adv = MulliganAdvisor(root, "奇迹德", NO_DB,
                          prior_path=data / "mulligan_prior.yaml")
    r = adv.advise(["GOOD", "BAD"], "PALADIN", True)
    assert r is not None and r["keep"] == ["GOOD"] and r["drop"] == ["BAD"]
    assert r["per_card"]["BAD"]["label"] == "建议换"
    assert r["per_card"]["GOOD"]["ev"] == ""        # 无 digest → 无匹配证据
    # 对手职业未知 → 级联走全体格, 仍有结论
    r2 = adv.advise(["GOOD"], "UNKNOWN", False)
    assert r2["keep"] == ["GOOD"]


def test_advise_lr_gated_out_mentions_reference(tmp_path):
    stats = {"GOOD": {"*|*": _cell(4, 0, 1, 1)}}
    lr = {"vocab": ["GOOD"], "classes": ["PALADIN"], "pairs": [],
          "coef": [0.0, 0.0, 0.0, 0.0], "intercept": 0.0,
          "metrics": {"test_auc": 0.30}}
    root = _mk_model(tmp_path / "data", stats, lr=lr)
    r = MulliganAdvisor(root, "奇迹德", NO_DB).advise(["GOOD"], "PALADIN", False)
    assert "仅参考" in r["basis"]


def test_advise_matched_evidence_from_digest(tmp_path):
    stats = {"GOOD": {"PALADIN|0": _cell(1, 0, 0, 0), "*|*": _cell(1, 0, 0, 0)}}
    digest = [{"c": "PALADIN", "o": 0, "f": ["GOOD", "A", "B"], "k": ["GOOD"], "r": 1},
              {"c": "PALADIN", "o": 0, "f": ["GOOD", "A", "C"], "k": [], "r": 0},
              {"c": "PALADIN", "o": 0, "f": ["GOOD", "A", "D"], "k": ["GOOD"], "r": 1}]
    root = _mk_model(tmp_path / "data", stats, digest=digest)
    r = MulliganAdvisor(root, "奇迹德", NO_DB).advise(["GOOD", "A"], "PALADIN", False)
    assert "近似手牌3局" in r["per_card"]["GOOD"]["ev"]


def test_prior_hot_reload_on_mtime_change(tmp_path):
    data = tmp_path / "data"
    root = _mk_model(data, {"GOOD": {"*|*": _cell(2, 2, 2, 2)}})
    prior = data / "mulligan_prior.yaml"
    prior.write_text("奇迹德:\n  cards:\n    GOOD: {keep: 0.20}\n", encoding="utf-8")
    adv = MulliganAdvisor(root, "奇迹德", NO_DB)
    r1 = adv.advise(["GOOD"], "PALADIN", None)
    g1 = r1["per_card"]["GOOD"]["gain"]
    # 改先验(换成强负) → 无需重训即生效
    prior.write_text("奇迹德:\n  cards:\n    GOOD: {keep: -0.30}\n", encoding="utf-8")
    import os
    os.utime(prior, None)                      # 确保mtime变化(快速写入可能同秒)
    r2 = adv.advise(["GOOD"], "PALADIN", None)
    assert r2["per_card"]["GOOD"]["gain"] < g1


def test_enrich_only_friendly_and_only_with_advisor():
    from hsbot.analysis import EffectAnalyzer

    store = SimpleNamespace(friendly_key=1,
                            heroes_facts=lambda: {1: "HERO_06x", 2: "HERO_09x"})
    evt = {"kind": "mulligan_offer", "actor": 1, "msg": "起手可留: X",
           "offered": ["GOOD", "GAME_005"]}
    # 无建议器 → 原样通过
    assert EffectAnalyzer(NO_DB).enrich(dict(evt), store).get("advice") is None
    # 有建议器 → 富化 advice(幸运币已剔除, 后手标记)
    seen = {}

    class Stub:
        def advise(self, offered, cls, coin, explore=False, seed=None):
            seen.update(offered=offered, cls=cls, coin=coin)
            return {"keep": ["GOOD"], "drop": [], "per_card": {},
                    "opp_class": cls, "coin": coin, "basis": "测试"}

    out = EffectAnalyzer(NO_DB, mulligan=Stub()).enrich(dict(evt), store)
    assert out["advice"]["keep"] == ["GOOD"]
    assert seen == {"offered": ["GOOD"], "cls": "PRIEST", "coin": True}
    # 对手的留牌事件 → 不建议
    opp = dict(evt, actor=2)
    assert EffectAnalyzer(NO_DB, mulligan=Stub()).enrich(opp, store).get("advice") is None


def test_render_mulligan_offer_fallback_and_advice():
    evt = {"kind": "mulligan_offer", "msg": "起手可留: GOOD、BAD(硬币)"}
    assert "起手可留" in chain_line(evt, NO_DB)
    adv = {"opp_class": "PRIEST", "coin": True, "keep": ["GOOD"], "drop": ["BAD"],
           "per_card": {"GOOD": {"name": "好牌", "gain": 0.2},
                        "BAD": {"name": "坏牌", "gain": None}}}
    line = chain_line({"kind": "mulligan_offer", "advice": adv}, NO_DB)
    assert "【留牌建议·vs牧师·后手】留 好牌(+20.0%) │ 换 坏牌" in line
