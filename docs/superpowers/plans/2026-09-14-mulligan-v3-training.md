# 留牌训练 v3 实施计划 —— 两阶段小基座 + 蒸馏系数接入实时留牌

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现留牌训练 v3（material_v2 素材 → Q 模型门控蒸馏标签 → 基座评分器 →
2ⁿ 打分 → 蒸馏系数表），把系数表接入实时留牌建议（零 torch），`mulligan_v3=false`
时全链路输出与 v2 逐字节一致。

**Architecture:** 训练侧全部进 `trainer/`（material_v2 重放扩展 / qvalue / scorer），
`python -m trainer mulligan train` 一个命令产出 v3.json 到现有版本目录；
推理侧 `hsbot/mulligan_ai.py` 只加纯 stdlib 的特征函数、组合输出函数与 v3 读取路径
（live 走系数表 = 现有 `best_keep_set` 管线，CLI 走基座全量枚举），render 只加组合措辞。

**Tech Stack:** 纯 stdlib（推理/蒸馏）+ tabpfn 6.1.0 / torch 2.2.2+cpu（训练侧，已装）
+ sklearn（对照基线，已装）。TabICL 只留后端槽位，本期不装。

**Spec:** `docs/superpowers/specs/2026-09-14-mulligan-foundation-model-design.md`
（含 §5.4 拆行合法性附录——训练行只许真实共现观测，反事实候选集只在推理端枚举）。

## Global Constraints

- 防泄漏铁律（spec §2，测试钉死）：评分器特征只允许决策时信息（offered/候选留集/
  对手职业/先后手/卡组构成）；Q 模型特征允许回合 K 已发生的信息；**任何反事实
  候选集复制本局胜负标签 = 测试失败**。
- 组合输出是机读事实，中文措辞/阈值展示归 render（项目铁律）；`anti_synergy`
  阈值 −0.02 是 spec §5.3 定稿值，常量放 mulligan_ai 并注明出处。
- live 进程零新增依赖、零 torch；TabPFN 权重缓存必须走 `tabpfn_env()` 钉项目
  目录（绝不落 C 盘，用户硬要求）。
- 手写常量必须注明依据（学出来的参数 vs 选定的超参数）。
- `mulligan_v3=false` 时：不写 v3.json、live/CLI 输出与 v2 逐字节一致（钉子测试）。
- 版本产物原子写 + `_VersionLock` 沿用现状；`_KEEP_VERSIONS` 清理不变。
- 测试不弹 UI/不抢前台；全部走日志回放与 tmp_path。
- 现有 207 测试全程保持绿；每任务一个 commit。

## 现状锚点（侦察结论，执行者必读）

- 语料：`data/training/奇迹德/` 83 个 jsonl（36 个带 `replaced_in`，83 个带
  `decklist`）+ 60 个 `.power.log` 切片。v2 现行模型 v009：LR AUC 0.714 /
  TabPFN AUC 0.727（时序留出），90 局。
- `trainer/material.py::_replay_slice` 已产出回合行（snap+actions+result）；
  v1 `material.jsonl` 与 value 模型不动。
- `hsbot/store.py::MulliganState.replaced_in` 与 `mulligan_facts()` 已有换入牌推导
  （decided 且未 closed 窗口内的抽牌），重放时直接消费。
- `hsbot/mulligan_ai.py`：`best_keep_set(cards, gains, pair_bonus)` 就是
  `Σgain + Σpair` 打分——v3 live 路径直接复用，只换 gains/pairs 来源。
- `_save_version(root, deck, stats, lr, tab, games, n_new, skip)` 在
  `trainer/mulligan.py:380`；`read_latest` 在 `hsbot/mulligan_ai.py:327`；
  `MulliganAdvisor.__init__` 在 `hsbot/mulligan_ai.py:421`，构造点
  `hsbot/watcher.py:262`（有 cfg 在手）。
- `trainer/backtest.py::group_folds/cv_auc` 是组级切分唯一权威实现，Q/评分器
  对照表必须复用它（组 = 会话 = `game.split("_g")[0]`）。
- CLI 子命令分发：`trainer/__main__.py` 把含 "mulligan" 的 argv 转发给
  `trainer/mulligan.py::main`。

---

### Task 1: material_v2 素材扩展（决策行/drawn_this_turn/结果行 + 夹具钉子）

**Files:**
- Modify: `trainer/material.py`（新增 `build_material_v2`，`_replay_slice` 加收料）
- Create: `tests/fixtures/mulligan_draw.log`
- Test: `tests/test_mulligan_train.py`（追加）

**Interfaces:**
- Produces: `build_material_v2(corpus_dir: Path, deck: str, out_dir: Path,
  carddb, battletag: str) -> dict`，写 `out_dir/material_v2.jsonl`，行类型：
  - `{"row":"turn","game":"<切片名>#g<gi>","snap":{...},"actions":[...],
    "drawn":[cid,...],"src":...,"result":0/1}`
  - `{"row":"mulligan","game":...,"offered":[cid],"kept":[cid],
    "replaced_in":[cid],"coin":0/1,"opp_class":"PRIEST"|"UNKNOWN",
    "decklist":{...}|None,"result":0/1}`
  - `{"row":"result","game":...,"result":0/1}`
  v1 的 `build_material`/`material.jsonl` 行为与产物**零变化**。
