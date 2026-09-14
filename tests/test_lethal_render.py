"""斩杀线渲染(render 第三行 + overlay 上区行): plan 机读事实 → "可斩: …" 措辞。

零变化铁律: plan=None / 非可斩时 stat_text 输出与现行两行版逐字节一致
(golden 硬编码断言, 防 措辞漂移)。结论词"可斩"归 render, plan 只带事实。
"""
import json

import pytest

from hearthstone.enums import CardType, GameTag, Zone

from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB

from .conftest import (mk_full, mk_heroes as _heroes, mk_store as _store,
                       mk_tag)

# 场景 golden(现行 HEAD 两行版逐字节): 敌 2血+0甲; 手中火球(6伤,法强0)
# → 斩杀 6 ≥ 2 → 行1 可斩; 通用模式(knowledge=None)库/组侧诚实降级 ?
_GOLDEN_TWO_LINES = (
    "敌 2(2血+0甲) │ 斩杀 6(手6+库?+场0,可斩) │ 法强 0\n"
    "回费 +0(手0+库?) │ 费 组?/库?/手4"
)
# analysis.lethal_plan 同构机读事实(契约 §2): 两个动作, 一可解析一不可解析
_PLAN = {"actions": [("CS2_029", 4), ("UNKNOWN_X", 0)],
         "total": 6, "face_det": 6, "face_exp": 0, "lethal": True,
         "enemy_total": 2, "board_atk": 0, "uncovered_n": 0}
_LINE3 = "可斩: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0 ≥ 2"
# overlay 分格面板所需的基础机读字段(与 test_overlay_smoke 同款)
_BASE_FIELDS = {"enemy_total": 2, "enemy_hp": 2, "enemy_armor": 0,
                "lethal": 6, "lethal_hand": 6, "lethal_deck": None,
                "lethal_board": 0, "can_kill": True, "spellpower": 0,
                "ramp": 0, "ramp_hand": 0, "ramp_deck": None,
                "cost_list": None, "cost_deck": None, "cost_hand": 4,
                "discount": {"cards": 0, "total": 0, "sources": []}}


def _scene(tmp_path):
    """最小局面: 双方英雄 + 敌 2 血甲 + 我手牌一张火球术(4费/6伤)。"""
    p = tmp_path / "cards.json"
    p.write_text(json.dumps([
        {"id": "CS2_029", "name": "火球术", "type": "SPELL", "cost": 4,
         "text": "造成$6点伤害。"},
    ], ensure_ascii=False), encoding="utf-8")
    db = CardDB(p)
    st, _ = _store()
    _heroes(st)
    st.apply(mk_tag(5, GameTag.DAMAGE, 28))    # 敌 30-28=2血+0甲 → 总 2
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.SPELL.value, ZONE_POSITION=1))
    return st, db, EffectAnalyzer(db)


def test_stat_text_golden_two_lines_unchanged(tmp_path):
    """基线回归: 不带 plan 的现行输出 = 硬编码 golden(改前后都必须过)。"""
    from hsbot.render import stat_fields, stat_text

    st, db, a = _scene(tmp_path)
    assert stat_text(stat_fields(st, knowledge=None, carddb=db, analyzer=a)) \
        == _GOLDEN_TWO_LINES


def test_stat_text_appends_lethal_line(tmp_path):
    """plan.lethal=True → 末尾追加第三行"可斩: …"; 名可解析/不可解析各一;
    f["plan"] 原样透传; top_summary 透传 plan。"""
    from hsbot.render import stat_fields, stat_text, top_summary

    st, db, a = _scene(tmp_path)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a, plan=_PLAN)
    assert f["plan"] is _PLAN                  # 原样透传(契约 §3)
    out = stat_text(f)
    assert out == f"{_GOLDEN_TWO_LINES}\n{_LINE3}"
    assert top_summary(st, knowledge=None, carddb=db, analyzer=a,
                       plan=_PLAN) == out      # 组合入口透传


