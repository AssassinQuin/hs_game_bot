# 可斩伤害口径修正 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 信息区伤害格从「理论上限粗估」切换为「法力可行链（plan.total）」口径——can_kill 只由 plan 判定、粗估不再误报可斩，可斩时悬浮窗数值转亮橙新色键。

**Architecture:** 纯消费侧改动：`render.stat_fields`（数据口径）→ `render.stat_text`（双口径措辞）→ `overlay._StatPanel`（渲染+新色键）。`analysis.lethal_plan`/`planner` 零改动。spec: `docs/superpowers/specs/2026-09-18-lethal-mana-feasible-design.md`。

**Tech Stack:** Python 3.11 / pytest / tkinter（布局测试 importorskip，无显示环境自动 skip）

## Global Constraints

- planner / analysis / assemble_snapshot 零改动（spec §6 非目标）
- `stat_fields` 签名不变（`watcher._emit_stat` 调用点零改动）
- 宁漏勿错：理论口径（`lethal_est=True`）下 `can_kill` 恒 `False`
- 措辞归 render；overlay 只读机读字段，不从文本反推
- 新字段名定死：`lethal_est`（bool，恒存在）；新色键名定死：`lethal`
- 双口径措辞定死（golden 钉死，防漂移）：
  - 法力可行链：`斩杀 {total}(场{board}+线{hand}{kill_mark})`
  - 理论粗估：`斩杀 理论{lethal}(手{hand}+库{q(deck)}+场{board}{kill_mark})`
- 提交信息风格：中文 + type 前缀（feat:/fix:/test:）
- 测试基线 376 passed（2026-09-17）；完成后全绿且数量只增不减

---

### Task 1: render 层口径切换（stat_fields 数据 + stat_text 措辞）

**Files:**
- Modify: `hsbot/render.py`（stat_fields ~322-456、stat_text ~544-571）
- Test: `tests/test_render_knowledge.py`（test_top_summary_two_lines ~213-282）
- Test: `tests/test_lethal_render.py`（模块头 + golden 区 + 5 个测试）

**Interfaces:**
- Consumes: `plan` dict（契约 §2，键 `total/board_atk/lethal`，由 `analysis.lethal_plan` 产出；测试自建同构 dict）
- Produces: `stat_fields` 返回 dict 新增 `"lethal_est": bool`（恒存在）；`lethal/lethal_hand/lethal_board/can_kill` 语义变更（plan 在场→法力可行链：`lethal=plan.total`、`lethal_board=plan.board_atk`、`lethal_hand=total−board`、`can_kill=plan.lethal`；plan 缺席/退化→粗估原式 + `can_kill=False`）。Task 2 的 overlay 依赖 `lethal_est` 与 `can_kill`。

- [ ] **Step 1: 改写 test_lethal_render.py 的 golden 与断言（失败测试）**

模块 docstring 整体替换为：

```python
"""斩杀线渲染(render 第三行 + overlay 上区行): plan 机读事实 → "可斩: …" 措辞。

口径(2026-09-18 lethal-mana-feasible spec): 斩杀行双口径 —— plan 在场 =
法力可行链 `斩杀 X(场A+线B)`; plan 缺席 = 理论粗估 `斩杀 理论X(手A+库B+场C)`
且 can_kill 恒 False(golden 硬编码断言, 防措辞漂移)。结论词"可斩"归 render,
plan 只带事实。
"""
```

`_GOLDEN_TWO_LINES` 定义（`_LINE3` 之前）替换为三个 golden：