- Produces: 统计 dict `{"slices","rows","games","wins","skip","mulligan_rows"}`。

- [ ] **Step 1: 制作夹具 `tests/fixtures/mulligan_draw.log`**

  从 `data/training/奇迹德/` 任一真实切片截取 CREATE_GAME → 我方第 3 回合段，
  按以下要求收尾（后手局，含硬币、换入、T2 抽牌）：
  - 我方 = 湫然#51704（PlayerID=2，EntityID=3），对手 UNKNOWN（PlayerID=1）；
  - 起手 SHOW_ENTITY 三张 + GAME_005（硬币）；
  - `EntityChoices ... ChoiceType=MULLIGAN`（含硬币共 4 实体）后
    `SendChoices` 只保留其中 1 张；
  - 决定后、我方首回合 turn_start 前：2 张换入牌 DECK→HAND 揭示；
  - 对手 T1（我方不产行）；TURN=2 我方回合内抽 1 张；
  - 结尾 `TAG_CHANGE Entity=3 tag=PLAYSTATE value=WON` + `Entity=2 ... LOST`。

  落盘后先跑一次探针拿真值（防手写日志与 store 时序假设漂移）：
  ```bash
  python - <<'EOF'
  from pathlib import Path
  from hsbot.carddb import CardDB
  from trainer.material import build_material_v2
  import tempfile, json
  tmp = Path(tempfile.mkdtemp())/"corpus"/"奇迹德"; tmp.mkdir(parents=True)
  (tmp/"mulligan_draw.power.log").write_text(
      Path("tests/fixtures/mulligan_draw.log").read_text(encoding="utf-8"), encoding="utf-8")
  st = build_material_v2(tmp.parent, "奇迹德", tmp.parent/"out",
                         CardDB("/nonexistent.json"), "湫然#51704")
  for ln in (tmp.parent/"out"/"material_v2.jsonl").read_text(encoding="utf-8").splitlines():
      r = json.loads(ln); print(json.dumps(r, ensure_ascii=False)[:200])
  print(st)
  EOF
  ```
  把打印出的 决策行 offered/kept/replaced_in/coin 与 T2 行 drawn **原样冻结**进
  Step 2 的断言（探针输出即真值；若探针行不符合上面 7 条结构要求，先修夹具
  再探针，不许改断言迁就坏夹具）。

- [ ] **Step 2: 写失败测试**（`tests/test_mulligan_train.py` 追加）

```python
def test_material_v2_rows_from_fixture(tmp_path):
    """v3 素材: 重放推导决策行(含 replaced_in——旧 meta 缺字段的补法)、
    回合行 drawn_this_turn、结果行; 真值来自 mulligan_draw.log 夹具探针。"""
    from trainer.material import build_material_v2
    corpus = tmp_path / "corpus" / "奇迹德"
    corpus.mkdir(parents=True)
    (corpus / "mulligan_draw.power.log").write_text(
        (ROOT / "tests" / "fixtures" / "mulligan_draw.log")
        .read_text(encoding="utf-8"), encoding="utf-8")
    stats = build_material_v2(tmp_path / "corpus", "奇迹德", tmp_path / "out",
                              NO_DB, "湫然#51704")
    rows = [json.loads(ln) for ln in
            (tmp_path / "out" / "material_v2.jsonl").read_text(encoding="utf-8")
            .splitlines() if ln.strip()]
    mull = [r for r in rows if r["row"] == "mulligan"]
    turns = [r for r in rows if r["row"] == "turn"]
    result = [r for r in rows if r["row"] == "result"]
    assert stats["mulligan_rows"] == 1 and len(mull) == 1
    m = mull[0]
    # ↓ 冻结值来自 Step 1 探针(硬编码, 不许用"从 replay 再推导"的自证写法)
    assert m["offered"] == <探针值: offered 去硬币后 cids>
    assert m["kept"] == <探针值>
    assert m["replaced_in"] == <探针值: 两张换入 cids>
    assert m["coin"] == 1 and m["result"] == 1 and m["opp_class"] == "UNKNOWN"
    t2 = [r for r in turns if r["snap"]["turn"] == 2]
    assert t2 and t2[0]["drawn"] == <探针值: T2 抽牌 cids>
    assert all(r["snap"]["my_turn"] for r in turns)      # 对手回合不产行
    assert len(result) == 1 and result[0]["result"] == 1


def test_material_v1_untouched(tmp_path):
    """v3 扩展不许动 v1 产物: build_material 的行结构与 material.jsonl 不变。"""
    corpus = tmp_path / "corpus" / "奇迹德"
    corpus.mkdir(parents=True)
    (corpus / "mulligan_draw.power.log").write_text(
        (ROOT / "tests" / "fixtures" / "mulligan_draw.log").read_text(encoding="utf-8"),
        encoding="utf-8")
    build_material_v2(tmp_path / "corpus", "奇迹德", tmp_path / "out", NO_DB, "湫然#51704")
    build_material(tmp_path / "corpus", "奇迹德", tmp_path / "out", NO_DB, "湫然#51704")
    rows = [json.loads(ln) for ln in
            (tmp_path / "out" / "material.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(set(r) == {"snap", "actions", "src", "result"} for r in rows)  # 无 row/drawn 键
    assert not any("row" in r for r in rows)
```