def test_stat_text_lethal_false_no_third_line(tmp_path):
    """plan.lethal=False → 不追加第三行, 输出与两行版逐字节一致。"""
    from hsbot.render import stat_fields, stat_text

    st, db, a = _scene(tmp_path)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={**_PLAN, "lethal": False})
    assert stat_text(f) == _GOLDEN_TWO_LINES


def test_plan_line_carddb_name_missing_falls_back(tmp_path):
    """卡名缺失: 未收录 card_id 回退 card_id(真卡表语义);
    carddb.name 抛异常也不拖垮渲染(假/半残卡表)。"""
    from hsbot.render import stat_fields, stat_text

    st, db, a = _scene(tmp_path)
    out = stat_text(stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                                plan=_PLAN))
    assert "→UNKNOWN_X(0费)" in out            # 未收录 → card_id 兜底

    class _Boom:
        @staticmethod
        def name(cid):
            raise KeyError(cid)

    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a, plan=_PLAN)
    f["_carddb"] = _Boom()                     # 出名失败: 全部回退 card_id
    assert stat_text(f) == (f"{_GOLDEN_TWO_LINES}\n"
                            "可斩: CS2_029(4费)→UNKNOWN_X(0费) 伤6+场0 ≥ 2")


def test_plan_degenerate_empty_actions_no_crash(tmp_path):
    """退化输入: lethal=True 但 actions 空 → 不崩溃, 仍报伤害构成
    (动作序列以 (无动作) 占位, 缺失数值以 ? 诚实降级)。"""
    from hsbot.render import stat_fields, stat_text

    st, db, a = _scene(tmp_path)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={"actions": [], "lethal": True})
    out = stat_text(f)                         # 不抛异常
    assert out.startswith(_GOLDEN_TWO_LINES)
    assert "可斩: (无动作) 伤?+场? ≥ ?" in out


def test_plan_line_optimal_when_not_lethal(tmp_path):
    """非可斩也出线(输出语义两级契约): `最优: … 伤X+场Y vs 敌Z`,
    绝不写"可斩"二字(总伤未达敌血); 措辞与可斩行明确区分。"""
    from hsbot.render import plan_line, stat_fields

    st, db, a = _scene(tmp_path)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={**_PLAN, "lethal": False})
    line = plan_line(f)
    assert line == "最优: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0 vs 敌2"
    assert "可斩" not in line


def test_plan_line_optimal_expectation_annotation(tmp_path):
    """face_exp>0 → 追加 `+期望N` 注记; 期望分量绝不与确定伤合并
    (伤X 保持确定伤原值, 宁漏勿错口径)。"""
    from hsbot.render import plan_line, stat_fields

    st, db, a = _scene(tmp_path)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={**_PLAN, "lethal": False, "face_exp": 4})
    assert plan_line(f) == \
        "最优: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0+期望4 vs 敌2"


def test_plan_line_empty_line_degenerate_hidden(tmp_path):
    """空线退化: actions 空且 零确定伤 且 零期望 → 不出行(诚实: 无建议可给;
    契约口径不含 board_atk —— 即便场攻>0 也按无建议隐藏, 宁漏勿错)。"""
    from hsbot.render import plan_line, stat_fields

    st, db, a = _scene(tmp_path)
    zeros = {"actions": [], "total": 0, "face_det": 0, "face_exp": 0,
             "lethal": False, "enemy_total": 2, "board_atk": 0,
             "uncovered_n": 0}
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a, plan=zeros)
    assert plan_line(f) is None
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={**zeros, "board_atk": 3})
    assert plan_line(f) is None               # 契约: 三条件全零即隐藏


def test_plan_data_line_facts_only():
    """数据行(支撑数据, dim 小字): 剩费=mana_trace 末位(打完整线后剩余),
    未覆盖张数=uncovered_n; 机读事实驱动零编造 —— 缺 mana_trace(T1 未下发)/
    零未覆盖 → 该段省略; 两项全无 → None。"""
    from hsbot.render import plan_data_line

    assert plan_data_line({"plan": {"mana_trace": (5, 3, 0),
                                    "uncovered_n": 2}}) \
        == "剩费0 · 未覆盖2张"
    assert plan_data_line({"plan": {"uncovered_n": 1}}) == "未覆盖1张"
    assert plan_data_line({"plan": {"mana_trace": (5, 3)}}) == "剩费3"
    assert plan_data_line({"plan": {"mana_trace": (), "uncovered_n": 0}}) is None
    assert plan_data_line({"plan": {}}) is None      # 旧形态无 mana_trace 键
    assert plan_data_line({"plan": None}) is None