```python
_GOLDEN_THEORY = (
    "敌 2(2血+0甲) │ 斩杀 理论6(手6+库?+场0) │ 法强 0\n"
    "回费 +0(手0+库?) │ 费 组?/库?/手4"
)
# plan 口径(_PLAN: total=6, board_atk=0 → 场0+线6)
_GOLDEN_PLAN_KILL = (
    "敌 2(2血+0甲) │ 斩杀 6(场0+线6,可斩) │ 法强 0\n"
    "回费 +0(手0+库?) │ 费 组?/库?/手4"
)
_GOLDEN_PLAN_NO_KILL = (
    "敌 2(2血+0甲) │ 斩杀 6(场0+线6) │ 法强 0\n"
    "回费 +0(手0+库?) │ 费 组?/库?/手4"
)
```

`test_stat_text_golden_two_lines_unchanged` 整体替换为：

```python
def test_stat_text_theory_golden(tmp_path):
    """基线回归: 不带 plan = 理论粗估口径 —— lethal_est=True, can_kill
    恒 False(宁漏勿错), 无 `,可斩` 标记; 硬编码 golden 防措辞漂移。"""
    from hsbot.render import stat_fields, stat_text

    st, db, a = _scene(tmp_path)
    f = stat_fields(st, knowledge=None, carddb=db, analyzer=a)
    assert f["lethal_est"] is True and f["can_kill"] is False
    assert stat_text(f) == _GOLDEN_THEORY
```

`test_stat_text_appends_lethal_line` 中，在 `assert f["plan"] is _PLAN` 之后加字段断言，golden 引用改名：

```python
    assert f["plan"] is _PLAN                  # 原样透传(契约 §3)
    assert f["lethal_est"] is False and f["can_kill"] is True
    assert f["lethal"] == 6 and f["lethal_board"] == 0 \
        and f["lethal_hand"] == 6              # 法力可行链接管主数字
    out = stat_text(f)
    assert out == f"{_GOLDEN_PLAN_KILL}\n{_LINE3}"
```

`test_stat_text_lethal_false_no_third_line` 的断言改为：

```python
    assert stat_text(f) == _GOLDEN_PLAN_NO_KILL
```

`test_plan_line_carddb_name_missing_falls_back` 中两处 `_GOLDEN_TWO_LINES` 引用改为 `_GOLDEN_PLAN_KILL`（该测试传 `plan=_PLAN`）。

`test_plan_degenerate_empty_actions_no_crash` 的断言改为（退化 plan 无 total → 理论数字，但 can_kill 随 `plan.lethal=True` → kill_mark 仍在）：

```python
    out = stat_text(f)                         # 不抛异常
    assert out.startswith("敌 2(2血+0甲) │ 斩杀 理论6(手6+库?+场0,可斩)")
    assert "可斩: (无动作) 伤?+场? ≥ ?" in out
```

其余测试（plan_line / plan_data_line / plan_rows / advice_panel，行 121 起）不动——它们只消费 `f["plan"]` 透传与 plan_rows，不受口径切换影响。

- [ ] **Step 2: 改写 test_render_knowledge.py::test_top_summary_two_lines（失败测试）**

docstring 替换：

```python
def test_top_summary_two_lines(tmp_path):
    """上部信息区: 敌血甲总和/斩杀(双口径)/法强 + 回费/费用,
    理论粗估(plan 缺席)不报可斩; 通用模式牌库侧诚实降级为 ?;
    友方未解析返回 None。"""
```

正文断言逐条替换（构造段 `_store()` 起至 `k.rebuild(st)` 不动）：

```python
    out = top_summary(st, knowledge=k, carddb=db, analyzer=a)
    l1, l2 = out.split("\n")
    # 理论斩杀 = 手火球(6+法强2)=8 + 库剩火球(6+法强2)=8 + 场攻 3 = 19;
    # plan 缺席 → 只报理论, 不报可斩(宁漏勿错, 2026-09-18 口径)
    assert "敌 8(5血+3甲)" in l1
    assert "斩杀 理论19(手8+库8+场3)" in l1
    assert ",可斩" not in l1
    assert "法强 2" in l1
```

（l2 的三条断言与后半段直到 `out2` 之前不动。）`out2` 段替换：