- [ ] **Step 3: 跑测试确认失败** — `python -m pytest tests/test_mulligan_train.py -k material_v2 -q`
  预期：`ImportError: cannot import name 'build_material_v2'`。

- [ ] **Step 4: 实现 `build_material_v2`**（`trainer/material.py`）

  `_replay_slice` 加参数 `collect_v2: bool = False`：on_event 里 `kind == "draw"`
  且 pend 不为 None 时 `drawn.append(cid)`（draw 事件已被 store 去重，同 cid 双张
  = 两次事件，保留重复）；v2 模式下每局收口后取
  `facts = st.mulligan_facts()`，friendly_key 的 `decided=True` 才产决策行；
  `opp_class = carddb.card_class(st.hero(st.opponent_key()).card_id) or "UNKNOWN"`；
  decklist 按切片 stem 读同目录 `<stem>.jsonl` 首行 meta 的 `decklist`（缺文件
  → None）。行 `game` 键 = `f"{path.name}#g{gi}"`。`build_material_v2` 复用
  `_replay_slice(collect_v2=True)` 的 `(turn_rows, mull_rows)` 双返回，写
  `material_v2.jsonl`（三类行都带 result；决策行 result 直接落行）。v1 路径
  零改动（默认参数关闭收料，`material.jsonl` 行结构不变）。

- [ ] **Step 5: 跑测试到绿** — 同 Step 3 命令 + `python -m pytest tests/test_trainer.py -q`（v1 不回归）。

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat: material_v2 素材扩展——重放推导决策行/换入牌/逐回合抽牌"`

### Task 2: 决策时特征构造器（§5.1 schema，纯 stdlib + 泄漏/定长钉子）

**Files:**
- Modify: `hsbot/mulligan_ai.py`（新段「v3 评分器特征 + 组合输出」）
- Test: `tests/test_mulligan_ai.py`（追加）

**Interfaces:**
- Produces:
```python
V3_LAYOUT = 1
ANTI_SYNERGY_THR = -0.02   # spec §5.3 定稿: pair_synergy < 此值 → 不宜同留

def v3_feature_names(vocab: list, classes: list, pairs: list) -> list: ...
def mulligan3_features(model: dict, offered: list, kept: list,
                       coin: bool, opp_class: str) -> list:
    """model = {"vocab","classes","pairs","engine","deck_tail"}(布局单点)。
    返回定长向量 = offered onehot+词表外兜底位 | kept onehot | 留牌数 |
    曲线覆盖1/2/3费(留集中有 ≤k 费可出) | 留集总费 | 留集低费(≤2)法术数 |
    留集引擎件数 | 留集/卡组引擎密度 | pair 指示(len(pairs)) |
    对手职业 onehot | 后手 | deck_tail(原样追加)。"""
```
  cost 查询走注入的 `model["cost"]`（cid→int|None callable，由训练/服务侧用
  carddb 构造；本层零卡表依赖）。

- [ ] **Step 1: 写失败测试**

```python
def _v3_model():
    return {"vocab": ["A", "B", "C"], "classes": ["PRIEST", "MAGE"],
            "pairs": [["A", "B"]], "engine": ["B"],
            "deck_tail": [0.5] * 15, "deck_engine": 2.0,
            "cost": {"A": 1, "B": 5, "C": 2}.get}
```
（cost/deck_engine 并入 model dict，签名即契约。）

```python
def test_v3_features_schema_fixed_length():
    import hsbot.mulligan_ai as ai
    m = _v3_model()
    names = ai.v3_feature_names(m["vocab"], m["classes"], m["pairs"])
    x1 = ai.mulligan3_features(m, ["A", "B"], ["A"], False, "PRIEST")
    x2 = ai.mulligan3_features(m, ["C"], [], True, "MAGE")
    assert len(names) == len(x1) == len(x2)
    # 泄漏钉子: schema 里不允许出现决策后字段
    banned = ("replaced", "drawn", "board", "snap", "hand_")
    assert not any(b in n for n in names for b in banned)

def test_v3_features_values_and_fallback():
    import hsbot.mulligan_ai as ai
    m = _v3_model()
    x = ai.mulligan3_features(m, ["A", "ZZZ"], ["A"], False, "PRIEST")
    assert x[0] == 1.0 and x[1] == 0.0 and x[2] == 0.0
    assert x[len(m["vocab"])] == 1.0                 # 词表外兜底位(ZZZ)
    assert x[len(m["vocab"]) + 1] == 1.0             # kept onehot A
    assert x[-len(m["deck_tail"]):] == m["deck_tail"]  # 尾段原样

def test_v3_features_curve_engine_pairs():
    import hsbot.mulligan_ai as ai
    m = _v3_model()
    x = ai.mulligan3_features(m, ["A", "B"], ["A", "B"], True, "PRIEST")
    names = ai.v3_feature_names(m["vocab"], m["classes"], m["pairs"])
    get = dict(zip(names, x))
    assert get["kept_n"] == 2
    assert get["cover_1"] == 1.0 and get["cover_2"] == 1.0 and get["cover_3"] == 0.0
    assert get["kept_cost"] == 6.0                   # A(1)+B(5)
    assert get["kept_cheap_spell"] == 0.0            # 无卡表 cardtype → 0
    assert get["kept_engine"] == 1.0                 # B 在 engine 词表
    assert get["engine_density"] == 0.5              # 1/2
    assert get["pair_A|B"] == 1.0
    assert get["coin"] == 1.0 and get["opp_PRIEST"] == 1.0 and get["opp_MAGE"] == 0.0
```

