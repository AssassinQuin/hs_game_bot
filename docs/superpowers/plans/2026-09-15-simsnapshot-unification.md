# SimSnapshot 统一快照层实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把分裂的模拟状态统一为一等 `SimSnapshot`（类改名+4 个游戏事实字段+`advance_turn`/`memo_key` 转移函数），live 斩杀线装配抽成 `assemble_snapshot`，并落地预测-实测对账通道（recon diff 纯函数 + watcher AuditExporter + JSONL + CLI 汇总）。

**Architecture:** planner 侧 simstate 演进（模块名不动、类 `SimState`→`SimSnapshot`），dfs.best_line 从快照读 board_atk/enemy_total 并统一斩杀算式，rollout 的 5 个散装循环变量全部寄居快照；hsbot 侧 lethal_plan 的 ~70 行内联投影抽成模块级 `assemble_snapshot`，新增 `recon.py` 纯函数对账与 watcher `AuditExporter`（与 TrainingExporter/SnapshotService 同型）落盘 `data/logs/sim_divergence.jsonl`。

**Tech Stack:** 纯 Python 标准库（dataclasses/bisect/hashlib/json/collections），无新增依赖。

**Spec:** [SimSnapshot 统一设计](../specs/2026-09-15-simsnapshot-unification-design.md)（§2 数据模型 / §3 转移函数 / §4 装配迁移 / §5 对账通道 / §6 铁律 / §7 成功标准 / §8 测试矩阵 / §10 切片）。

## Global Constraints

- **开工前置条件**：工作区干净。2026-09-15 深夜实测有**并发 P1 会话**在改 `planner/rollout.py`/`tests/test_rollout.py`（前置过滤+截断，未提交）——本计划 Task 3 的基线即该 163 行版本（含 `_face_ceiling`/`_drawable`/`LAUNCH_DRAWS_CAP`，全部原样保留）；若开工时该流仍未提交，先等它落地或请用户裁决，**不得覆盖**。
- **公共契约不变**（spec §6.1）：`Plan`、`RolloutResult` 既有字段（launch_turn/turns/hand_sizes/engines）、`sim_mulligan`/`combo_outputs`/simcal 接口全不动；`RolloutResult` 只**追加** `snapshots` 字段（排最后、带默认值）。
- **live 斩杀线输出零变化**（spec §6.2）：test_lethal_plan 既有**断言**一字不改（桩模块签名随接线同步是允许的夹具改动）；装配侧等价性 = 同输入喂给 best_line 的快照字段逐项相同（face=0/dealt_total=0）。
- **纯重构可证**（spec §7）：rollout 迁移前后，同 seed 同输入的 `launch_turn/hand_sizes/engines` **逐点相同**（Task 3 有 before/after dump 对照步骤）；同 seed simcal 合成校准逐点一致。
- **planner 依赖铁律**（spec §6.3）：planner 不 import store/watcher（除 `hsbot.consts`）；SimSnapshot 纯数据可哈希；新代码无 IO/全局态，随机性只经调用方。
- **对账零干扰**（spec §5/§6.5）：AuditExporter 任何异常只记 WARNING 不阻断主链；一致时零输出；`recon.py` 的 diff 函数零 IO（CLI main 例外，只在 `__main__` 入口读文件）。
- **本计划明确不做**（spec §9）：不改 effects 语法表/Piece 语义、不做自动改规则、不对齐 store 快照 schema、不做对手建模、不动 rollout v1 简化口径与 simcal 三层校准口径、**P2 启动谓词重设计不在此列**（另立）。
- 每个任务结束全测试套绿（`python3 -m pytest tests/ -q`），提交信息沿用仓库中文风格（`feat:`/`test:`/`docs:`/`refactor:` 前缀）。测试不弹 UI/不抢前台（Config.load 覆盖 overlay_enabled=False）。
- **口径澄清（spec 行文 vs 代码现实，以代码为准的两处）**：① spec §2 列"现有 9 字段"漏了 `disc_next_cat`（2026-09-14 减费类目裁决新增），SimSnapshot 必须保留；② spec §3 说 memo_key 是"七元组"，实际是含 `disc_next_cat` 的 8 元投影——`disc_next` 同值不同类目时后续减费作用不同，`disc_next_cat` 不进键会撞错误坍缩。

## File Structure

| 文件 | 职责 |
|---|---|
| Modify `planner/simstate.py` | 类改名 SimSnapshot + turn/enemy_total/board_atk/dealt_total 字段 + `advance_turn` + `memo_key`；play() 热路径透传新字段 |
| Modify `planner/dfs.py` | best_line 签名简化（去 board_atk/enemy_total kwargs）+ 统一斩杀算式 + memo_key 接线 |
| Modify `planner/rollout.py` | 5 个散装循环变量消失，快照序列推进；P1 前置过滤/截断原样保留；`snapshots` 派生字段 |
| Modify `planner/mc.py` | SimState→SimSnapshot import；best_line 调用改 replace 投影（2 处） |
| Modify `trainer/simcal.py` | first_lethal_turns 快照带 enemy_total（1 处调用迁移） |
| Modify `hsbot/analysis.py` | 新增模块级 `assemble_snapshot`；lethal_plan 薄化 |
| Create `hsbot/recon.py` | 对账 diff 纯函数（reconcile/ir_source_hash/summarize）+ CLI main |
| Modify `hsbot/watcher.py` | AuditExporter 类 + PLAY 块开始基态捕获 + play 事件对账 + 新局 reset |
| Create `tests/test_snapshot.py` | SimSnapshot/advance_turn/memo_key/play 透传 单测 |
| Create `tests/test_recon.py` | reconcile 三类分歧 fixture + AuditExporter 端到端 + 一致零输出 |
| Modify `tests/test_planner.py` | best_line 调用点 kwargs → 快照字段（~8 处，断言零改动） |
| Modify `tests/test_lethal_plan.py` | 桩模块签名同步（initial_state 新 kwargs、best_line 去 kwargs、记录改读 state） |
| Modify `docs/ARCHITECTURE.md` | 两通道架构总纲增补 |

---

### Task 1: SimSnapshot 数据模型 + advance_turn + memo_key（纯增）

**Files:**
- Modify: `planner/simstate.py`（类定义 14-40 行区、play 热路径 110-119 行区、模块尾追加）
- Modify: `planner/mc.py:68`（import 行）
- Test: `tests/test_snapshot.py`（创建）

**Interfaces:**
- Produces:
  - `SimSnapshot`（frozen dataclass，字段序：`turn, mana, hand, sp, disc_hand, disc_next, disc_next_cat="", engines=0, drawn=0, known_draws=(), face=0, enemy_total: int|None=None, board_atk=0, dealt_total=0`）
  - `initial_state(mana, hand_cards, sp, known_draws=(), disc_hand=0, disc_next=0, engines=0, disc_next_cat="", *, turn=1, enemy_total=None, board_atk=0, dealt_total=0) -> SimSnapshot`（新 4 参 keyword-only，旧调用全部兼容）
  - `advance_turn(snap, *, enemy_total) -> SimSnapshot`
  - `memo_key(snap) -> tuple`（8 元投影）
  - 后续任务的 rollout/dfs/装配全部依赖。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_snapshot.py`：

```python
"""SimSnapshot 统一快照层(spec §2/§3): 新字段/advance_turn/memo_key/play 透传。"""
import pytest

from planner.pieces import Piece
from planner.simstate import (SimSnapshot, advance_turn, initial_state,
                              memo_key, play)


def _snap(**kw):
    base = dict(mana=5, hand=(("MOON", 1),), sp=0)
    base.update(kw)
    return initial_state(base.pop("mana"), base.pop("hand"), base.pop("sp"), **kw)