```python
    out2 = top_summary(st, knowledge=None, carddb=db, analyzer=a)
    assert "库?" in out2 and "组?" in out2
    assert "斩杀 理论11(手8+库?+场3)" in out2
```

机读字段段替换（can_kill 翻转 + lethal_est）：

```python
    f = stat_fields(st, knowledge=k, carddb=db, analyzer=a)
    assert f["enemy_total"] == 8 and f["can_kill"] is False \
        and f["lethal_est"] is True
    assert f["lethal"] == 19 and f["lethal_hand"] == 8 \
        and f["lethal_deck"] == 8 and f["lethal_board"] == 3
```

（ramp/cost/discount 三条断言与 `stat_text(f) == out` 不动。）`f2` 段替换：

```python
    f2 = stat_fields(st, knowledge=None, carddb=db, analyzer=a)
    assert f2["lethal_deck"] is None and f2["cost_list"] is None \
        and f2["can_kill"] is False and f2["lethal_est"] is True
```

测试末尾追加 plan 口径钉子（虚高修复的回归钉）：

```python
    # plan 口径(2026-09-18 spec): 法力可行链接管主数字与判定
    pl = {"actions": [], "total": 9, "face_det": 9, "face_exp": 0,
          "lethal": True, "enemy_total": 8, "board_atk": 3, "uncovered_n": 0}
    f3 = stat_fields(st, knowledge=k, carddb=db, analyzer=a, plan=pl)
    assert f3["lethal"] == 9 and f3["lethal_board"] == 3 \
        and f3["lethal_hand"] == 6
    assert f3["can_kill"] is True and f3["lethal_est"] is False
    assert "斩杀 9(场3+线6,可斩)" in stat_text(f3)
    # 回归钉: 理论 19 > 敌 8, 但 plan 不可斩 → 不报可斩
    pl2 = {**pl, "total": 5, "lethal": False}
    f4 = stat_fields(st, knowledge=k, carddb=db, analyzer=a, plan=pl2)
    assert f4["lethal"] == 5 and f4["can_kill"] is False \
        and f4["lethal_est"] is False
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_lethal_render.py tests/test_render_knowledge.py -q`
Expected: FAIL —— `lethal_est` KeyError 与 golden 不匹配（`理论6` vs `6(...,可斩)`）。

- [ ] **Step 4: 实现 stat_fields 口径切换**

`hsbot/render.py` stat_fields docstring 替换为：

```python
    """信息区机读字段(唯一事实来源): 敌血甲/可斩伤害/法强/回费/费用。
    文本版(stat_text)与悬浮窗分格面板(overlay)都从它渲染, 不做平行计算。
    伤害口径(2026-09-18 lethal-mana-feasible spec): plan 在场 → 法力可行链
    plan.total(逐张付费含实时减费/已知抽/引擎抽; 场=plan.board_atk,
    线=total−场), can_kill 复用 plan.lethal(planner 统一斩杀算式, 不重算);
    plan 缺席(对手回合/开关关/无手牌)或退化(无 total)→ 理论粗估(手牌伤害+
    牌库剩余伤害+场面总攻, 均按当前实际法强加成, 无法力约束) + lethal_est=True,
    can_kill 恒 False(粗估不构成可斩依据)。
    友方或对手未解析 → None(信息区保持原样)。
    plan: 斩杀线机读事实(analysis.lethal_plan 产物, None=未算/开关关闭),
    原样透传入 dict。"""
```

函数体内，删除 `lethal = burst_hand + (burst_deck or 0) + burst_board` 一行；在 `mana = st.mana_now(me)` 行之后、`return {` 之前插入：