- [ ] **Step 2: 跑失败** — `python -m pytest tests/test_mulligan_ai.py -k v3_features -q`
  预期 AttributeError。
- [ ] **Step 3: 实现**（`hsbot/mulligan_ai.py` 新段；纯 stdlib，cost/type 经
  `model["cost"]/model["cardtype"]` callable 注入，缺省 `{}.get` → 0）
- [ ] **Step 4: 跑绿** — 同 Step 2 + 全文件 `python -m pytest tests/test_mulligan_ai.py -q`
- [ ] **Step 5: Commit** — `feat: v3 决策时特征构造器(定长 schema+词表外兜底+泄漏钉子)`

### Task 3: 组合维度输出（§5.3 契约，纯函数）

**Files:**
- Modify: `hsbot/mulligan_ai.py`（同 Task 2 段）
- Test: `tests/test_mulligan_ai.py`

**Interfaces:**
- Produces:
```python
def combined_outputs(uniq: list, gains: dict, pair_bonus: dict,
                     keep: list) -> dict:
    """2ⁿ 打分表 → 机读组合事实。返回 {"keep","marginal"{cid: s(keep)−s(keep∖c)},
    "pair_synergy"{(a,b): s(ab)−s(a)−s(b)+s(∅)}(offered 内全部 ≤10 对),
    "anti_synergy"[(a,b)](synergy < ANTI_SYNERGY_THR),
    "reject"[cid](c∉keep 且 s(keep∪{c})−s(keep)<0), "scores"{frozenset: float}}。
    打分 = Σgains + Σpair_bonus(与 best_keep_set 同一函数, 加性模型下精确)。"""
```

- [ ] **Step 1: 写失败测试**

```python
def test_combined_outputs_contract():
    import hsbot.mulligan_ai as ai
    gains = {"A": 0.10, "B": 0.05, "C": -0.20}
    syn = {("A", "C"): -0.05}                        # A+C 互相拖累
    r = ai.combined_outputs(["A", "B", "C"], gains, syn, ["A", "B"])
    assert r["keep"] == ["A", "B"]
    assert abs(r["marginal"]["A"] - (ai._set_score({"A", "B"}, gains, syn)
              - ai._set_score({"B"}, gains, syn))) < 1e-9
    assert abs(r["pair_synergy"][("A", "C")] + 0.05) < 1e-9
    assert ("A", "C") in [tuple(p) for p in r["anti_synergy"]]
    assert "C" in r["reject"]                        # 边际为负且未留

def test_combined_outputs_symmetry_and_edges():
    import hsbot.mulligan_ai as ai
    r = ai.combined_outputs(["A", "B"], {"A": 0.1, "B": -0.1},
                            {("A", "B"): 0.02}, ["A"])
    assert abs(r["pair_synergy"][("A", "B")] - 0.02) < 1e-9
    assert abs(r["pair_synergy"][("B", "A")] - 0.02) < 1e-9   # 对称
    single = ai.combined_outputs(["A"], {"A": 0.1}, {}, ["A"])
    assert single["pair_synergy"] == {} and single["anti_synergy"] == []
    assert single["marginal"] == {"A": ai._set_score({"A"}, {"A": 0.1}, {})}
```

- [ ] **Step 2: 跑失败** → **Step 3: 实现**（内部 `_set_score(s, gains, pair_bonus)`
  为 best_keep_set 的打分子表达式提取；对称化 pair 键 `tuple(sorted(p))`）
- [ ] **Step 4: 跑绿** → **Step 5: Commit** — `feat: v3 组合维度输出契约(边际/协同/不宜同留/换牌)`

### Task 4: Q 模型（trainer/qvalue.py——行构造 + OOF CV + 门控 + K=3 蒸馏标签）

**Files:**
- Create: `trainer/qvalue.py`
- Test: `tests/test_mulligan_v3.py`（新文件，v3 专测）

**Interfaces:**
- Consumes: Task 1 的 turn/decision 行；`trainer.states.flatten`；
  `trainer.mulligan.deck_features`；`hsbot.mulligan_ai.LR_AUC_GATE`；
  `trainer.backtest.group_folds`。