def test_snapshot_new_fields_default_and_hashable():
    s = initial_state(5, (("MOON", 1), ("BIG", 4)), 1)
    assert isinstance(s, SimSnapshot)
    assert (s.turn, s.enemy_total, s.board_atk, s.dealt_total) == (1, None, 0, 0)
    assert hash(s) == hash(initial_state(5, (("MOON", 1), ("BIG", 4)), 1))
    s2 = initial_state(5, (("MOON", 1),), 0, turn=3, enemy_total=26,
                       board_atk=4, dealt_total=7)
    assert (s2.turn, s2.enemy_total, s2.board_atk, s2.dealt_total) == (3, 26, 4, 7)


def test_advance_turn_accumulates_face_into_dealt_and_resets():
    s = _snap(hand=(("MOON", 1), ("BIG", 4)), turn=3, enemy_total=30,
              dealt_total=5, face=4)
    nxt = advance_turn(s, enemy_total=28)
    assert nxt.turn == 4 and nxt.mana == 4            # min(10, 新回合)
    assert nxt.dealt_total == 9 and nxt.face == 0     # 5+4 累计, 本回合清零
    assert nxt.enemy_total == 28                      # 逐回合数据替换(None 也合法)
    assert advance_turn(s, enemy_total=None).enemy_total is None


def test_advance_turn_draws_known_head_from_t2_and_sorts():
    s = _snap(hand=(("BIG", 4),), known_draws=(("A", 2), ("B", 1)), turn=1)
    nxt = advance_turn(s, enemy_total=30)             # 1→2: 自然抽 1
    assert nxt.hand == (("A", 2), ("BIG", 4))         # 队头入手且升序规范化
    assert nxt.known_draws == (("B", 1),)
    assert nxt.drawn == 0


def test_advance_turn_empty_known_counts_drawn():
    s = _snap(turn=4)
    nxt = advance_turn(s, enemy_total=30)
    assert nxt.hand == s.hand and nxt.drawn == 1


def test_advance_turn_clears_disc_next_keeps_persistent_fields():
    s = _snap(disc_next=3, disc_next_cat="星灵", disc_hand=2, sp=1, engines=2,
              known_draws=(("A", 2),))
    nxt = advance_turn(s, enemy_total=None)
    assert (nxt.disc_next, nxt.disc_next_cat) == (0, "")   # 一次性余额随回合过期
    assert (nxt.disc_hand, nxt.sp, nxt.engines) == (2, 1, 2)


def test_memo_key_is_transition_projection():
    s = _snap(hand=(("BIG", 4), ("MOON", 1)), disc_next=3, disc_next_cat="法术",
              known_draws=(("A", 2),))
    assert memo_key(s) == (5, (("BIG", 4), ("MOON", 1)), 0, 0, 3, "法术", 0,
                          (("A", 2),))
    # 判定/累计量不进键(后缀不变性): face/drawn/turn/enemy_total/board_atk/dealt_total
    for other in (_snap(face=9), _snap(turn=7), _snap(enemy_total=26),
                  _snap(board_atk=3), _snap(dealt_total=11)):
        assert memo_key(_snap()) == memo_key(other) or memo_key(other) != memo_key(s)