```python
    # 伤害口径(2026-09-18 lethal-mana-feasible spec): plan 在场(我方回合+
    # 开关开)→ 法力可行链接管, can_kill 复用 plan.lethal(避免双口径);
    # plan 缺席/退化(无 total)→ 理论粗估原式 + lethal_est=True, can_kill
    # 恒 False(宁漏勿错: 粗估无法力约束, 不报可斩; 退化 plan 带 lethal=True
    # 时 can_kill 仍随 plan, 数字诚实降级为理论值)。
    if plan is not None and plan.get("total") is not None:
        burst_board = plan.get("board_atk", 0)
        burst_hand = plan["total"] - burst_board
        lethal = plan["total"]
        can_kill = bool(plan["lethal"])
        lethal_est = False
    else:
        lethal = burst_hand + (burst_deck or 0) + burst_board
        can_kill = bool(plan.get("lethal")) if plan is not None else False
        lethal_est = True
```

return dict 中原两行：

```python
        "lethal": lethal, "lethal_hand": burst_hand, "lethal_deck": burst_deck,
        "lethal_board": burst_board,
        "can_kill": enemy is not None and lethal > 0 and lethal >= enemy,
```

替换为：

```python
        "lethal": lethal, "lethal_hand": burst_hand, "lethal_deck": burst_deck,
        "lethal_board": burst_board,
        "can_kill": can_kill,
        "lethal_est": lethal_est,
```

- [ ] **Step 5: 实现 stat_text 双口径措辞**

`hsbot/render.py` stat_text 整体替换为：

```python
def stat_text(f: dict) -> str:
    """信息区字段 → 两行文本(控制台/会话文件用; 悬浮窗走分格面板)。
    斩杀行双口径(2026-09-18 spec): 法力可行链(lethal_est=False) →
    `斩杀 X(场A+线B)`; 理论粗估(lethal_est=True) → `斩杀 理论X(手A+库B+场C)`,
    无法力约束不构成可斩依据。can_kill 时追加 `,可斩`。手牌减费在身时行尾
    追加 减N(来源) 段; 无减费保持两段。plan.lethal 时末尾追加第三行
    "可斩: 线 伤X+场Y ≥ Z"(措辞归 render); 无第三行时不影响前两行。"""
    q = lambda v: "?" if v is None else str(v)               # noqa: E731
    if f["enemy_total"] is None:
        enemy_txt = "?"
    else:
        enemy_txt = f"{f['enemy_total']}({f['enemy_hp']}血+{f['enemy_armor']}甲)"
    kill_mark = ",可斩" if f["can_kill"] else ""
    if f.get("lethal_est"):
        lethal_txt = (f"理论{f['lethal']}(手{f['lethal_hand']}"
                      f"+库{q(f['lethal_deck'])}"
                      f"+场{f['lethal_board']}{kill_mark})")
    else:
        lethal_txt = (f"{f['lethal']}(场{f['lethal_board']}"
                      f"+线{f['lethal_hand']}{kill_mark})")
    line2 = (f"回费 +{f['ramp']}(手{f['ramp_hand']}+库{q(f['ramp_deck'])})"
             f" │ 费 组{q(f['cost_list'])}/库{q(f['cost_deck'])}/手{f['cost_hand']}")
    disc = f.get("discount") or {}
    if disc.get("total"):
        srcs = "·".join(disc.get("sources") or [])
        line2 += f" │ 减{disc['total']}" + (f"({srcs})" if srcs else "")
    txt = (f"敌 {enemy_txt} │ 斩杀 {lethal_txt}"
           f" │ 法强 {f['spellpower']}\n{line2}")
    plan = f.get("plan") or {}
    line3 = _lethal_line(plan, f.get("_carddb")) if plan.get("lethal") else None
    if line3:
        txt += f"\n{line3}"
    return txt
```

- [ ] **Step 6: 跑本任务测试确认通过**

Run: `python -m pytest tests/test_lethal_render.py tests/test_render_knowledge.py -q`
Expected: PASS（本两个文件全绿；test_overlay_* 此时**应仍绿**——overlay 旧代码读 `f["can_kill"]`（值变了但类型没变）+ 旧细节文本用的是测试自造 dict，不经过 stat_fields；若 overlay 测试意外红了，检查是否漏改 stat_fields 语义）。