- Produces:
```python
def q_rows(rows_v2: list[dict], carddb, prior: dict) -> tuple[list, list, list, list, dict]:
    """→ (X, y, groups, q3_index_by_game, meta)。每回合行 1 样本;
    特征 = flatten(snap) + 前 K 回合抽牌聚合(低费法术/引擎件/曲线覆盖3位)
    + 换入牌聚合(同3项) + 对手职业 onehot + deck_features 尾段。
    q3_index_by_game = {game: 行下标}(该局 turn==3 行, 缺则 turn==2, 再缺则无键)。"""

def q_oof_fit(X, y, groups, data_dir, n_splits: int = 5) -> dict:
    """组级 CV(复用 group_folds): 每折 TabPFN fit→OOF 预测。
    → {"auc": mean|None, "aucs": [...], "oof": {样本下标: 概率}}。
    tabpfn 不可导入 → {"auc": None, "oof": {}}(调用方按门控不过处理)。"""
```

- [ ] **Step 1: 写失败测试**（合成行，不依赖 tabpfn 的部分先红）

```python
def _v2_rows_fixture():
    """两局×两回合的合成 v2 素材行(结构同 Task 1 契约)。"""
    def snap(turn, hp):
        return {"turn": turn, "my_turn": True,
                "me": {"hp": hp, "armor": 0, "hand": [], "board": [],
                       "deck": 20, "secrets": 0, "mana": turn, "mana_cap": turn,
                       "overload": 0, "fatigue": 0, "spellpower": 0,
                       "class": None},
                "opp": dict(same 结构)}
    # game g1: T2 抽 CS2_029(法术3费), g2: T3 抽 EX1_166 …(构造可控聚合)
    return [...完整两局行, decision 行带 decklist {"CS2_029": 2, "EX1_166": 2}...]

def test_q_rows_shape_and_grouping():
    from trainer.qvalue import q_rows
    X, y, groups, q3, meta = q_rows(_v2_rows_fixture(), NO_DB, {"engine": []})
    assert len(X) == len(y) == len(groups) == len(meta["names"])
    assert len(set(groups)) == 2                     # 组 = 局
    assert set(q3) <= set(groups)                    # 锚点键都是局键
    assert all(isinstance(v, float) for v in X[0])

def test_q_rows_no_future_leak_within_game():
    """Q 特征聚合只用 turn ≤ K 的抽牌: T2 行的向量不随 T3 抽牌变化。"""
    from trainer.qvalue import q_rows
    rows = _v2_rows_fixture()
    X, *_ = q_rows(rows, NO_DB, {"engine": []})
    # 改 T3 的 drawn → T2 行向量不变(同局 T2 是 X 中靠前的对应行)
    ...
    assert x_t2_before == x_t2_after

def test_q_oof_gate_semantics():
    from trainer.qvalue import q_oof_fit
    r = q_oof_fit(X, y, groups, data_dir=tmp_path)   # 无锚点差异合成数据
    if r["auc"] is not None:
        assert 0.0 <= r["auc"] <= 1.0 and set(r["oof"]) <= set(range(len(y)))
```

- [ ] **Step 2: 跑失败** — `python -m pytest tests/test_mulligan_v3.py -q`
- [ ] **Step 3: 实现 qvalue.py**（draw 聚合：carddb.cost/type，缺卡表计 0；
  engine 词表来自 prior["engine"]；onehot 类别表 = 语料内出现职业排序 +
  UNKNOWN 兜底，names 落 meta；`q_oof_fit` 内 `tabpfn_env(data_dir)` 后懒
  import tabpfn，ImportError → auc None；锚点 OOF 概率从折预测回填）
- [ ] **Step 4: 跑绿**（tabpfn 相关用例挂 `pytest.importorskip("tabpfn")`，
  环境变量隔离 data_dir=tmp_path——审计 Fix7 先例）
- [ ] **Step 5: Commit** — `feat: Q 模型(回合行样本/OOF 组级CV/K=3 锚点蒸馏标签)`

### Task 5: 评分器训练 + 组级 CV 对照表 + 后端抽象（trainer/scorer.py）

**Files:**
- Create: `trainer/scorer.py`
- Test: `tests/test_mulligan_v3.py`

**Interfaces:**
- Consumes: Task 1 决策行、Task 2 特征、Task 4 q3 标签、`backtest.group_folds`。
- Produces:
```python
def scorer_dataset(rows_v2, carddb, prior, q3: dict, q_gated: bool,
                   vocab: list, classes: list, pairs: list) -> dict:
    """每局 1 行(真实留集)。y = 0.5·result + 0.5·q3[game](q_gated 且该局有锚点)
    否则纯 result。→ {"X","y","groups","games","model"(mulligan3_features 的
    model dict, 含 deck_tail 逐局不同 → 不进 model, 单独 "tails":[...])}。"""

def cv_compare(ds: dict, data_dir, pref: str) -> tuple[dict, str, object]:
    """组级 CV 对照表: TabPFN / TabICL(可导入才跑) / 逻辑回归 / 统计表加性基线。
    → (report{"模型": {"auc": mean, "logloss": …}}, winner_backend, predict_fn)。
    winner = pref 后端若可导入, 否则另一可导入者; 都不可 → "lr"。"""

def fit_scorer(ds: dict, data_dir) -> object:
    """全量拟合 TabPFN(上下文) → predict(list[vec])→list[float]; 不可导入→None。"""

def distill(ds: dict, predict, vocab: list, pairs: list) -> tuple[dict, dict, float]:
    """对每局 × 2ⁿ 候选集: target=基座 p(候选集特征), 设计矩阵=[vocab 指示
    +pairs 指示], 纯 Python 正规方程最小二乘 → (gain{cid}, syn{"a|b"}, agree)。
    agree = top-1 加性系数集 与 top-1 基座集 一致率(组=局)。"""
```