def test_plan_rows_composition_and_tone(tmp_path):
    """plan_rows(推荐区行组装): 首行=线行(可斩金/advice 色, 非可斩最优
    常规/stat 色), 次行=数据行(dim); 空线退化 → []; 数据行仅在有事实时出。"""
    from hsbot.render import plan_rows, stat_fields

    st, db, a = _scene(tmp_path)
    plan = {**_PLAN, "mana_trace": (5, 3), "uncovered_n": 1}
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a, plan=plan)
    assert plan_rows(f) == [
        ("可斩: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0 ≥ 2", "advice"),
        ("剩费3 · 未覆盖1张", "dim")]
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={**plan, "lethal": False})
    rows = plan_rows(f)
    assert rows[0] == ("最优: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0 vs 敌2",
                       "stat")
    assert rows[1] == ("剩费3 · 未覆盖1张", "dim")
    # 空线退化 / 无 plan → 空行集(诚实)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a,
                    plan={**_PLAN, "lethal": False, "actions": [],
                          "face_det": 0, "face_exp": 0})
    assert plan_rows(f) == []
    assert plan_rows({"plan": None, "_carddb": db}) == []
    # 无支撑数据事实 → 只有线行
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a, plan=_PLAN)
    assert plan_rows(f) == [
        ("可斩: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0 ≥ 2", "advice")]


def test_advice_panel_plan_line_row(tmp_path):
    """overlay 中上部推荐区(2026-09-14 三区布局): plan.lethal → 推荐区首行
    "可斩: …"(金/advice 色); 非可斩 → "最优: …"(常规/stat 色, 语义两级);
    无 plan(未解析/无手牌) → 不出新行不编造; 留牌行共存时线行排第一。
    行值驻留: plan=None 的后续更新(对手回合)不清线, 仅 reset 清空。"""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
        root.withdraw()                        # 测试不显示 UI
    except Exception as exc:                   # noqa: BLE001  无显示环境
        pytest.skip(f"无可用显示: {exc}")
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _AdvicePanel
        from hsbot.render import plan_rows, stat_fields

        st, db, a = _scene(tmp_path)
        f = stat_fields(st, knowledge=None, carddb=db, analyzer=a, plan=_PLAN)
        p = _AdvicePanel(tk, root, dict(_DEFAULT_COLORS), 10)
        p.set_plan(plan_rows({"plan": None}))          # 无 plan → 不编造
        assert all(l.cget("text") == "" for l in p._labels)
        p.set_plan(plan_rows(f))                       # 可斩 → 首行金色
        assert p._labels[0].cget("text") == _LINE3
        assert p._labels[0].cget("foreground") == _DEFAULT_COLORS["advice"]
        p.set_advice([("留 火球术(+20.0%)", "advice")])   # 与留牌行共存
        assert [l.cget("text") for l in p._labels][:2] == \
            [_LINE3, "留 火球术(+20.0%)"]
        opt = plan_rows({**f, "plan": {**_PLAN, "lethal": False}})
        p.set_plan(opt)                                # 非可斩: 最优行替换
        assert p._labels[0].cget("text") == \
            "最优: 火球术(4费)→UNKNOWN_X(0费) 伤6+场0 vs 敌2"
        assert p._labels[0].cget("foreground") == _DEFAULT_COLORS["stat"]
        p.set_plan(plan_rows({"plan": None}))          # 对手回合: 值驻留不清
        assert p._labels[0].cget("text").startswith("最优: ")
        p.reset()                                      # 仅局终清空
        assert all(l.cget("text") == "" for l in p._labels)
    finally:
        root.destroy()
