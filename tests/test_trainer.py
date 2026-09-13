"""trainer: 状态快照 / 素材构建 / 价值模型。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hsbot.carddb import CardDB
from trainer.material import build_material
from trainer.states import flatten, snapshot

NO_DB = CardDB("/nonexistent/cards.json")      # 降级: name=card_id, cost=None


def _snap_store():
    from .conftest import mk_create_game, mk_full, mk_heroes, mk_store
    st, _ = mk_store()
    st.note_friendly(1)
    mk_heroes(st)
    return st


def test_snapshot_needs_friendly():
    from .conftest import mk_create_game, mk_pm
    from hsbot.store import GameStore

    class _Tree:
        def __iter__(self):
            return iter(())

    st = GameStore(carddb=NO_DB, battletag="", tree=_Tree(),
                   player_manager=mk_pm())     # 未打留牌: 主客未定
    st.apply(mk_create_game())
    assert snapshot(st, NO_DB) is None


def test_snapshot_facts_and_flatten():
    from hearthstone.enums import CardType, Zone

    from .conftest import mk_full
    st = _snap_store()
    st.apply(mk_full(60, "EX1_166", ZONE=Zone.HAND.value, CONTROLLER=1))
    st.apply(mk_full(61, "CS2_042", ZONE=Zone.PLAY.value, CONTROLLER=1,
                     ATK=2, HEALTH=3, CARDTYPE=CardType.MINION.value, TAUNT=1))
    snap = snapshot(st, NO_DB)
    assert snap is not None
    assert snap["me"]["hand"][0]["cid"] == "EX1_166"
    assert snap["me"]["board"][0]["hp"] == 3
    assert snap["me"]["board"][0]["taunt"] is True
    assert snap["me"]["class"] is None         # 无卡表: 职业降级为 None
    vec, names = flatten(snap)
    assert len(vec) == len(names) and len(vec) > 10
    assert all(isinstance(v, float) for v in vec)
    assert vec == flatten(snap)[0]             # 同局面同向量(确定性)
    assert names.index("me_hp") < names.index("opp_hp")


def test_material_from_fixture(tmp_path):
    corpus = tmp_path / "corpus" / "奇迹德"
    corpus.mkdir(parents=True)
    log = ROOT / "tests" / "fixtures" / "mini_game.log"
    (corpus / "mini_game.power.log").write_text(
        log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    stats = build_material(tmp_path / "corpus", "奇迹德", tmp_path / "out",
                           NO_DB, "湫然#51704")
    lines = [json.loads(ln) for ln in (tmp_path / "out" / "material.jsonl")
             .read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert stats["rows"] == len(lines)
    for r in lines:
        assert r["result"] in (0, 1)
        assert set(r) >= {"snap", "actions", "src", "result"}
        assert r["snap"]["me"]["hp"] >= 0


def test_value_train_and_predict(tmp_path):
    from trainer.value import predict, train

    def snap(big):
        side = lambda hp: {"hp": hp, "armor": 0, "hand": [], "board": [],
                           "deck": 20, "secrets": 0, "mana": 4, "mana_cap": 4,
                           "overload": 0, "fatigue": 0, "spellpower": 0,
                           "class": None}
        return {"turn": 3, "my_turn": True, "me": side(30 if big else 10),
                "opp": side(20)}
    rows = [{"snap": snap(i % 2 == 0), "actions": [],
             "result": 1 if i % 2 == 0 else 0, "src": f"s{i}"}
            for i in range(40)]
    (tmp_path / "material.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    metrics = train(tmp_path, tmp_path)
    assert metrics["n_rows"] == 40
    assert metrics["test_auc"] and metrics["test_auc"] > 0.8   # 血量差可分
    p = predict(None, tmp_path, snap(True))
    assert 0.0 <= p <= 1.0 and p > 0.5                         # 大血量局面 → 高胜率


def test_group_folds_no_leakage():
    from trainer.backtest import group_folds
    groups = ["a"] * 4 + ["b"] * 4 + ["c"] * 4 + ["d"] * 4
    folds = group_folds(groups, n_splits=3, seed=7)
    assert len(folds) == 3
    seen_all = []
    for tr, va in folds:
        g_tr = {groups[i] for i in tr}
        g_va = {groups[i] for i in va}
        assert not (g_tr & g_va)                # 同组绝不跨训练/验证
        seen_all.extend(va)
    assert sorted(seen_all) == list(range(len(groups)))  # 每个样本恰验证一次