- [ ] **Step 7: 提交**

```bash
git add hsbot/render.py tests/test_lethal_render.py tests/test_render_knowledge.py
git commit -m "feat: 可斩伤害口径切换——stat_fields 法力可行链(plan.total)接管主数字与判定, 理论粗估降级标注(can_kill 恒 False 堵虚高误报), stat_text 双口径措辞"
```

---

### Task 2: overlay 可斩色键与面板双形态渲染

**Files:**
- Modify: `hsbot/overlay.py`（模块 docstring:11、_DEFAULT_COLORS:52、_StatPanel docstring:93-99、_CELLS:102、update() lethal 段:166-172）
- Modify: `config.yaml`（overlay_colors 注释块 :50 后）
- Test: `tests/test_overlay_layout.py`（_BASE + test_stat_panel_new_cells + 新测试）
- Test: `tests/test_overlay_smoke.py`（两处 base dict + 断言 :108-124）

**Interfaces:**
- Consumes: Task 1 的 `f["lethal_est"]`（bool）、`f["can_kill"]`（bool）；色键名 `lethal`
- Produces: `_DEFAULT_COLORS["lethal"] = "#ff9500"`；面板细节双形态文案（Task 3 验收目检依据）

- [ ] **Step 1: 改写 overlay 测试（失败测试）**

`tests/test_overlay_layout.py` `_BASE` 中（replace_all，该文件内此模式唯一）：

```python
         "lethal_board": 0, "can_kill": False, "spellpower": 0,
```
→
```python
         "lethal_board": 0, "can_kill": False, "lethal_est": False,
         "spellpower": 0,
```

`test_stat_panel_new_cells` 整体替换：

```python
def test_stat_panel_new_cells():
    """六格布局: 可斩伤害格(2026-09-18 口径)细节双形态 —— 法力可行链
    (场+线+潜力库) / 理论粗估(手+库+场); 可斩数值转亮橙(lethal 新色键);
    法力格缺数据默认 1; 回费细节按 库+手(卡组、手牌)序; 减费格不变。"""
    root = tk.Tk()
    root.withdraw()                    # 测试不显示 UI
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _StatPanel
        assert ("lethal", "可斩伤害") in _StatPanel._CELLS   # 标签更名
        p = _StatPanel(tk, root, dict(_DEFAULT_COLORS), 10)
        p.update(dict(_BASE))
        assert p.cells["mana"][0].cget("text") == "2"
        assert p.cells["mana"][1].cget("text") == "水晶2/3"
        assert p.cells["lethal"][1].cget("text") == \
            "场0+线0 · 潜力库14 · 法强0"
        assert p.cells["lethal"][0].cget("foreground") == \
            _DEFAULT_COLORS["stat"]                    # 不可斩: 常规色
        p.update({**_BASE, "can_kill": True})
        assert p.cells["lethal"][1].cget("text") == \
            "可斩 场0+线0 · 潜力库14 · 法强0"
        assert p.cells["lethal"][0].cget("foreground") == \
            _DEFAULT_COLORS["lethal"]                  # 可斩: 亮橙新色键
        p.update({**_BASE, "lethal_est": True})        # 理论粗估形态
        assert p.cells["lethal"][1].cget("text") == \
            "理论 手0+库14+场0 · 法强0"
        p.update({k: v for k, v in _BASE.items()
                  if k not in ("mana", "mana_res")})
        assert p.cells["mana"][0].cget("text") == "1"     # 默认 1(用户定版)
        assert p.cells["mana"][1].cget("text") == ""
        p.reset()
        assert p.cells["enemy"][0].cget("text") == "—"
        assert p.cells["mana"][0].cget("text") == "1"
    finally:
        root.destroy()
```

文件末尾追加新测试：