- [ ] **Step 1: 写失败测试**

```python
def test_scorer_labels_distillation_and_degrade():
    from trainer.scorer import scorer_dataset
    ds = scorer_dataset(rows_v2, NO_DB, {"engine": []},
                        q3={"s_g01#1": 0.7}, q_gated=True, vocab=[...], ...)
    g1 = ds["games"].index("s_g01#1"); g2 = ds["games"].index("s_g02#1")
    assert abs(ds["y"][g1] - (0.5 * result1 + 0.5 * 0.7)) < 1e-9
    assert ds["y"][g2] == result2                     # 无锚点 → 纯 result

def test_scorer_labels_degrade_when_gate_fails():
    ds = scorer_dataset(..., q_gated=False, ...)
    assert all(abs(a - b) < 1e-9 for a, b in zip(ds["y"], results))

def test_distill_recovers_additive_truth():
    """合成可加真值(gain+syn)生成的打分表 → 蒸馏系数应复原且 agree==1.0。"""
    from trainer.scorer import distill
    gain_true = {"A": 0.1, "B": -0.05}
    syn_true = {"A|B": 0.03}
    predict = lambda rows: [sum(gain_true.get(c, 0) for c in s)
                            + syn_true.get("|".join(...), 0) ... for 候选集 s in rows]
    gain, syn, agree = distill(ds, predict, ["A", "B"], [["A", "B"]])
    assert abs(gain["A"] - 0.1) < 1e-6 and abs(syn["A|B"] - 0.03) < 1e-6
    assert agree == 1.0

def test_cv_compare_runs_without_tabicl():
    from trainer.scorer import cv_compare
    report, winner, predict = cv_compare(ds, data_dir=tmp_path, pref="tabpfn_v2")
    assert "统计表加性" in report and winner in ("tabpfn_v2", "lr")
```

- [ ] **Step 2: 跑失败** → **Step 3: 实现**
  - 统计表加性基线：折内训练行喂 `hsbot.mulligan_ai.table_update` →
    `card_advice` 增益作打分（折外打分候选留集总分）。
  - LR：sklearn `LogisticRegression(C=0.3)`，同 v2 惯例。
  - TabPFN/TabICL fit 函数各自懒 import + `tabpfn_env(data_dir)`；
    `scorer_backend` 名 → 实现映射 `{"tabpfn_v2": _fit_tabpfn, "tabicl_v2": _fit_tabicl}`。
  - distill 正规方程：纯 Python 高斯消元（列数 ≈ |vocab|+|pairs| ≤ 60）；
    候选集特征构造走 Task 2 `mulligan3_features`（deck_tail 用该局 tail）。
  - **纪律**：distill 的 target 是 `predict(候选集特征)`（模型自己的打分面），
    绝不出现本局真实胜负（§5.2 姊妹条款，测试即
    `test_distill_recovers_additive_truth` 的 predict 里没有 y）。
- [ ] **Step 4: 跑绿** → **Step 5: Commit** — `feat: v3 评分器训练+组级CV对照表+蒸馏系数与一致率`

### Task 6: v3 训练接线（cmd_train + _save_version + config）

**Files:**
- Modify: `trainer/mulligan.py`（`cmd_train` 尾部接 v3；`_save_version` 加 `v3` 参数）
- Modify: `hsbot/config.py`（DEFAULTS/_FLOAT 加三键）
- Modify: `hsbot/mulligan_ai.py::read_latest`（加载 `v3.json` → `art["v3"]`）
- Modify: `config.yaml`（v3 注释块）
- Test: `tests/test_mulligan_v3.py`

**Interfaces:**
- Produces: 版本目录新产物 `v3.json`：
```json
{"layout": 1, "backend": "tabpfn_v2", "vocab": [...], "classes": [...],
 "pairs": [["A","B"]], "engine": [...], "gain": {"CID": 0.03},
 "syn": {"A|B": 0.01}, "agree": 0.93, "auc": 0.61, "q_auc": 0.58,
 "q_gated": true, "distill_ok": true, "anti_thr": -0.02,
 "deck_tail": [...], "X": [[...]], "y": [...]}
```
  `distill_ok = (agree ≥ distill_min_agree) and (auc ≥ LR_AUC_GATE)`
  （AUC 门控是 spec §7"模型互不信任"哲学对 v3 的沿用，缺它坏基座可自洽过
  一致率门——写进 v3.json 与 meta 便于解释）。config 新键：
  `mulligan_v3: true / scorer_backend: tabpfn_v2 / distill_min_agree: 0.90`。