def test_play_passes_new_fields_through():
    pieces = {("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True)}
    s = _snap(hand=(("COIN", 0), ("MOON", 1)), turn=2, enemy_total=26,
              board_atk=4, dealt_total=5)
    nxt = play(s, 0, pieces)
    assert (nxt.turn, nxt.enemy_total, nxt.board_atk, nxt.dealt_total) == (2, 26, 4, 5)
    assert nxt.mana == 6                             # 5-0+1
```

注意 `test_memo_key_is_transition_projection` 最后的 for 断言写法是"同键佣缩"的正面表达——简化为直白形式（避免恒真式）：

```python
def test_memo_key_equal_across_bookkeeping_fields():
    mk = memo_key(_spawn := _snap(hand=(("MOON", 1),), known_draws=(("A", 2),)))
    assert mk == memo_key(_snap(hand=(("MOON", 1),), known_draws=(("A", 2),),
                                face=9, drawn=3, turn=7, enemy_total=26,
                                board_atk=3, dealt_total=11))
```

（执行时用这个直白版本替换 for 循环那两行。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_snapshot.py -v`
Expected: FAIL — `ImportError: cannot import name 'SimSnapshot'`。

- [ ] **Step 3: 实现 planner/simstate.py**

3a. 模块 docstring 首行 `SimState 不可变状态` 改为 `SimSnapshot 不可变快照`，其余保留。类定义替换为（`initial_state` 同步）：

```python
@dataclass(frozen=True)
class SimSnapshot:
    """决策点/推演快照(全 tuple/frozen, 可哈希; dfs.memo 以 memo_key 的
    投影为键, 见 planner/dfs.py 模块注释的后缀不变性)。

    turn/enemy_total/board_atk/dealt_total 是统一快照层新增的游戏事实:
    turn 推演期只增; enemy_total 本回合敌方有效血甲(live=store; sim=曲线/
    档位表; None=无数据, 消费方跳过斩杀/启动判定); board_atk 我方可打脸
    场攻; dealt_total 跨回合累计已造成确定伤害。"""
    turn: int
    mana: int
    hand: tuple                  # ((card_id, cost), ...) 升序排序规范化
    sp: int                      # 当前法强
    disc_hand: int               # 在身的手牌法术减费余额(每张法术都减, 不耗尽)
    disc_next: int               # "下一张"减费面值(一次性: 全额作用下一张, 用后清零)
    disc_next_cat: str = ""      # 该余额的类目词(星灵/法术/…; 空=不限类目):
                                 # 类目不符或有剩余费的牌才作用并消耗
                                 # (2026-09-14 用户裁决"0 水晶不需要")
    engines: int = 0             # 在场施法抽牌引擎数
    drawn: int = 0               # 已消耗的未知抽牌数(Plan.face_exp 期望折算用)
    known_draws: tuple = ()      # ((cid, cost), ...) 队头=最先抽到(消耗式)
    face: int = 0                # 已累计确定伤害
    enemy_total: int | None = None   # 本回合敌方有效血甲; None=无数据
    board_atk: int = 0           # 我方场面可打脸攻击
    dealt_total: int = 0         # 跨回合累计已造成确定伤害


def initial_state(mana: int, hand_cards, sp: int, known_draws=(), disc_hand: int = 0,
                  disc_next: int = 0, engines: int = 0, disc_next_cat: str = "",
                  *, turn: int = 1, enemy_total: int | None = None,
                  board_atk: int = 0, dealt_total: int = 0) -> SimSnapshot:
    """构造初始快照: hand_cards=((cid, cost), ...), 手牌升序排序规范化;
    known_draws 保持调用方给的队列序(队头=最先抽到, 不排序);
    turn/enemy_total/board_atk/dealt_total 为统一快照层的游戏事实
    (live 装配=store 投影; sim 装配=rollout 抽样构造)。"""
    return SimSnapshot(turn=turn, mana=mana, hand=tuple(sorted(hand_cards)),
                       sp=sp, disc_hand=disc_hand, disc_next=disc_next,
                       disc_next_cat=disc_next_cat, engines=engines, drawn=0,
                       known_draws=tuple(known_draws), face=face_total := 0,
                       enemy_total=enemy_total, board_atk=board_atk,
                       dealt_total=dealt_total)
```

（执行注意：`face=face_total := 0` 是笔误示例——直接写 `face=0`。）

3b. `play()` 热路径的 `st.__dict__.update(...)` 块整体替换（docstring 追加一行"turn/enemy_total/board_atk/dealt_total 原样透传(play 不读不写)"）：

```python
    st = SimSnapshot.__new__(SimSnapshot)
    st.__dict__.update(turn=state.turn,
                       mana=min(10, state.mana - eff + p.mana_gain), hand=hand,
                       sp=state.sp + p.spellpower_gain,
                       disc_hand=state.disc_hand + p.discount_hand,
                       disc_next=disc_next, disc_next_cat=disc_next_cat,
                       engines=state.engines + (1 if p.engine else 0),
                       drawn=drawn, known_draws=known,
                       face=state.face + sum((b + state.sp) if p.spell_scaled
                                             else b for b in p.segments),
                       enemy_total=state.enemy_total,
                       board_atk=state.board_atk,
                       dealt_total=state.dealt_total)
    return st
```

3c. 模块尾追加：

```python
def advance_turn(snap: SimSnapshot, *, enemy_total: int | None) -> SimSnapshot:
    """回合推进(spec §3): turn+1; mana=min(10, 新回合); face 累入 dealt_total
    后清零; T≥2 自然抽 1(known 队头入手, 空则 drawn+1); disc_next 余额随回合
    过期清零(类目词一并); sp/disc_hand/engines/known_draws 跨回合保留
    (口径=rollout 现行 v1)。enemy_total=None 即该回合无数据。"""
    turn = snap.turn + 1
    hand, known, drawn = snap.hand, snap.known_draws, snap.drawn
    if turn >= 2:
        if known:
            hand = tuple(sorted(hand + (known[0],)))
            known = known[1:]
        else:
            drawn += 1
    return SimSnapshot(turn=turn, mana=min(10, turn), hand=hand, sp=snap.sp,
                       disc_hand=snap.disc_hand, disc_next=0, disc_next_cat="",
                       engines=snap.engines, drawn=drawn, known_draws=known,
                       face=0, enemy_total=enemy_total,
                       board_atk=snap.board_atk,
                       dealt_total=snap.dealt_total + snap.face)


def memo_key(snap: SimSnapshot) -> tuple:
    """决策点键: 快照的转移相关投影(后缀不变性论证见 planner/dfs.py)。

    turn/enemy_total/board_atk/dealt_total/face/drawn 不进键 —— 后续转移
    不读它们(结算/判定只在根上取值); disc_next_cat 必须进键: 同 disc_next
    不同类目 → 后续减费作用不同, 漏进会撞错误坍缩。"""
    return (snap.mana, snap.hand, snap.sp, snap.disc_hand, snap.disc_next,
            snap.disc_next_cat, snap.engines, snap.known_draws)
```

3d. `planner/mc.py:68`：`from .simstate import SimState` → `from .simstate import SimSnapshot`，函数注解 `state: SimState`（2 处）→ `SimSnapshot`。

- [ ] **Step 4: 跑测试确认通过（含回归）**

Run: `python3 -m pytest tests/test_snapshot.py tests/test_planner.py tests/test_lethal_mc.py tests/test_rollout.py -q`
Expected: 全 PASS（旧套绿=类改名与新字段默认值零破坏的背书）。

- [ ] **Step 5: 提交**

```bash
git add planner/simstate.py planner/mc.py tests/test_snapshot.py
git commit -m "feat: SimSnapshot 统一快照——类改名+turn/enemy_total/board_atk/dealt_total 字段+advance_turn+memo_key(纯增)"
```

---

### Task 2: dfs.best_line 签名简化 + 统一斩杀算式 + 全调用方迁移

**Files:**
- Modify: `planner/dfs.py`（签名 25-33、lethal 判定 79-84、_key 87-91、模块注释）
- Modify: `planner/mc.py:152-154, 181`（2 处调用）
- Modify: `trainer/simcal.py:76-79`（1 处调用）
- Modify: `hsbot/analysis.py:309-326`（lethal_plan 装配调用，临时就地构造；Task 4 再抽 assemble）
- Modify: `tests/test_planner.py`（~8 处调用点）
- Modify: `tests/test_lethal_plan.py:81-106`（桩签名）
- Test: `tests/test_planner.py`（追加统一算式用例）

**Interfaces:**
- Produces: `best_line(state, pieces, *, exp_per_draw: float = 0.0) -> Plan`——board_atk/enemy_total/dealt_total/face 全部从快照读；斩杀统一算式 `lethal ⇔ face_det + board_atk ≥ enemy_total − dealt_total − face`。Task 3/4/5 与全部测试依赖此签名。

- [ ] **Step 1: 写失败测试**

`tests/test_planner.py` 追加（沿用该文件现有 Piece 手搓/initial_state 模式）：

```python
# ---------------- 统一斩杀算式(SimSnapshot §3) ----------------

def test_best_line_unified_lethal_absorbs_face_and_dealt():
    """face_det+board_atk ≥ enemy_total−dealt_total−face: 快照已造成的
    本回合 face 与跨回合 dealt_total 都是已扣减量 —— rollout 旧
    replace(face=0) 技巧被吸收。"""
    pieces = {("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                                is_spell=True)}
    st = initial_state(4, (("BIG", 4),), 0, enemy_total=10, board_atk=0,
                       dealt_total=3, face=1)
    plan = best_line(st, pieces)                 # 线伤 6: 6 ≥ 10-3-1=6
    assert plan.lethal is True and plan.total == 6
    st2 = initial_state(4, (("BIG", 4),), 0, enemy_total=11, dealt_total=3,
                        face=1)
    assert best_line(st2, pieces).lethal is False  # 6 < 11-3-1=7


def test_best_line_board_atk_from_snapshot_and_none_enemy():
    pieces = {("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                                is_spell=True)}
    st = initial_state(4, (("BIG", 4),), 0, enemy_total=6, board_atk=2)
    plan = best_line(st, pieces)                 # 6+2 ≥ 6
    assert plan.lethal is True and plan.board_atk == 2 and plan.total == 8
    stn = initial_state(4, (("BIG", 4),), 0)     # enemy_total=None
    pn = best_line(stn, pieces)
    assert pn.lethal is False and pn.enemy_total is None
```

同时把 `tests/test_lethal_plan.py` 桩改为（替换 81-106 行区，断言零改动）：

```python
    m_sim.initial_state = (lambda mana, hand_cards, sp, known_draws=(), disc_hand=0,
                           disc_next=0, engines=0, disc_next_cat="", *, turn=1,
                           enemy_total=None, board_atk=0, dealt_total=0:
                           SimpleNamespace(
                               mana=mana, hand=tuple(sorted(hand_cards)), sp=sp,
                               known_draws=tuple(known_draws), engines=engines,
                               disc_hand=disc_hand, disc_next=disc_next,
                               turn=turn, enemy_total=enemy_total,
                               board_atk=board_atk, dealt_total=dealt_total))

    def best_line(state, pieces, *, exp_per_draw=0.0):
        calls.append({"state": state, "pieces": pieces,
                      "board_atk": state.board_atk,
                      "enemy_total": state.enemy_total,
                      "exp_per_draw": exp_per_draw,
                      "engines": getattr(state, "engines", None)})
        mana, sp, face, actions, board_atk = state.mana, state.sp, 0, [], state.board_atk
        for cid, cost in state.hand:
            piece = pieces.get((cid, cost))
            if piece is None or cost > mana:         # 缺键=惰性牌(仅费), 不可支付跳过
                continue
            mana -= cost
            actions.append((cid, cost))
            face += piece.damage_at(sp)
        total = face + board_atk
        need = None if state.enemy_total is None else (
            state.enemy_total - state.dealt_total - state.face)
        return SimpleNamespace(
            actions=tuple(actions), total=total, face_det=face,
            face_exp=int(round(exp_per_draw)),
            lethal=need is not None and total >= need,
            enemy_total=state.enemy_total, board_atk=board_atk, uncovered_n=0,
            mana_trace=())
```

（桩内 `state.face`/`state.dealt_total` 由上面的 SimpleNamespace 提供。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_planner.py tests/test_lethal_plan.py -q`
Expected: FAIL — 新用例 `TypeError: best_line() got an unexpected keyword argument`（新用例传的 enemy_total/board_atk/dealt_total 还不被 initial_state 接受，或 best_line 仍要求旧签名路径）；test_lethal_plan 因 lethal_plan 仍传旧 kwargs 也可 FAIL。

- [ ] **Step 3: 实现**

3a. `planner/dfs.py`：

- import 行改：`from .simstate import memo_key, play`
- 模块注释第 5-8 行的"七元组"措辞更新为"投影(memo_key, 含 disc_next_cat)"，补一句"统一斩杀算式吸收 rollout 的 face 清零技巧"。
- 签名与 lethal 段替换：

```python
def best_line(state, pieces: dict, *, exp_per_draw: float = 0.0) -> Plan:
    """从 state 出发搜索最优线, 返回 Plan(机读事实, 措辞零)。

    board_atk/enemy_total/dealt_total 从快照读(SimSnapshot 统一); exp_per_draw
    只进期望注记(face_exp), 不参与分支裁剪(见模块注释)。斩杀统一算式:
    lethal ⇔ face_det + board_atk ≥ enemy_total − dealt_total − face。"""
    memo: dict = {}                  # memo_key 决策点 -> (后缀伤害, 后续动作)

    def search(st):
        key = memo_key(st)
        ...（search 体内 `_key(st)` 同步改 `memo_key(st)`，其余不动）
```

- 尾段替换：

```python
    total = face + state.board_atk
    need = None if state.enemy_total is None else (
        state.enemy_total - state.dealt_total - state.face)
    return Plan(actions=actions, total=total, face_det=face,
                face_exp=face_exp,
                lethal=(need is not None and total >= need),
                enemy_total=state.enemy_total, uncovered_n=uncovered,
                board_atk=state.board_atk, mana_trace=tuple(trace))
```

- 删除 `_key` 函数（被 simstate.memo_key 取代）。

3b. `planner/plan.py` 的 `lethal` 字段注释更新：`enemy_total 不为 None 且 face_det + board_atk ≥ enemy_total − dealt_total − face(统一斩杀算式)`。

3c. `planner/mc.py` 两处调用：

```python
        plan = best_line(replace(state, hand=tuple(sorted(hand)),
                                 known_draws=queue, board_atk=board_atk,
                                 enemy_total=enemy_total),
                         pieces)
```

```python
    plan = best_line(replace(state, board_atk=board_atk, enemy_total=enemy_total),
                     pieces)
```

（`replace` 已 import；mc_plan 自身的 `killed`/`p_lethal` 用 plan.total 与本函数入参 enemy_total 比较，语义不变。）

3d. `trainer/simcal.py` first_lethal_turns：

```python
        st = initial_state(me["mana"], hand, sp=me.get("spellpower", 0),
                           engines=engines, enemy_total=opp["hp"] + opp["armor"])
        if best_line(st, pieces).lethal:
            out[g] = snap["turn"]
```

3e. `hsbot/analysis.py` lethal_plan 装配调用（Task 4 再抽函数，本任务只改调用形态）：

```python
    plan = best_line(
        initial_state(st.mana_now(me), tuple(sorted(hand)), sp,
                      known_draws=tuple(known_draws), engines=engines,
                      turn=st.friendly_turn_number(), enemy_total=enemy_total,
                      board_atk=st.board_face_attack(me)),
        pieces, exp_per_draw=exp_per_draw)
```

（`enemy_total` 变量在上文已算好；`opp`/`enemy_total` 原行保留。）

3f. `tests/test_planner.py` 调用点迁移模式（**断言零改动**，文件头补 `from dataclasses import replace` 若无）：
- `best_line(st, pieces, board_atk=3, enemy_total=3)` → `best_line(replace(st, board_atk=3, enemy_total=3), pieces)`（st 由 initial_state 构造的场景改为在 initial_state 里直接传 `enemy_total=…, board_atk=…` 亦可，择一即可）
- `best_line(initial_state(2, (...), 0), pieces, enemy_total=6)` → `best_line(initial_state(2, (...), 0, enemy_total=6), pieces)`
- 343 行暴力交叉验证循环里的随机 board_atk 用 `replace(state, board_atk=rng.randint(0, 3), enemy_total=...)`。
逐处照此模式改完（约 8 处；纯调用形态，不改任何 assert）。

- [ ] **Step 4: 跑测试确认通过（全套）**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿（含 test_lethal_plan 全部既有断言、test_lethal_mc、test_rollout——rollout 内部调用在本任务同步迁移为其自身 replace 形态：`best_line(dataclasses.replace(st, face=0, known_draws=known), pieces, enemy_total=need)` → `best_line(dataclasses.replace(st, face=0, known_draws=known, enemy_total=need), pieces)`，Task 3 再整体重构）。

**执行注**：rollout.py 当前的 best_line 调用（155-157 行）在本任务必须一并迁移（否则套件红），按上行括号内的形态改。

- [ ] **Step 5: 提交**

```bash
git add planner/dfs.py planner/plan.py planner/mc.py planner/rollout.py trainer/simcal.py hsbot/analysis.py tests/test_planner.py tests/test_lethal_plan.py
git commit -m "refactor: best_line 从快照读 board_atk/enemy_total——统一斩杀算式吸收 face 清零技巧; 全调用方迁移(断言零改动)"
```

---

### Task 3: rollout 迁移到快照序列（5 个循环变量消失）

**Files:**
- Modify: `planner/rollout.py`（整文件重写主循环区；P1 的 `_face_ceiling`/`_drawable`/`LAUNCH_DRAWS_CAP`/cap 逻辑原样保留）
- Test: `tests/test_rollout.py`（追加 snapshots 字段用例；既有用例是逐点等价的主证明，零改动）

**Interfaces:**
- Consumes: Task 1 `advance_turn`/`initial_state(turn=…, enemy_total=…)`、Task 2 `best_line(snap, pieces)`。
- Produces: `RolloutResult(launch_turn, turns, hand_sizes=(), engines=0, snapshots=())`——`snapshots` 为各回合策略收尾后的快照序列（launch 回合含在内）；`rollout` 签名不变（`launch_draws_cap` 保留）。simcal/trainer.sim 零改动。

- [ ] **Step 1: 迁移前基准 dump（纯重构可证的"前"侧）**

```bash
python3 - <<'EOF'
import json, random
from planner.pieces import Piece
from planner.rollout import rollout
pieces = {
    ("MOON", 1): Piece("MOON", 1, segments=(1,), spell_scaled=True, is_spell=True),
    ("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True, is_spell=True),
    ("AUCTION", 5): Piece("AUCTION", 5, engine=True),
    ("DRAW2", 3): Piece("DRAW2", 3, is_spell=True, draw_n=2),
    ("INNERVATE", 0): Piece("INNERVATE", 0, mana_gain=1, is_spell=True),
    ("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True),
}
cost = {"MOON": 1, "BIG": 4, "AUCTION": 5, "DRAW2": 3, "INNERVATE": 0, "COIN": 0}
deck = {k: 4 for k in cost}
full = [c for c, n in deck.items() for _ in range(n)]
out = []
for seed in range(20):
    order = tuple(random.Random(seed).sample(full, len(full)))
    for keep in ((), ("AUCTION",), ("BIG",), ("AUCTION", "BIG")):
        for coin in (False, True):
            r = rollout(order, offered=("AUCTION", "BIG", "MOON"),
                        keep=keep, coin=coin, pieces=pieces, cost_of=cost,
                        k_max=8, enemy_totals=(None, 30, 28, 26, 24, 22, 20, 18))
            out.append([r.launch_turn, list(r.hand_sizes), r.engines])
open("data/logs/rollout_equiv_before.json", "w").write(json.dumps(out))
print("dumped", len(out))
EOF
```

（`data/logs/` 不入库，仅本机对照用；若目录不存在先建。）

- [ ] **Step 2: 写失败测试（snapshots 新字段）**

`tests/test_rollout.py` 追加：

```python
def test_rollout_snapshots_sequence_contract():
    """SimSnapshot 统一(spec §4): 每回合策略收尾后快照入序列, 公共契约
    字段(launch_turn/hand_sizes/engines)与快照序列自洽。"""
    order = ("AUCTION",) + ("MOON",) * 7
    r = rollout(order, offered=("AUCTION", "MOON"), keep=("AUCTION",),
                coin=False, pieces=_sim_pieces(), cost_of=_COST, k_max=6,
                enemy_totals=(None, None, None, None, None, 6))
    assert len(r.snapshots) == r.turns
    assert [s.turn for s in r.snapshots] == list(range(1, r.turns + 1))
    assert [len(s.hand) for s in r.snapshots] == list(r.hand_sizes)
    assert r.snapshots[-1].engines == r.engines
    # 无启动全程推演: snapshots 长度 = k_max
    r2 = rollout(("MOON",), offered=("MOON",), keep=("MOON",), coin=False,
                 pieces=_sim_pieces(), cost_of=_COST, k_max=3,
                 enemy_totals=(30, 30, 30))
    assert len(r2.snapshots) == 3
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: 新用例 FAIL — `RolloutResult` 无 `snapshots` 字段。

- [ ] **Step 4: 实现（rollout 主循环重写）**

`RolloutResult` 增字段（排最后）：

```python
@dataclasses.dataclass(frozen=True)
class RolloutResult:
    launch_turn: int | None            # 首个启动回合; 未启动 None
    turns: int                         # 推演到的回合数
    hand_sizes: tuple[int, ...] = ()   # 各回合策略收尾后手牌数(轨迹层校准用)
    engines: int = 0                   # 终态在场引擎数
    snapshots: tuple = ()              # 各回合策略收尾后的 SimSnapshot(spec §4)
```

主循环区（从 `engines = sp = disc_hand = 0` 到 return）整体替换：

```python
    snap = initial_state(
        1, tuple((c, cost_of[c]) for c in hand), 0,
        known_draws=tuple((c, cost_of[c]) for c in stream),
        enemy_total=enemy_totals[0])
    hand_sizes: list[int] = []
    snapshots: list = []
    for t in range(1, k_max + 1):
        if t >= 2:
            # v1 口径(spec §9 不改): 血甲曲线是逐回合经验血甲(分布已含真实
            # 伤害), 模拟侧跨回合已打伤害不得再扣(双计) —— dealt_total 逐回
            # 合清零, 等价旧实现"face 不跨回合携带"; 本回合 face 仍进统一算式
            snap = dataclasses.replace(
                advance_turn(snap, enemy_total=enemy_totals[t - 1]),
                dealt_total=0)
        while True:                          # 策略出牌: 打到打不动为止
            played = False
            for i, key in enumerate(snap.hand):
                if _policy_playable(key, pieces, snap.engines):
                    try:
                        snap = play(snap, i, pieces)
                        played = True
                        break
                    except ValueError:       # 不可支付: 试下一张
                        continue
            if not played:
                break
        hand_sizes.append(len(snap.hand))
        snapshots.append(snap)
        if snap.enemy_total is not None:
            # P1 前置(原样保留): 封闭回合手牌上界(可抽回合连 cap 内前缀一起
            # 计)够不到血甲 → 免 DFS; 可抽回合 known_draws 截断到 cap
            if _drawable(snap, pieces):
                known = snap.known_draws[:cap]
                ceiling = _face_ceiling(snap.sp, snap.hand + known, pieces)
            else:
                known = ()
                ceiling = _face_ceiling(snap.sp, snap.hand, pieces)
            if ceiling >= snap.enemy_total - snap.face:   # need(dealt 恒 0)
                # 统一斩杀算式吸收旧 replace(face=0) 技巧: best_line 从快照
                # 读 enemy_total/face, 只算手牌剩余爆发
                plan = best_line(dataclasses.replace(snap, known_draws=known),
                                 pieces)
                if plan.lethal:
                    return RolloutResult(t, t, tuple(hand_sizes), snap.engines,
                                         tuple(snapshots))
    return RolloutResult(None, k_max, tuple(hand_sizes), snap.engines,
                         tuple(snapshots))
```

（import 行改 `from .simstate import advance_turn, initial_state, play`；`keep/stream/fill` 头部段落、`enemy_totals` 长度守卫、`cap` 行全部不动；模块 docstring 补一段"快照序列推进：5 个散装循环变量(hand/stream/engines/sp/disc_hand)全部寄居 SimSnapshot"。）

- [ ] **Step 5: 跑测试确认通过 + 逐点等价对照**

Run: `python3 -m pytest tests/test_rollout.py tests/test_mulligan_v3.py tests/test_trainer.py -q`
Expected: 全 PASS（既有 v3.1 ×5 + P1 ×6 用例零改动通过 = launch_turn/hand_sizes/engines 逐点等价的主证明）。

再跑 Step 1 的同脚本，输出写 `data/logs/rollout_equiv_after.json`，然后：

```bash
python3 -c "import json; a=json.load(open('data/logs/rollout_equiv_before.json')); b=json.load(open('data/logs/rollout_equiv_after.json')); assert a==b, 'NOT point-wise equal'; print('point-wise equal:', len(a), 'cases')"
```

Expected: `point-wise equal: 160 cases`。

- [ ] **Step 6: 提交**

```bash
git add planner/rollout.py tests/test_rollout.py
git commit -m "refactor: rollout 迁移 SimSnapshot 快照序列——5 循环变量寄居快照+snapshots 派生字段; P1 前置过滤/截断原样; 逐点等价 dump 对照通过"
```

---

### Task 4: assemble_snapshot 抽取 + lethal_plan 薄化（live 钉子）

**Files:**
- Modify: `hsbot/analysis.py`（lethal_plan 261-336 行区重构）
- Test: `tests/test_lethal_plan.py`（追加 assemble 单测；既有断言零改动）

**Interfaces:**
- Produces: 模块级 `assemble_snapshot(st, knowledge, analyzer) -> tuple | None`——`(SimSnapshot, pieces)`；None=我方未解析。live 钉子 + Task 5 AuditExporter 依赖。exp_per_draw 仍在 lethal_plan 内计算（台账派生期望量，非状态事实，spec §3）。

- [ ] **Step 1: 写失败测试**

`tests/test_lethal_plan.py` 追加（复用 `_scene` 夹具；不走桩——assemble 是 hsbot 侧真代码）：

```python
# ---------------- assemble_snapshot 投影(spec §4) ----------------

def test_assemble_snapshot_projection(tmp_path):
    """store/knowledge → (SimSnapshot, pieces): 手牌费 COST 标签优先、
    known_draws 台账队列、敌血甲、场攻、回合数取 friendly_turn_number。"""
    from hsbot.analysis import assemble_snapshot
    st, db, a = _scene(tmp_path, mana=5, enemy=8)
    st.apply(mk_tag(10, GameTag.COST, 2))            # 火球 4→2
    snap, pieces = assemble_snapshot(st, None, a)
    assert snap.mana == 5 and snap.hand == (("TST_BOLT", 1), ("TST_FIRE", 2))
    assert snap.enemy_total == 8 and snap.board_atk == 0 and snap.face == 0
    assert snap.dealt_total == 0 and snap.turn == st.friendly_turn_number()
    assert set(pieces) == {("TST_BOLT", 1), ("TST_FIRE", 2)}


def test_assemble_snapshot_none_without_friendly(tmp_path):
    from hsbot.analysis import assemble_snapshot
    st, db, a = _scene(tmp_path)
    st.friendly_key = None
    assert assemble_snapshot(st, None, a) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_lethal_plan.py -q`
Expected: FAIL — `ImportError: cannot import name 'assemble_snapshot'`。

- [ ] **Step 3: 实现（analysis.py）**

lethal_plan 主体重构为两块（planner 延迟 import 移入 assemble）：

```python
def assemble_snapshot(st, knowledge, analyzer) -> tuple | None:
    """store/knowledge → (SimSnapshot, pieces) 投影(spec §4, glue 留 hsbot 侧,
    planner 仍不 import store)。None = 我方未解析。口径与原 lethal_plan 内联
    投影逐行同源(2026-09-15 抽取, live 钉子背书)。"""
    from planner.pieces import build_piece
    from planner.simstate import initial_state
    me = st.friendly_key
    if me is None:
        return None
    hand = []
    for e in st.hand(me):
        cid = getattr(e, "card_id", None)
        if not cid:
            continue
        cost = e.tags.get(GameTag.COST)        # 减费在身的实际费优先
        if cost is None:
            cost = analyzer.carddb.cost(cid)   # 卡表缺牌 → 0(诚实降级)
        hand.append((cid, cost if cost is not None else 0))
    sp = st.spellpower(me)
    known_draws: list[tuple[str, int]] = []
    if knowledge is not None:
        led = knowledge.ledger
        # 确定抽牌队列, 队头=最先抽到: 顶牌恒确定; 底牌仅在顶底之间无未知牌
        # (unknown_middle==0, 全库已知)时才是确定抽——否则该抽位实际抽到的
        # 是未知牌, 底牌降级走期望通道(宁漏勿错, 2026-09-14 审计修正)
        queue = [led.known_top]
        if led.unknown_middle == 0:
            # 底牌队列剔除顶牌(rebuild 的 bottom_map 含 pos=1 顶牌, 直接拼
            # 会双计), 且按牌位升序 = 抽牌序
            queue += [cid for cid, pos in sorted(
                ((c, p) for c, p in led.known_bottom if p > 1),
                key=lambda cp: cp[1])]
        for cid in queue:
            if cid:
                known_draws.append((cid, analyzer.carddb.cost(cid) or 0))
    # 场上引擎接线(审计 2026-09-14 高#1): 我方 cast_draw 随从数 = engines
    board_cids = [e.card_id for e in st.board(me) if e.card_id]
    engines = sum(board_cids.count(cid)
                  for cid in {c for c in board_cids
                              if build_piece(c, 0, analyzer).engine})
    pieces = {(cid, cost): build_piece(cid, cost, analyzer)
              for cid, cost in dict.fromkeys(hand + known_draws)}
    opp = st.opponent_key()
    snap = initial_state(
        st.mana_now(me), tuple(sorted(hand)), sp,
        known_draws=tuple(known_draws), engines=engines,
        turn=st.friendly_turn_number(),
        enemy_total=(st.hero_total_hp(opp) if opp is not None else None),
        board_atk=st.board_face_attack(me))
    return snap, pieces


def lethal_plan(st, knowledge, analyzer, *, enabled: bool = True) -> dict | None:
    """手牌出牌线搜索(T1 斩杀 DFS)的机读事实编排: 装配走 assemble_snapshot,
    搜索委托 planner 纯函数。返回 dict(actions/total/face_det/face_exp/
    lethal/enemy_total/board_atk/uncovered_n, 措辞零, 字段口径=接口契约 §2);
    None = 开关关 / 我方未解析 / 无手牌。"""
    if not enabled:
        return None
    asm = assemble_snapshot(st, knowledge, analyzer)
    if asm is None:
        return None
    snap, pieces = asm
    if not snap.hand:
        return None
    exp_per_draw = 0.0
    if knowledge is not None:
        remaining = knowledge.ledger.remaining
        deck_left = sum(remaining.values())
        # 期望注记(不进可斩判定): 当前实际法强的单卡伤害 × 台账剩余组成
        exp_per_draw = (sum(n * (analyzer.burst_damage(cid, snap.sp) or 0)
                            for cid, n in remaining.items()) / max(1, deck_left))
    from planner.dfs import best_line          # 延迟 import(测试可打桩)
    plan = best_line(snap, pieces, exp_per_draw=exp_per_draw)
    return {"actions": list(plan.actions), "total": plan.total,
            "face_det": plan.face_det, "face_exp": plan.face_exp,
            "lethal": plan.lethal, "enemy_total": plan.enemy_total,
            "board_atk": plan.board_atk,
            # 缺牌(卡表无条目)=效果未知: 与线内未覆盖张合并诚实计数
            "uncovered_n": plan.uncovered_n
            + sum(1 for cid, _c in snap.hand if analyzer.carddb.raw(cid) is None),
            # 每步打出后的剩余费(planner 事实原样透传; 悬浮窗数据行"剩费N"用)
            "mana_trace": plan.mana_trace}
```

（`hand` 变量在 uncovered_n 处改用 `snap.hand`；其余 lethal_plan 旧体删除。）

- [ ] **Step 4: 跑测试确认通过（live 钉子）**

Run: `python3 -m pytest tests/test_lethal_plan.py tests/test_lethal_render.py tests/test_output_hub.py -q`
Expected: 全 PASS——既有断言零改动 = live 斩杀线输出零变化。

- [ ] **Step 5: 提交**

```bash
git add hsbot/analysis.py tests/test_lethal_plan.py
git commit -m "refactor: assemble_snapshot 抽取——lethal_plan 70 行内联投影成模块级装配(glue 留 hsbot), live 钉子背书"
```

---

### Task 5: recon 对账纯函数 + watcher AuditExporter + JSONL 落盘

**Files:**
- Create: `hsbot/recon.py`
- Modify: `hsbot/watcher.py`（新增 AuditExporter 类 + __init__ 接线 + `_process_tree` 基态捕获 + `_route` on_play + `_new_game` reset；顶部 `from hearthstone.enums import BlockType`）
- Test: `tests/test_recon.py`（创建）

**Interfaces:**
- Consumes: Task 4 `assemble_snapshot`、Task 1 `play`、store play 事件字段（`card_id/cost_tag/eid/is_power`，`_emit_play` 已带）。
- Produces:
  - `reconcile(base, pieces, evt, after, carddb) -> dict | None`（分歧记录；一致 None）
  - `ir_source_hash(carddb, cid) -> str`
  - Task 6 依赖二者与 JSONL 行格式 `{ts, game_id, card_id, piece, ir_source_hash, predicted, actual, diffs}`。
  - `watcher.AuditExporter(cfg)`：`capture_base(store, knowledge, analyzer, eid)` / `on_play(evt, store, knowledge, analyzer)` / `reset()`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recon.py`：

```python
"""预测-实测对账(spec §5): reconcile 纯函数 + AuditExporter 落盘。
fixture 三类分歧(伤害/回费/抽牌)+一致零输出(spec §8)。"""
import json

import pytest
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


def _run_play(st, *, used=4, enemy_dmg=6):
    b = mk_block(BlockType.PLAY, 10)
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
    aud.capture_base(st, None, a, 10)
    _run_play(st, used=4, enemy_dmg=4)                # 实测只打 4(卡牌改版? )
    evt = log.by_kind("play")[0]
    evt["game_id"] = "deadbeef"
    aud.on_play(evt, st, None, a)
    f = tmp_path / "logs" / "sim_divergence.jsonl"
    rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    assert rec["game_id"] == "deadbeef" and rec["card_id"] == "TST_FIRE"
    assert {x["field"] for x in rec["diffs"]} == {"face"}
    assert rec["piece"]["segments"] == (6,) and rec["piece"]["cost"] == 4


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_recon.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hsbot.recon'`。

- [ ] **Step 3: 实现 hsbot/recon.py**

```python
"""预测-实测对账(spec §5): play() 预测转移 vs store 实测转移的纯函数 diff。

零 IO 纪律: 落盘在 watcher.AuditExporter, 本模块只算不写(summarize 同为
纯函数; main 仅作 CLI 入口读文件)。transition 级对账(非整线终态对比):
每条分歧的定位面 = Piece 字段面 —— mana 组↔cost/减费/回费, face↔segments×sp,
hand_n/hand_cards↔engine/draw_n, engines↔engine, sp↔spellpower_gain。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections import Counter

from planner.pieces import Piece
from planner.simstate import play


def _hand_multiset(hand) -> Counter:
    return Counter(cid for cid, _cost in hand)


def ir_source_hash(carddb, cid: str) -> str:
    """卡表原始记录的短哈希: 分歧直接定位到编译输入(发现→改规则→
    COMPILER_VERSION+1→重编译→分歧消失 的收敛闭环锚点)。"""
    raw = carddb.raw(cid)
    if raw is None:
        return "missing"
    return hashlib.sha1(json.dumps(raw, sort_keys=True,
                                   ensure_ascii=False).encode("utf-8")
                        ).hexdigest()[:12]


def reconcile(base, pieces: dict, evt: dict, after, carddb) -> dict | None:
    """(PLAY 块开始基态, play 事件, 块后实测态) → 分歧记录; 一致 → None。

    evt 用 play 事件的 card_id/cost_tag(块开始锁定的真实手牌价); 预测 =
    play(base, 手牌中的该牌); 实测 = after 快照同口径投影。基态里没有该牌
    (装配竞态/衍生牌) → None 不对账。
    """
    cid = evt.get("card_id")
    cost = evt.get("cost_tag")
    idx = None
    for i, key in enumerate(base.hand):
        if key[0] == cid and (cost is None or key[1] == cost):
            idx = i
            break
    if idx is None:
        return None
    key = base.hand[idx]
    p = pieces.get(key)
    if p is None:
        p = Piece(card_id=key[0], cost=key[1])        # inert: 与 play 同降级
    predicted_face = 0
    try:
        pred = play(base, idx, pieces)
    except ValueError:                               # 预测不可支付但实际打出
        pred = base
        diffs = [{"field": "playable", "predicted": False, "actual": True}]
    else:
        predicted_face = pred.face - base.face
        diffs = []
        if pred.mana != after.mana:
            diffs.append({"field": "mana", "predicted": pred.mana,
                          "actual": after.mana})
        if len(pred.hand) != len(after.hand):
            diffs.append({"field": "hand_n", "predicted": len(pred.hand),
                          "actual": len(after.hand)})
        pm, am = _hand_multiset(pred.hand), _hand_multiset(after.hand)
        if pm != am:
            diffs.append({"field": "hand_cards", "predicted": sorted(pm - am),
                          "actual": sorted(am - pm)})
        if pred.engines != after.engines:
            diffs.append({"field": "engines", "predicted": pred.engines,
                          "actual": after.engines})
        if pred.sp != after.sp:
            diffs.append({"field": "sp", "predicted": pred.sp, "actual": after.sp})
    actual_face = (base.enemy_total - after.enemy_total
                   if base.enemy_total is not None
                   and after.enemy_total is not None else None)
    if actual_face is not None and predicted_face != actual_face:
        diffs.append({"field": "face", "predicted": predicted_face,
                      "actual": actual_face})
    if not diffs:
        return None
    piece_d = dataclasses.asdict(p)
    piece_d["card_cats"] = sorted(p.card_cats)       # frozenset 不可 json 化
    return {"ts": round(time.time(), 3), "game_id": evt.get("game_id"),
            "card_id": cid, "piece": piece_d,
            "ir_source_hash": ir_source_hash(carddb, cid),
            "predicted": {"mana": pred.mana, "hand_n": len(pred.hand),
                          "engines": pred.engines, "sp": pred.sp,
                          "face": predicted_face},
            "actual": {"mana": after.mana, "hand_n": len(after.hand),
                       "engines": after.engines, "sp": after.sp,
                       "face": actual_face},
            "diffs": diffs}
```

- [ ] **Step 4: 实现 watcher 接线**

4a. `hsbot/watcher.py` 顶部 import 区加 `from hearthstone.enums import BlockType`；TrainingExporter 类后追加：

```python
class AuditExporter:
    """预测-实测对账落盘(spec §5): PLAY 块开始时捕获基态快照(装配自 store,
    此时块内包尚未应用 = 预测基态), play 事件落地时 play() 重放该牌 →
    recon.reconcile diff → data/logs/sim_divergence.jsonl 追加。
    零干扰: 任何异常只 WARNING, 绝不阻断 live 主链; 一致时零输出。"""

    def __init__(self, cfg) -> None:
        self.path = Path(cfg.data_dir) / "logs" / "sim_divergence.jsonl"
        self._bases: dict = {}

    def reset(self) -> None:
        """新局: 未配对的基态整体作废(与 GameScope 重置语义同步)。"""
        self._bases.clear()

    def capture_base(self, store, knowledge, analyzer, eid) -> None:
        try:
            asm = assemble_snapshot(store, knowledge, analyzer)
            if asm is not None:
                self._bases[eid] = asm
        except Exception:  # noqa: BLE001
            log.warning("对账基态捕获失败(eid=%s)", eid, exc_info=True)

    def on_play(self, evt: dict, store, knowledge, analyzer) -> None:
        base = self._bases.pop(evt.get("eid"), None)
        if base is None or evt.get("is_power"):
            return                          # 无基态/英雄技能: 不对账
        try:
            after = assemble_snapshot(store, knowledge, analyzer)
            if after is None:
                return
            from .recon import reconcile
            rec = reconcile(base[0], base[1], evt, after[0],
                            analyzer.carddb)
            if rec is not None:
                import json as _json
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fp:
                    fp.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            log.warning("对账落盘失败(card=%s)", evt.get("card_id"),
                        exc_info=True)
```

（`assemble_snapshot` 加入 watcher 头部 `from .analysis import ...` 现有 import 列表。）

4b. `Watcher.__init__` 的 `self.exporter = TrainingExporter(cfg, carddb)` 行后加：

```python
        self.audit = AuditExporter(cfg)               # 预测-实测对账(spec §5)
```

4c. `_new_game` 的 `self.match = GameScope(no=self.game_no)` 行后加：

```python
        self.audit.reset()
```

4d. `_process_tree` 包循环（`while i < limit:` 体内 `self.match.gs.apply(pkt, depth)` 之前）插基态捕获：

```python
        while i < limit:
            pkt, depth = flat[i]
            if (type(pkt).__name__ == "Block"
                    and getattr(pkt, "type", None) == BlockType.PLAY
                    and isinstance(pkt.entity, int)):
                # 对账基态: PLAY 块开始 = store 尚未应用块内包(预测基态)
                self.audit.capture_base(self.match.gs, self.match.knowledge,
                                        self.analyzer, pkt.entity)
            try:
                self.match.gs.apply(pkt, depth)
```

4e. `_route` 的 `is_my_play` 判定块后（`evt = self.analyzer.enrich(...)` 之前）插：

```python
        if kind == "play":
            self.audit.on_play(evt, self.match.gs, self.match.knowledge,
                               self.analyzer)
```

- [ ] **Step 5: 跑测试确认通过（含全套 + 回放不扰）**

Run: `python3 -m pytest tests/test_recon.py tests/test_watcher_integration.py tests/ -q`
Expected: 全 PASS——mini_game.log 回放友方未解析 → assemble 返回 None → 零 JSONL、零 UI 扰动；`data/logs/sim_divergence.jsonl` 只在真分歧时增长。

- [ ] **Step 6: 提交**

```bash
git add hsbot/recon.py hsbot/watcher.py tests/test_recon.py
git commit -m "feat: 预测-实测对账通道——recon 纯函数 diff + watcher AuditExporter(PLAY 块基态捕获/play 事件重放) + sim_divergence.jsonl; 零干扰"
```

---

### Task 6: recon CLI 汇总 + ARCHITECTURE.md 两通道总纲 + 收尾

**Files:**
- Modify: `hsbot/recon.py`（追加 summarize/main）
- Modify: `docs/ARCHITECTURE.md`（增补总纲节）
- Test: `tests/test_recon.py`（追加）

**Interfaces:**
- Produces: `summarize(records) -> dict`（`{"n", "by_card": [(cid, n)...], "by_field": {field: n}}`）；`main(argv=None) -> int`（`python -m hsbot.recon <jsonl>`）。

- [ ] **Step 1: 写失败测试**

`tests/test_recon.py` 追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_recon.py -v`
Expected: FAIL — `ImportError: cannot import name 'summarize'`。

- [ ] **Step 3: 实现（recon.py 追加）**

```python
def summarize(records) -> dict:
    """对账记录 → 汇总(top 分歧卡 / 分歧字段计数)——消费入口 v1, 不做自动
    改规则(spec §5)。"""
    by_card = Counter(r["card_id"] for r in records)
    by_field = Counter(d["field"] for r in records for d in r["diffs"])
    return {"n": len(records), "by_card": by_card.most_common(),
            "by_field": dict(by_field)}


def main(argv=None) -> int:
    import sys
    from pathlib import Path
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if len(argv) != 1:
        print("用法: python -m hsbot.recon <sim_divergence.jsonl>")
        return 2
    path = Path(argv[0])
    records = [json.loads(ln) for ln in
               path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    s = summarize(records)
    print(f"对账分歧 {s['n']} 条 ({path.name})")
    print("字段分布: " + (", ".join(f"{k}×{v}" for k, v in s["by_field"].items())
                        or "无"))
    for cid, n in s["by_card"][:10]:
        print(f"  {cid} ×{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_recon.py -q && python3 -m hsbot.recon`
Expected: 测试全 PASS；无参 CLI 打印用法、退出码 2。

- [ ] **Step 5: ARCHITECTURE.md 增补两通道总纲**

在"## 1. 模块与类图"节之前插入：

```markdown
## 0.5 两通道架构总纲(2026-09-15 SimSnapshot 统一定稿)

```
通道 1(真实)  Power.log → pipeline/watcher → store(唯一状态权威)
                ├→ 快照持久化 / 训练语料 / 真实结果
                └→ 角色: 通道 2 的初始条件供给 + 对账靶
通道 2(模拟)  效果 IR(effects 编译→Piece) + SimSnapshot(游戏快照)
                → play / advance_turn 推演
                → 一切下游模拟与建议: live 斩杀线 / 留牌模拟器 / 未来通用预测器
```

后续一切模拟、建议类功能只长在通道 2 上; 通道 1 的角色 = 初始条件供给 +
对账靶(纯展示类输出直读 store, 不属模拟/建议, 不受本原则约束)。装配分置:
live = `analysis.assemble_snapshot`(store 投影, glue 留 hsbot 侧, planner
仍不 import store); sim = `rollout` 抽样构造 `SimSnapshot`。

预测-实测对账(SimSnapshot spec §5): 我方每张牌结算后 `play()` 重放预测
转移 vs store 实测转移, diff 纯函数在 `hsbot/recon.py`(零 IO), 落盘归
`watcher.AuditExporter`(`data/logs/sim_divergence.jsonl`, 零干扰), 汇总
`python -m hsbot.recon <jsonl>` —— 分歧可直接定位到 effects 语法表 /
Piece 语义 / play 转移, 形成收敛闭环(不自动改规则)。
```

类与职责清单表同步补两行：

```markdown
| `analysis.assemble_snapshot` | store/knowledge → (SimSnapshot, pieces) 装配投影 | 不改状态; planner 依赖铁律不破 |
| `watcher.AuditExporter` | 预测-实测对账 JSONL 落盘 | 不做游戏决策; 零干扰(失败仅 WARNING) |
```

- [ ] **Step 6: 全套回归 + 提交**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿（基线 238+2 起步，加 P1 波与本计划新增用例后的总数以开工实测为准）。

```bash
git add hsbot/recon.py docs/ARCHITECTURE.md tests/test_recon.py
git commit -m "feat: recon CLI 汇总 + ARCHITECTURE 两通道总纲增补——SimSnapshot 统一层收尾"
```

---

## Self-Review 记录

- **Spec 覆盖**：§2 数据模型→Task 1；§3 转移函数/统一算式→Task 1+2（exp_per_draw 保留 kwarg✓）；§4 装配迁移→Task 2（调用方）+Task 3（rollout）+Task 4（assemble/薄化）；§5 对账→Task 5+6；§6 铁律→Global Constraints；§7 成功标准→Task 3 Step 1/5（逐点 dump）、live 钉子（Task 4 断言零改动）、memo 坍缩（memo_key 与旧 `_key` 逐字段同构=坍缩键不变，由 test_memo_key 钉死）；§8 测试矩阵逐格→Task 1（advance_turn 四格）/Task 1（memo_key）/Task 3（RolloutResult 契约回归）/Task 4（assemble 单测+live 钉子）/Task 5（recon 三类分歧+一致零输出）；§9 不做→Global Constraints；§10 切片 1-6 与 Task 1-6 一一对应。
- **已知张力（spec 内部，已按 §7 裁决）**：spec §3 的 advance_turn 把 face 累入 dealt_total，但 §7 要求 simcal 逐点一致 ⇒ rollout 在 advance 后 `replace(dealt_total=0)`（v1 血甲曲线=逐回合经验值，跨回扣减会双计）——Task 3 代码注释与本记录双备案。
- **占位符扫描**：无 TBD/TODO；Task 1 Step 1 的 for 循环恒真式已在正文内给出替换版本；Task 1 Step 3a 的海象笔误已内联标注纠正——执行时按注写直白代码。
- **类型一致性**：`initial_state` 新 4 参 keyword-only 在 Task 1 定义、Task 2（test_planner/simcal/analysis）/Task 3（rollout）/Task 4（assemble）消费一致；`memo_key`/`advance_turn`/`assemble_snapshot`/`reconcile`/`summarize`/`AuditExporter.{capture_base,on_play,reset}` 各任务间签名逐一核对；JSONL 行字段与 spec §5 条目逐键对齐（ts/game_id/card_id/piece/ir_source_hash/predicted/actual/diffs）。
- **并发风险**：Task 3 基线=含 P1 未提交改动的 163 行 rollout（开工前置条件已列）；若 P1 流继续演进，重放本计划 Task 3 前需重新对齐 `_face_ceiling`/`_drawable`/cap 的现状。