```python
def test_stat_panel_lethal_color_missing_key_falls_back():
    """旧 config 无 lethal 色键: can_kill 回退 stat 色(向后兼容)。"""
    root = tk.Tk()
    root.withdraw()
    try:
        from hsbot.overlay import _DEFAULT_COLORS, _StatPanel
        colors = {k: v for k, v in _DEFAULT_COLORS.items() if k != "lethal"}
        p = _StatPanel(tk, root, colors, 10)
        p.update({**_BASE, "can_kill": True})
        assert p.cells["lethal"][0].cget("foreground") == \
            _DEFAULT_COLORS["stat"]
    finally:
        root.destroy()
```

`tests/test_overlay_smoke.py`：两处 base dict（约 :77 与 :99）都插入 lethal_est（对 `"can_kill": False, "spellpower": 0,` 用 replace_all）：

```python
                "lethal_board": 0, "can_kill": False, "spellpower": 0,
```
→
```python
                "lethal_board": 0, "can_kill": False, "lethal_est": False,
                "spellpower": 0,
```
（注意两处缩进可能不同：:77 处 12 空格、:99 处 16 空格；若 replace_all 因缩进不同只命中一处，对另一处单独 Edit，old_string 以实际文件为准。）

断言替换（:109 / :118 / :119）：

```python
        assert p.cells["lethal"][1].cget("text") == "手0+库14+场0 · 法强0"
```
→
```python
        assert p.cells["lethal"][1].cget("text") == "场0+线0 · 潜力库14 · 法强0"
```

```python
        assert p.cells["lethal"][0].cget("foreground") == _DEFAULT_COLORS["advice"]
```
→
```python
        assert p.cells["lethal"][0].cget("foreground") == _DEFAULT_COLORS["lethal"]
```

```python
        assert p.cells["lethal"][1].cget("text") == "可斩 手0+库14+场0 · 法强0"
```
→
```python
        assert p.cells["lethal"][1].cget("text") == "可斩 场0+线0 · 潜力库14 · 法强0"
```

（:108 `"14"`、:123-124 `"库?"` 断言不动——新文案 `潜力库?` 仍含子串 `库?`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_overlay_layout.py tests/test_overlay_smoke.py -q`
Expected: FAIL —— 标签断言（理论伤害≠可斩伤害）、色键 KeyError/断言不等、细节文本不匹配。

- [ ] **Step 3: 实现 overlay 改动**

`hsbot/overlay.py`：

(a) :11 模块 docstring：`敌/理论伤害/法力` → `敌/可斩伤害/法力`

(b) `_DEFAULT_COLORS` 中 `"advice"` 行之后插入：

```python
    "lethal": "#ff9500",    # 可斩伤害数值(法力可行链 ≥ 敌血甲; 亮橙, 区别于 advice 金)
```

(c) `_StatPanel` docstring（:93-99）三处措辞：`敌/理论伤害/法力` → `敌/可斩伤害/法力`；`法强并入理论伤害格细节` → `法强并入可斩伤害格细节`；`"可斩"时理论伤害数值转金色(advice 色)` → `"可斩"时可斩伤害数值转亮橙(lethal 色, 缺键回退 stat 色)`

(d) `_CELLS`：

```python
    _CELLS = [("enemy", "敌"), ("lethal", "可斩伤害"), ("mana", "法力"),
              ("ramp", "回费"), ("discount", "减费"), ("cost", "法术费")]
```

(e) `update()` 中 lethal 段（原 `v, d = self.cells["lethal"]` 起三组 configure）替换为：

```python
        v, d = self.cells["lethal"]
        v.configure(text=str(f["lethal"]),
                    fg=(c.get("lethal") or c["stat"]) if f["can_kill"]
                       else c["stat"])
        if f.get("lethal_est"):
            det = (f"理论 手{f['lethal_hand']}+库{self._q(f['lethal_deck'])}"
                   f"+场{f['lethal_board']}")
        else:
            det = (f"场{f['lethal_board']}+线{f['lethal_hand']}"
                   f" · 潜力库{self._q(f['lethal_deck'])}")
        if f["can_kill"]:
            det = f"可斩 {det}"
        d.configure(text=f"{det} · 法强{f['spellpower']}")