- [ ] **Step 1: 写失败测试**

```python
def test_config_v3_keys():
    from hsbot.config import Config
    cfg = Config.load({"config": "/nonexistent.yaml"})
    assert cfg.mulligan_v3 is True and cfg.scorer_backend == "tabpfn_v2"
    assert abs(cfg.distill_min_agree - 0.90) < 1e-9

def test_save_version_writes_v3_and_meta(tmp_path):
    from trainer import mulligan as M
    g = M.Game(path="s_g01", result=1, coin=False, opp_class="PRIEST",
               cards=["A"], kept=["A"], mtime=1.0, size=10)
    v3 = {"layout": 1, "distill_ok": True, "agree": 1.0, "auc": 0.6}
    M._save_version(root, "测试", {}, None, None, [g], 1, Counter(), v3=v3)
    art = json.loads((root / "v001" / "v3.json").read_text(encoding="utf-8"))
    meta = json.loads((root / "v001" / "meta.json").read_text(encoding="utf-8"))
    assert art["distill_ok"] is True and "v3" in meta

def test_save_version_without_v3_byte_identical_files(tmp_path):
    """mulligan_v3=false 等价 v3=None: v001 产物集合与 v2 时代逐字节一致。"""
    M._save_version(root, "测试", {}, None, None, [g], 1, Counter())
    assert not (root / "v001" / "v3.json").exists()

def test_read_latest_loads_v3(tmp_path): ...
```

- [ ] **Step 2: 跑失败** → **Step 3: 实现**
  - `cmd_train`：v2 管线不动；`if cfg.mulligan_v3:` →
    `from .scorer import train_v3` → `v3 = train_v3(cfg, deck, carddb, prior)`
    （内部：material_v2 陈旧性跳过——`material_v2.jsonl` mtime 新于全部切片
    则复用；qvalue → scorer → cv_compare → fit_scorer → distill；打印 §9
    对照表 + 一致率 + 门槛结论；异常/语料不足 → log.warning + v3=None，**训练
    主流程不因 v3 失败而失败**）。`_save_version(..., v3=v3)` 写 `v3.json`
    （X/y 为评分器行），meta.json 增 `"v3": {"auc","q_auc","agree","distill_ok",
    "backend"}` 摘要。
  - config 三键入 DEFAULTS/_FLOAT；config.yaml 注释块照 spec §8 措辞。
- [ ] **Step 4: 跑绿**（含全部既有测试）→ **Step 5: Commit** — `feat: v3 训练接线——cmd_train 产出 v3.json(一致率+AUC 双门控)`

### Task 7: live 接线（系数表路径 + 零变化钉子 + render 组合措辞）

**Files:**
- Modify: `hsbot/mulligan_ai.py`（`MulliganAdvisor.__init__` 加 `v3=False`；
  `advise()` v3 分支）
- Modify: `hsbot/watcher.py:262`（构造传 `v3=cfg.mulligan_v3`）
- Modify: `hsbot/render.py`（`_render_mulligan_offer` 追加 anti 措辞）
- Test: `tests/test_mulligan_ai.py`、`tests/test_mulligan_v3.py`

**Interfaces:**
- advise() 返回 dict 在 v3 生效时新增：`"scorer": "v3"`、`"v3": combined_outputs(...)
  的 keep/marginal/pair_synergy/anti_synergy/reject`（机读，键名同 §5.3）。
  优先级：v3(distill_ok) → (live) LR → 统计表；CLI 下 v3_base 见 Task 8。
  Thompson 探索路径不变（仍走统计表后验）。

- [ ] **Step 1: 写失败测试**

```python
def _write_v3(root, ver="v001", **over):
    产物五件套 + v3.json(gain/syn/distill_ok=True, vocab 覆盖 GOOD/A)

def test_advise_v3_uses_distilled_coefficients(tmp_path):
    adv = MulliganAdvisor(root, "奇迹德", NO_DB, v3=True)
    r = adv.advise(["GOOD", "A"], "PALADIN", False)
    assert r["scorer"] == "v3" and r["keep"] == <按 gain/syn 最优集>
    assert "marginal" in r["v3"] and "anti_synergy" in r["v3"]

def test_advise_v3_fallback_when_gate_fails(tmp_path):
    v3.json distill_ok=False → r["scorer"] != "v3"(回退统计表/LR)

def test_advise_v3_off_identical_to_v2(tmp_path):
    """零变化钉子: 同一产物目录, v3=False(即使 v3.json 存在)输出与无 v3.json 完全一致。"""
    r_off  = MulliganAdvisor(root, "奇迹德", NO_DB, v3=False).advise(["GOOD"], "PALADIN", False)
    del art["v3"] 后重读
    r_v2   = MulliganAdvisor(root, "奇迹德", NO_DB, v3=False).advise(["GOOD"], "PALADIN", False)
    assert json.dumps(r_off, sort_keys=True) == json.dumps(r_v2, sort_keys=True)

def test_render_v3_anti_synergy_wording():
    evt = {"advice": {基础字段 + "v3": {"anti_synergy": [["A", "B"]], ...}}}
    line = _render_mulligan_offer(evt, NO_DB)
    assert "不宜同留" in line
    evt 无 v3 → 行与 v2 渲染逐字节一致(零变化另一半)
```