```

`config.yaml` overlay_colors 注释块 `advice` 行后插入一行：

```yaml
#   lethal: "#ff9500"     # 可斩伤害数值(法力可行链总伤≥敌血甲)
```

- [ ] **Step 4: 跑 overlay 测试确认通过**

Run: `python -m pytest tests/test_overlay_layout.py tests/test_overlay_smoke.py -q`
Expected: PASS（无显示环境时 tkinter 用例 skip，属正常）。

- [ ] **Step 5: 提交**

```bash
git add hsbot/overlay.py config.yaml tests/test_overlay_layout.py tests/test_overlay_smoke.py
git commit -m "feat: 悬浮窗可斩伤害格双形态渲染+亮橙色键 lethal #ff9500——法力可行链(场+线+潜力库)/理论粗估(手+库+场), 标签更名可斩伤害, 缺色键回退 stat"
```

---

### Task 3: 全量回归与验收

**Files:**
- 无新改动（验收任务；如发现文档残留措辞则修改后提交）

**Interfaces:**
- Consumes: Task 1/2 的全部产物
- Produces: 验收结论（全绿测试 + replay 目检 + 无残留旧措辞）

- [ ] **Step 1: 全量测试**

Run: `python -m pytest -q`
Expected: 全绿，数量 ≥ 376（基线 376 + 新增 test_stat_panel_lethal_color_missing_key_falls_back 等，约 378-380，取决于 tkinter skip）。

- [ ] **Step 2: 残留措辞扫描**

Run: `grep -rn "理论伤害" hsbot/ tests/ config.yaml README.md docs/ARCHITECTURE.md docs/PLAY_ADVICE.md`
Expected: 仅 `docs/superpowers/specs/`（spec 历史文档）命中；代码与在用文档零残留。若有在用文档命中（如 PLAY_ADVICE 描述信息区措辞），同步更新该措辞并纳入 Step 4 提交。

- [ ] **Step 3: replay 目检**

Run: `python -m hsbot replay examples/Power.log`
Expected（人工核对控制台输出）:
- 回合结束快照前信息区两行：`斩杀 理论N(手A+库B+场C)` 形态（replay 无悬浮窗、stat 行按 lethal_plan 开关出现 plan 口径时为 `斩杀 X(场A+线B)`）；
- 不再出现理论数字后跟 `,可斩` 的组合（除非同批 plan.lethal=True）；
- 退出码 0，无 `! 事件处理异常`。

- [ ] **Step 4: 提交（如有文档修正）**

```bash
git add -A
git commit -m "docs: 可斩口径上线后的文档措辞同步(如 Step 2 有发现)"
```
（无改动则跳过本步。）

---

## Self-Review 记录

- **Spec 覆盖**：spec §2 字段表 → Task 1 Step 4；§3 stat_text/overlay/config → Task 1 Step 5 + Task 2 Step 3；§5 五条契约 → Task 1 Step 1/2（golden、can_kill 翻转、lethal_est、plan 口径、回归钉）+ Task 2 Step 1（色键、缺键回退）；§5 replay/pytest 验收 → Task 3。§6 非目标未越界（planner/analysis 零改动、未接 mc_plan、未动 pipeline）。
- **占位符扫描**：无 TBD/TODO；所有断言与实现均给出完整代码。
- **类型/命名一致性**：`lethal_est`（bool）在 Task 1 产出、Task 2 消费，拼写一致；色键 `lethal` 三处（_DEFAULT_COLORS/config/测试）一致；golden 文案与 stat_text/面板实现逐字符对齐。