- [ ] **Step 2: 跑失败** → **Step 3: 实现**
  - advise() v3 分支：`gains = {c: v3["gain"].get(c, 0.0) for c in uniq}`、
    `pair_bonus = {tuple(k.split("|")): v for k, v in v3["syn"].items()
    if 两卡 ∈ uniq}` → `keep = best_keep_set(uniq, gains, pair_bonus)` →
    `combined_outputs(uniq, gains, pair_bonus, keep)` 挂 `r["v3"]`。
  - render：`adv.get("v3", {}).get("anti_synergy")` 非空 → 追加
    `│ 不宜同留: X+Y`（carddb 名，反查失败用 cid）；无 v3 键输出零变化。
- [ ] **Step 4: 跑绿**（含 watcher 集成测试 + `python -m pytest tests/ -q` 全量）
  → **Step 5: Commit** — `feat: live 留牌 v3——蒸馏系数路径+组合措辞+零变化钉子`

### Task 8: CLI advise v3（基座全量枚举）+ 文档回填

**Files:**
- Modify: `hsbot/mulligan_ai.py`（`TabPFNWrap3`：v3 特征 fit/批量打分，懒 import）
- Modify: `trainer/mulligan.py::cmd_advise`（v3 输出段）
- Modify: `docs/MULLIGAN_AI.md`（新增 §9 v3 节）
- Test: `tests/test_mulligan_v3.py`

**Interfaces:**
- `TabPFNWrap3(payload, data_dir)`：`usable(payload, data_dir)`（distill_ok 且
  tabpfn 可导入且 X 非空）；`best_set(offered, coin, opp_class)` 基座全量枚举；
  打分批量走 `mulligan3_features`。CLI advise：`cfg.mulligan_v3` 且 usable →
  `scorer="v3_base"`，输出 §5.3 全字段（逐卡边际/逐对协同/不宜同留/换牌），
  措辞用 render 既有函数 + `combined_outputs` 数值直印。

- [ ] **Step 1: 写失败测试**（TabPFNWrap3 的 usable 门控与布局校验可无 torch
  测；best_set 用 importorskip("tabpfn") + 合成 X/y 小样本）
- [ ] **Step 2: 跑失败** → **Step 3: 实现** → **Step 4: 跑绿**
- [ ] **Step 5: 文档**：MULLIGAN_AI.md §9（架构图/门槛/config/与 v2 关系：
  v3 是 live 主路径的系数来源，统计表/LR 保留为兜底级联）；config.yaml 注释
  已在 Task 6 落。
- [ ] **Step 6: Commit** — `feat: CLI advise v3 基座全量枚举 + MULLIGAN_AI v3 文档`

### Task 9: 真实语料训练 + 端到端验收

**Files:** 无代码（产出真模型 + 验收记录进 commit message）

- [ ] **Step 1: 全量测试** — `python -m pytest tests/ -q`（207+ 全绿）
- [ ] **Step 2: 真实训练** — `python -m trainer mulligan train`（奇迹德，83 局）。
  记录：Q AUC、评分器对照表四行、蒸馏一致率、门槛结论、新版本号。
  Q/评分器 AUC < 0.55 或一致率 < 0.90 → 按设计诚实降级，如实记录（不调参凑数）。
- [ ] **Step 3: CLI advise 冒烟** — `python -m trainer mulligan advise --vs 牧师 --coin
  --hand <真实起手>`：确认组合字段输出与回退路径。
- [ ] **Step 4: live 回放冒烟** — `run_replay` 一个真实切片（mute 模式，不弹窗），
  确认 mulligan_offer 行渲染含 v3 措辞、无异常刷屏。
- [ ] **Step 5: Commit** — `chore: v3 真实语料训练验收(一致率 x.xx / Q AUC x.xx)`（模型产物在 data/，按 .gitignore 现状处理）

---

## Self-Review 结论

- 覆盖 spec §3（Task 1）、§4（Task 4）、§5（Task 2/3/5）、§6（Task 5/6/7）、
  §7（Task 5/6 后端抽象与级联）、§8（Task 6 config）、§9（Task 5 对照表 +
  Task 9 验收）、§10 测试矩阵逐行有落点、§11 切片 1-7 对应 Task 1-8。
- spec §5.4 附录纪律：反事实标签复制无实现路径（Task 5 distill 的 target 只来自
  基座打分面，测试钉死）。
- 类型一致性：`combined_outputs`/`mulligan3_features`/`v3.json` 键名在 Task 2/3/5/6/7
  间已互相对齐（model dict 键 vocab/classes/pairs/engine/deck_tail/cost/cardtype/
  deck_engine）。
