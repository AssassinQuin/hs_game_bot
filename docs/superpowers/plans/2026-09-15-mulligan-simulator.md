# 留牌模拟器(v3.1)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用蒙特卡洛 rollout 直接计算 P(启动≤K│keep集),为 0 变化自闭卡组(奇迹德)提供留牌评分与组合维度输出,并以三层一致性校准(轨迹/启动/结果)对真实语料自验证。

**Architecture:** planner 侧新增纯函数 rollout(复用 SimState/play/best_line,抽样=known_draws 队列);trainer 侧新增 `sim`(CRN 2ⁿ 枚举+组合输出+CLI)与 `simcal`(三层校准)。live(hsbot/)零触碰——零变化保证是结构性的。

**Tech Stack:** 纯 Python 标准库(random/itertools/dataclasses),无新增依赖。

**上游设计:** [v3.1 spec](../specs/2026-09-15-mulligan-simulator-design.md)(§3 rollout / §4 谓词 / §5 CRN / §6 三层校准 / §7 pieces 硬门)。

## Global Constraints

- **live 零触碰**:不修改 `hsbot/` 下任何文件;`planner/simstate.py`、`planner/pieces.py` 的改动必须满足"build_piece 产出的 Piece.draw_n 恒 0 → play() 行为与旧版逐字段一致"(钉子测试钉死)。
- **抽样取代折算**(spec §3):rollout 路径中未知抽牌以 known_draws 队列喂给 play,`exp_per_draw`/期望折算在 rollout 及其调用方永不出现(钉子测试:源码级禁用断言)。
- 纯函数纪律:planner 新代码无 IO、无全局态;随机性只经 `random.Random(seed)` 实例。
- 每个任务结束全测试套绿(`python3 -m pytest tests/ -q`),提交信息沿用仓库中文风格(`feat:`/`test:`/`fix:` 前缀)。
- 性能预算:spec §5 允许"分钟级";本计划默认 `orders=200`(对 spec"1k 起步"的**偏离,已备案**:DFS 级启动判定约 20ms/次,200×32×k_max=8 分钟级内,`--orders` 可调到 1k)。
- **范围裁定**:spec §10 切片 6(蒸馏系数+live 接线)**推迟**至 v3 §6 基础设施落地后另立计划;spec §8 的 config.yaml 键随切片 6 一并落地(本期开关=CLI 子命令存在性)。最小可信验证用真实语料(83 局奇迹德)而非 two_games 夹具——强于 spec §9 原措辞,意图一致。

## File Structure

| 文件 | 职责 |
|---|---|
| Create `planner/rollout.py` | 多回合 rollout 纯函数(策略+回合推进+启动判定) |
| Create `trainer/sim.py` | sim pieces 构建+完备性硬门+CRN 2ⁿ 枚举+组合输出+曲线提取+CLI |
| Create `trainer/simcal.py` | 三层一致性校准(轨迹/启动/结果) |
| Create `tests/test_rollout.py` | rollout/draw 通道/pieces 构建/枚举/输出 全部新测试 |
| Modify `planner/pieces.py` | Piece 增 `draw_n` 字段(默认 0,build_piece 永不填) |
| Modify `planner/simstate.py` | play() 增独立抽牌通道(重构共享 _draw_one) |
| Modify `trainer/__main__.py` | `sim` 子命令分发(仿 mulligan 三行式) |
| Modify `docs/MULLIGAN_AI.md` | v3.1 节回填(校准实跑结果后) |

---

### Task 1: Piece.draw_n 通道 + play() 独立抽牌

**Files:**
- Modify: `planner/pieces.py:19-33`(Piece 字段)
- Modify: `planner/simstate.py:70-84`(play 抽牌段重构)
- Test: `tests/test_rollout.py`(创建)

**Interfaces:**
- Produces: `Piece.draw_n: int = 0`(build_piece 永不填充);`play()` 在引擎触发之外按 `p.draw_n` 独立抽牌(同样走 known_draws 队头/drawn 计数)。后续任务的 rollout 依赖此通道。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_rollout.py`:

```python
"""rollout 多回合推演 + 独立抽牌通道(v3.1 设计 §3)。

假卡表沿用 tests/test_planner.py 的 _db 模式(无网络)。
"""
import json

import pytest

from hsbot.analysis import EffectAnalyzer
from hsbot.carddb import CardDB

from planner.pieces import Piece, build_piece
from planner.simstate import initial_state, play

CARDS = [
    {"id": "MOON", "name": "月火术", "type": "SPELL", "cost": 1,
     "text": "造成$1点伤害。"},
    {"id": "AUCTION", "name": "黑市拍卖师", "type": "MINION", "cost": 5,
     "text": "每当你施放一个法术后，抽一张牌。"},
    {"id": "DRAW2", "name": "奥术洞察", "type": "SPELL", "cost": 3,
     "text": "抽2张牌。"},
    {"id": "INNERVATE", "name": "激活", "type": "SPELL", "cost": 0,
     "text": "在本回合中，获得一个 法力水晶。"},
    {"id": "BIG", "name": "星火术", "type": "SPELL", "cost": 4,
     "text": "造成$6点伤害。"},
]


def _db(tmp_path, cards):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    return CardDB(p)


@pytest.fixture
def analyzer(tmp_path):
    return EffectAnalyzer(_db(tmp_path, CARDS))


# ---------------- Task1: 独立抽牌通道 ----------------

def test_play_standalone_draw_pops_known_queue():
    """draw_n=2 的法术: 两张已知牌队头入手, drawn 不增。"""
    pieces = {("DRAW2", 3): Piece(card_id="DRAW2", cost=3, is_spell=True,
                                  draw_n=2)}
    st = initial_state(5, (("DRAW2", 3),),
                       known_draws=(("MOON", 1), ("MOON", 1)))
    nxt = play(st, 0, pieces)
    assert sorted(nxt.hand) == [("MOON", 1), ("MOON", 1)]
    assert nxt.drawn == 0


def test_play_standalone_draw_unknown_counts_drawn():
    pieces = {("DRAW2", 3): Piece(card_id="DRAW2", cost=3, is_spell=True,
                                  draw_n=2)}
    st = initial_state(5, (("DRAW2", 3),))
    nxt = play(st, 0, pieces)
    assert nxt.hand == ()
    assert nxt.drawn == 2


def test_build_piece_never_sets_draw_n(analyzer):
    """live 零变化铁律: build_piece 产出的 draw_n 恒 0(draw_n 只由
    trainer.sim 后填)。触发句(每当)与非触发抽牌都不得进 build_piece。"""
    for cid in ("MOON", "AUCTION", "DRAW2"):
        assert build_piece(cid, 1, analyzer).draw_n == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `Piece.__init__` 收到未知关键字 `draw_n`(TypeError)。

- [ ] **Step 3: 最小实现**

`planner/pieces.py` 的 Piece 增字段(排在 `is_spell` 之后):

```python
    is_spell: bool = False       # cardtype == "SPELL"
    draw_n: int = 0              # 独立抽牌数(rollout 专用; build_piece 永不填,
                                 # live 斩杀线路径恒 0 —— 零变化由钉子测试背书)
```

`planner/simstate.py` 的 play() 抽牌段重构(替换第 70–84 行的引擎触发块):

```python
    known = state.known_draws
    drawn = state.drawn
    hand_list = None                      # 惰性 list 化: 只有入手牌才重排

    def _draw_one() -> None:              # 已知牌队头入手, 未知只计数
        nonlocal known, drawn, hand_list   # (rollout 的抽样 = known 队列喂入)
        if known:
            if hand_list is None:
                hand_list = list(rest)
            insort(hand_list, known[0])   # 队头入手, 二分插入保持升序
            known = known[1:]             # 弹出
        else:
            drawn += 1

    if p.is_spell and state.engines > 0:  # 引擎触发只看打牌前的 engines
        for _ in range(state.engines):    # 每个在场引擎各自触发一次
            _draw_one()                   # (双拍卖师施一法术抽两张)
    for _ in range(p.draw_n):             # 独立抽牌(rollout 专用; 恒 0 即无操作)
        _draw_one()
    hand = tuple(hand_list) if hand_list is not None else rest
```

同步更新 play() docstring 的抽牌条目,追加一行:
`- 独立抽牌: p.draw_n>0 时逐张抽(同样 known 队头优先); build_piece 恒不填, live 路径零影响。`

- [ ] **Step 4: 跑测试确认通过(含回归)**

Run: `python3 -m pytest tests/test_rollout.py tests/test_planner.py -q`
Expected: PASS(旧 planner 测试全绿=重构等价背书)。

- [ ] **Step 5: 提交**

```bash
git add planner/pieces.py planner/simstate.py tests/test_rollout.py
git commit -m "feat: play() 独立抽牌通道(draw_n)——rollout 前置; build_piece 恒不填, live 零变化钉子"
```

---

### Task 2: planner/rollout.py 多回合推演核心

**Files:**
- Create: `planner/rollout.py`
- Test: `tests/test_rollout.py`(追加)

**Interfaces:**
- Consumes: `initial_state/play` (Task 1 后的语义), `best_line(state, pieces, *, enemy_total)`。
- Produces:
  - `RolloutResult(launch_turn: int | None, turns: int, hand_sizes: tuple[int, ...] = (), engines: int = 0)`
  - `rollout(full_order: tuple[str, ...], *, offered: tuple[str, ...], keep: tuple[str, ...], coin: bool, pieces: dict, cost_of: dict[str, int], k_max: int, enemy_totals: Sequence[int | None]) -> RolloutResult`
  - Task 3/6 依赖此签名;`cost_of` 必须含 "COIN"(coin=True 时)。

- [ ] **Step 1: 写失败测试**

`tests/test_rollout.py` 追加:

```python
# ---------------- Task2: rollout 核心 ----------------

from planner.rollout import RolloutResult, rollout


def _sim_pieces():
    """手搓 pieces(不经编译器, 测试确定性): 月火1费1伤/星火4费6伤/
    拍卖师5费引擎/激活0费回费。"""
    return {
        ("MOON", 1): Piece("MOON", 1, segments=(1,), spell_scaled=True,
                           is_spell=True),
        ("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                          is_spell=True),
        ("AUCTION", 5): Piece("AUCTION", 5, engine=True),
        ("INNERVATE", 0): Piece("INNERVATE", 0, mana_gain=1, is_spell=True),
        ("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True),
    }


_COST = {"MOON": 1, "BIG": 4, "AUCTION": 5, "INNERVATE": 0, "COIN": 0}


def test_rollout_holds_damage_until_affordable_then_launches():
    """留星火: 策略不打伤害牌(无引擎), T4 费够 → best_line 启动。"""
    r = rollout(("MOON", "MOON", "BIG"), offered=("MOON", "BIG"),
                keep=("BIG",), coin=False, pieces=_sim_pieces(),
                cost_of=_COST, k_max=6,
                enemy_totals=(None, 6, 6, 6, 6, 6))
    assert r.launch_turn == 4


def test_rollout_no_launch_returns_none():
    r = rollout(("MOON",), offered=("MOON",), keep=("MOON",), coin=False,
                pieces=_sim_pieces(), cost_of=_COST, k_max=3,
                enemy_totals=(30, 30, 30))
    assert r.launch_turn is None
    assert r.turns == 3
    assert len(r.hand_sizes) == 3


def test_rollout_policy_deploys_engine_and_damage_becomes_fuel():
    """T5 上拍卖师; T6 月火作为燃料被打出并触发抽牌循环 → 启动。"""
    # 牌序须含 keep 卡: 拍卖师按首次出现移除后, 抽牌流 = 月火×7
    order = ("AUCTION",) + ("MOON",) * 7
    r = rollout(order, offered=("AUCTION", "MOON"), keep=("AUCTION",),
                coin=False, pieces=_sim_pieces(), cost_of=_COST, k_max=6,
                enemy_totals=(None, None, None, None, None, 6))
    assert r.launch_turn == 6
    assert r.engines == 1


def test_rollout_pure_function_same_input_same_output():
    kw = dict(offered=("MOON", "BIG"), keep=("BIG",), coin=False,
              pieces=_sim_pieces(), cost_of=_COST, k_max=5,
              enemy_totals=(None, 6, 6, 6, 6))
    assert rollout(("MOON", "MOON", "BIG"), **kw) == \
        rollout(("MOON", "MOON", "BIG"), **kw)


def test_rollout_keep_card_not_in_order_raises():
    with pytest.raises(ValueError):
        rollout(("MOON",), offered=("MOON", "BIG"), keep=("BIG",),
                coin=False, pieces=_sim_pieces(), cost_of=_COST, k_max=2,
                enemy_totals=(6, 6))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'planner.rollout'`。

- [ ] **Step 3: 实现 planner/rollout.py**

```python
"""多回合 rollout —— 自闭卡组留牌模拟(v3.1 设计 §3/§4)。

与 dfs 的语义分歧(设计 §3 单列条款): dfs 中未知抽牌只计数, 经 exp_per_draw
进期望注记; 本模块把抽样牌序作为 known_draws 队列喂给 play —— 抽到的牌
真实入手, 期望折算通道在本模块永不出现。

脚本策略(设计 §3): 引擎/回费/无伤害段牌尽快打; 伤害段牌仅引擎在场时打
(转抽牌循环燃料), 否则留手作启动组件 —— "绝不打出启动组件"的落地。
启动判定复用 best_line(board_atk=0 保守口径, 宁漏勿错)。

简化口径(v1, 轨迹层校准兜底): 被换牌视为洗回统一抽牌流; 忽略手牌上限、
疲劳与费用锁定; T1 不自然抽、T≥2 每回合抽 1; coin 注入为 0 费回费法术。
纯函数: 无 IO/全局态; 随机性全在调用方(trainer.sim 的 CRN 层)。
"""
from __future__ import annotations

import dataclasses

from .dfs import best_line
from .simstate import initial_state, play


@dataclasses.dataclass(frozen=True)
class RolloutResult:
    launch_turn: int | None            # 首个启动回合; 未启动 None
    turns: int                         # 推演到的回合数
    hand_sizes: tuple[int, ...] = ()   # 各回合策略收尾后手牌数(轨迹层校准用)
    engines: int = 0                   # 终态在场引擎数


def _policy_playable(key, pieces: dict, engines: int) -> bool:
    """脚本策略单卡裁定: 引擎/回费/无伤害段 → 打; 伤害段 → 仅引擎在场打。"""
    p = pieces.get(key)
    if p is None or p.engine or p.mana_gain or not p.segments:
        return True
    return engines > 0


def rollout(full_order, *, offered, keep, coin, pieces, cost_of, k_max,
            enemy_totals) -> RolloutResult:
    """一副完整牌序 + 留牌决策 → 启动回合。

    full_order: 整副牌(含重复)的一个随机序 —— CRN 层对全部 keep 集共用;
    keep 中的卡按首次出现从 full_order 移除, 剩余序列即抽牌流。
    enemy_totals: 按回合(1-based)的敌方有效血甲; None 回合跳过启动判定。
    """
    stream = list(full_order)
    for cid in keep:
        if cid not in stream:
            raise ValueError(f"keep 卡 {cid} 不在牌序中(调用方数据错)")
        stream.remove(cid)
    hand = list(keep)
    if coin:
        hand.append("COIN")
    fill = len(offered) - len(keep)          # 换牌补抽(v1 简化: 同一抽牌流)
    hand += stream[:fill]
    del stream[:fill]

    engines = sp = disc_hand = 0
    hand_sizes: list[int] = []
    for t in range(1, k_max + 1):
        if t >= 2 and stream:
            hand.append(stream.pop(0))       # 回合开始自然抽
        st = initial_state(
            min(10, t), tuple((c, cost_of[c]) for c in hand),
            sp=sp, disc_hand=disc_hand, engines=engines,
            known_draws=tuple((c, cost_of[c]) for c in stream))
        while True:                          # 策略出牌: 打到打不动为止
            played = False
            for i, key in enumerate(st.hand):
                if _policy_playable(key, pieces, st.engines):
                    try:
                        st = play(st, i, pieces)
                        played = True
                        break
                    except ValueError:       # 不可支付: 试下一张
                        continue
            if not played:
                break
        hand_sizes.append(len(st.hand))
        total = enemy_totals[t - 1]
        if total is not None:
            # 已打出的策略伤害先扣血, best_line 只算手牌剩余爆发(face 清零)
            plan = best_line(dataclasses.replace(st, face=0), pieces,
                             enemy_total=total - st.face)
            if plan.lethal:
                return RolloutResult(t, t, tuple(hand_sizes), st.engines)
        engines, sp, disc_hand = st.engines, st.sp, st.disc_hand
        hand = [k[0] for k in st.hand]
        stream = [k[0] for k in st.known_draws]   # 引擎循环抽走的已入手
    return RolloutResult(None, k_max, tuple(hand_sizes), engines)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: PASS ×8。

- [ ] **Step 5: 提交**

```bash
git add planner/rollout.py tests/test_rollout.py
git commit -m "feat: planner/rollout 多回合推演——抽样牌序喂 known_draws, 策略'引擎优先/伤害留手', 启动判定复用 best_line"
```

---

### Task 3: trainer/sim.py —— pieces 构建 + 完备性硬门

**Files:**
- Create: `trainer/sim.py`
- Test: `tests/test_rollout.py`(追加)

**Interfaces:**
- Consumes: `build_piece(cid, cost, analyzer)`、`EffectAnalyzer(carddb)`、`carddb.{cost,raw,text}`、IR `Draw(amount, scope)`。
- Produces:
  - `COIN_CID = "COIN"`
  - `build_sim_pieces(decklist: dict, carddb, analyzer) -> tuple[dict, dict]` → `(pieces, cost_of)`,含 COIN 注入与 draw_n 后填
  - `pieces_completeness(decklist, carddb, pieces, cost_of) -> list[str]`(空=通过)
  - Task 4/6 依赖。

- [ ] **Step 1: 写失败测试**

`tests/test_rollout.py` 追加:

```python
# ---------------- Task3: sim pieces 构建 + 完备性硬门 ----------------

from trainer.sim import COIN_CID, build_sim_pieces, pieces_completeness


def test_build_sim_pieces_injects_coin_and_draws(analyzer, tmp_path):
    db = _db(tmp_path, CARDS)
    decklist = {"MOON": 2, "AUCTION": 1, "DRAW2": 2}
    pieces, cost_of = build_sim_pieces(decklist, db, analyzer)
    assert cost_of["MOON"] == 1 and cost_of["AUCTION"] == 5
    coin = pieces[(COIN_CID, 0)]
    assert coin.mana_gain == 1 and coin.is_spell and cost_of[COIN_CID] == 0
    # 独立抽牌后填: 奥术洞察 Draw(2) → draw_n=2; 拍卖师(每当)守卫 → 0
    assert pieces[("DRAW2", 3)].draw_n == 2
    assert pieces[("AUCTION", 5)].draw_n == 0


def test_pieces_completeness_gates_missing_card_and_key(analyzer, tmp_path):
    db = _db(tmp_path, CARDS)
    decklist = {"MOON": 2, "GHOST": 1}        # GHOST 不在卡表
    pieces, cost_of = build_sim_pieces(decklist, db, analyzer)
    assert any("GHOST" in s for s in
               pieces_completeness(decklist, db, pieces, cost_of))
    del pieces[("MOON", 1)]                   # 人为抽走键
    assert any("MOON" in s for s in
               pieces_completeness(decklist, db, pieces, cost_of))
    ok_deck = {"MOON": 2}
    p2, c2 = build_sim_pieces(ok_deck, db, analyzer)
    assert pieces_completeness(ok_deck, db, p2, c2) == []
```

注意:若 `Draw(2)` 后填断言失败且原因是卡表文本 `"抽2张牌。"` 未被 IR 编译成 `Draw(amount=2)`,调整 CARDS 中该卡 text 措辞(如 `"抽两张牌。"`)直到编译出 Draw——本测试钉的是后填管线,不是编译器;在测试注释里保留此说明。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trainer.sim'`。

- [ ] **Step 3: 实现 trainer/sim.py(本任务只写 pieces 部分)**

```python
"""留牌模拟器(v3.1)—— 训练/CLI 侧: pieces 构建、CRN 2ⁿ 枚举、组合维度
输出、语料曲线提取与 CLI。live 进程零依赖本模块。

铁律(spec §7): pieces 不完备 → 硬失败清单, 该卡组模拟器禁用, 级联降级
(TabPFN/LR/统计表)。与 live 斩杀线的 inert 诚实降级是显式分歧。
"""
from __future__ import annotations

import dataclasses

from planner.pieces import Piece, build_piece

COIN_CID = "COIN"


def _draw_n_from_ir(carddb, analyzer, cid: str) -> int:
    """非触发的独立抽牌数; 文本含触发/条件标记一律 0(宁漏勿错)。"""
    text = carddb.text(cid) or ""
    if "每当" in text or "如果" in text:
        return 0
    ir = analyzer.cache.get_or_compile(carddb.raw(cid))
    if ir is None:
        return 0
    n = 0
    for e in ir.effects:
        if type(e).__name__ == "Draw" and getattr(e, "scope", "") != "opponent":
            n += getattr(e, "amount", 1)   # 对手抽牌(自然平衡族)不计我方
    return n


def build_sim_pieces(decklist: dict, carddb, analyzer) -> tuple[dict, dict]:
    """decklist{cid:n} → (pieces, cost_of)。

    build_piece 刻意忽略 Draw(触发句误标); 此处对非触发抽牌后填 draw_n,
    并注入幸运币(0 费回费法术 —— 引擎在场施放可触发抽牌, 忠实游戏规则)。
    """
    pieces: dict = {}
    cost_of: dict = {}
    for cid in decklist:
        cost = carddb.cost(cid) or 0
        cost_of[cid] = cost
        piece = build_piece(cid, cost, analyzer)
        draws = _draw_n_from_ir(carddb, analyzer, cid)
        if draws:
            piece = dataclasses.replace(piece, draw_n=draws)
        pieces[(cid, cost)] = piece
    cost_of[COIN_CID] = 0
    pieces[(COIN_CID, 0)] = Piece(card_id=COIN_CID, cost=0, mana_gain=1,
                                  is_spell=True)
    return pieces, cost_of


def pieces_completeness(decklist: dict, carddb, pieces: dict,
                        cost_of: dict) -> list[str]:
    """模拟器硬门: 卡表缺牌/pieces 缺键 → 清单非空即禁用(spec §7)。"""
    bad = []
    for cid in decklist:
        if carddb.raw(cid) is None:
            bad.append(f"{cid}: 卡表缺牌")
        elif (cid, cost_of.get(cid)) not in pieces:
            bad.append(f"{cid}: pieces 缺键")
    return bad
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: PASS ×10。

- [ ] **Step 5: 提交**

```bash
git add trainer/sim.py tests/test_rollout.py
git commit -m "feat: trainer.sim pieces 构建(Draw 后填+COIN 注入)与完备性硬门"
```

---

### Task 4: CRN 2ⁿ 枚举 + P_win + 组合维度输出

**Files:**
- Modify: `trainer/sim.py`(追加)
- Test: `tests/test_rollout.py`(追加)

**Interfaces:**
- Consumes: `rollout`(Task 2)。
- Produces:
  - `ANTI_SYNERGY_THRESHOLD = -0.02`
  - `all_keep_sets(offered) -> list[frozenset]`
  - `sim_mulligan(decklist, offered, *, pieces, cost_of, coin=False, orders=200, k_max=8, enemy_totals, survive, seed=0) -> dict`,键:`scores: dict[frozenset, float]`、`keep: tuple`、`per_card_marginal: dict[str, float]`、`pair_synergy: dict[tuple, float]`、`anti_synergy: list[tuple]`、`reject: list[str]`、`launch_hist: dict[frozenset, list[int]]`、`orders`、`k_max`
  - `combo_outputs(scores: dict, offered) -> dict`(纯函数,剥离派生字段)
  - Task 6/7 依赖。

- [ ] **Step 1: 写失败测试**

`tests/test_rollout.py` 追加:

```python
# ---------------- Task4: CRN 枚举 + 组合维度输出 ----------------

import time as _time

from trainer.sim import ANTI_SYNERGY_THRESHOLD, combo_outputs, sim_mulligan


def test_combo_outputs_hand_computed():
    """纯函数: 手算值精确钉死(spec §5.3 公式)。"""
    scores = {frozenset(): 0.40, frozenset(("A",)): 0.50,
              frozenset(("B",)): 0.38, frozenset(("A", "B")): 0.44}
    out = combo_outputs(scores, ("A", "B"))
    assert out["keep"] == ("A",)
    assert out["per_card_marginal"] == {"A": pytest.approx(0.10)}
    assert out["pair_synergy"][("A", "B")] == pytest.approx(-0.04)
    assert out["anti_synergy"] == [("A", "B")]     # -0.04 < -0.02
    assert out["reject"] == ["B"]                  # 0.38-0.40 = -0.02 < 0


def test_sim_mulligan_deterministic_and_ordered():
    deck = {"MOON": 2, "BIG": 1}
    kw = dict(pieces=_sim_pieces(), cost_of=_COST, coin=False, orders=8,
              k_max=5, enemy_totals=(None, 6, 6, 6, 6),
              survive=[1.0] * 5, seed=0)
    r1 = sim_mulligan(deck, ("MOON", "BIG"), **kw)
    r2 = sim_mulligan(deck, ("MOON", "BIG"), **kw)
    assert r1["scores"] == r2["scores"]            # 同种子可复现
    assert len(r1["scores"]) == 4                  # 2^2 个 keep 集
    assert r1["scores"][frozenset(("BIG",))] >= \
        r1["scores"][frozenset()]                  # 留星火不劣于全换


def test_sim_mulligan_launch_hist_bounded():
    deck = {"MOON": 2, "BIG": 1}
    r = sim_mulligan(deck, ("MOON", "BIG"), pieces=_sim_pieces(),
                     cost_of=_COST, orders=5, k_max=4,
                     enemy_totals=(None, 6, 6, 6), survive=[1.0] * 4, seed=1)
    for s, hist in r["launch_hist"].items():
        assert 0 <= sum(hist) <= 5
        assert hist[0] == 0                        # [0] 弃用位


def test_sim_mulligan_perf_small_deck():
    deck = {"MOON": 12, "AUCTION": 2, "DRAW2": 6}
    pieces, cost_of = None, None                   # 手搓 pieces, 免编译器
    pieces = {("MOON", 1): Piece("MOON", 1, segments=(1,), spell_scaled=True,
                                 is_spell=True),
              ("AUCTION", 5): Piece("AUCTION", 5, engine=True),
              ("DRAW2", 3): Piece("DRAW2", 3, is_spell=True, draw_n=2),
              ("COIN", 0): Piece("COIN", 0, mana_gain=1, is_spell=True)}
    cost_of = {"MOON": 1, "AUCTION": 5, "DRAW2": 3, "COIN": 0}
    t0 = _time.time()
    r = sim_mulligan(deck, ("MOON", "AUCTION", "DRAW2"), pieces=pieces,
                     cost_of=cost_of, orders=5, k_max=6,
                     enemy_totals=(30,) * 6, survive=[1.0] * 6, seed=0)
    assert _time.time() - t0 < 5.0, "5 牌序×8 集合×6 回合须 5s 内(性能钉子)"
    assert set(r) >= {"scores", "keep", "per_card_marginal", "pair_synergy",
                      "anti_synergy", "reject", "launch_hist"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `ImportError: cannot import name 'combo_outputs'`。

- [ ] **Step 3: 实现(trainer/sim.py 追加)**

```python
import random
from itertools import combinations

from planner.rollout import rollout

ANTI_SYNERGY_THRESHOLD = -0.02      # spec §5.3


def all_keep_sets(offered) -> list[frozenset]:
    """2ⁿ 候选留集(n≤5, ≤32), 含空集与全集。"""
    out: list[frozenset] = []
    for r in range(len(offered) + 1):
        out.extend(frozenset(c) for c in combinations(offered, r))
    return out


def sim_mulligan(decklist: dict, offered, *, pieces: dict, cost_of: dict,
                 coin: bool = False, orders: int = 200, k_max: int = 8,
                 enemy_totals, survive, seed: int = 0) -> dict:
    """CRN 蒙特卡洛: 每个抽样牌序枚举全部 keep 集(同随机源, 差值低方差)。

    score(S) = Σ_t P(启动=t│S) · survive(t) · P(胜│启动=1.0 常数 v1)
    (结果层校准验证 ≈1 假设, 见 simcal)。返回见模块 Interfaces。
    """
    full = [cid for cid, n in decklist.items() for _ in range(n)]
    missing = [c for c in offered if c not in decklist]
    if missing:
        raise ValueError(f"offered 不在牌表中: {missing}")
    sets = all_keep_sets(offered)
    hist = {s: [0] * (k_max + 1) for s in sets}    # [0] 弃用; 1..k_max 计启动回合
    rng = random.Random(seed)
    for _ in range(orders):
        order = tuple(rng.sample(full, len(full)))
        for s in sets:
            r = rollout(order, offered=offered, keep=tuple(sorted(s)),
                        coin=coin, pieces=pieces, cost_of=cost_of,
                        k_max=k_max, enemy_totals=enemy_totals)
            if r.launch_turn is not None:
                hist[s][r.launch_turn] += 1
    scores = {s: sum(hist[s][t] / orders * survive[t - 1]
                     for t in range(1, k_max + 1) if hist[s][t])
              for s in sets}
    return {"scores": scores, "orders": orders, "k_max": k_max,
            "launch_hist": hist, **combo_outputs(scores, offered)}


def combo_outputs(scores: dict, offered) -> dict:
    """组合维度输出契约(spec §5.3, 全部由 2ⁿ 打分表组合而来)。"""
    best = max(scores, key=scores.get)
    base = scores[frozenset()]
    per_card_marginal = {c: scores[best] - scores[best - frozenset((c,))]
                         for c in sorted(best)}
    pair_synergy: dict = {}
    anti_synergy: list = []
    for a, b in combinations(sorted(offered), 2):
        syn = (scores.get(frozenset((a, b)), 0.0)
               - scores.get(frozenset((a,)), 0.0)
               - scores.get(frozenset((b,)), 0.0) + base)
        pair_synergy[(a, b)] = syn
        if syn < ANTI_SYNERGY_THRESHOLD:
            anti_synergy.append((a, b))
    reject = [c for c in sorted(offered)
              if c not in best and scores[frozenset((c,))] - base < 0]
    return {"keep": tuple(sorted(best)),
            "per_card_marginal": per_card_marginal,
            "pair_synergy": pair_synergy,
            "anti_synergy": anti_synergy, "reject": reject}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: PASS ×14。

- [ ] **Step 5: 提交**

```bash
git add trainer/sim.py tests/test_rollout.py
git commit -m "feat: CRN 同牌序枚举 2ⁿ keep 集 + P_win 三因子打分 + 组合维度输出(per_card/pair/anti/reject)"
```

---

### Task 5: 语料曲线提取(enemy_hp_curve / survive_curve)

**Files:**
- Modify: `trainer/sim.py`(追加)
- Test: `tests/test_rollout.py`(追加)

**Interfaces:**
- Consumes: material 行结构 `{"snap": {"turn", "my_turn", "me": {"hand", "hp"...}, "opp": {"hp", "armor"}}, "src", "result"}`(旧口径 223 行即满足)。
- Produces:
  - `ENEMY_LADDER = (26, 30, 40, 48, 56)`(2026-09-15 用户增补:固定血甲档位表,产品侧用)
  - `ladder_totals(rung: int, k_max: int = 8) -> list[int]`(全回合恒定档位)
  - `enemy_hp_curve(rows, k_max=8, quantile=0.5) -> list[int | None]`(1-based;**仅校准侧用**)
  - `survive_curve(rows, k_max=8) -> list[float]`
  - Task 6/7 依赖。

- [ ] **Step 1: 写失败测试**

`tests/test_rollout.py` 追加:

```python
# ---------------- Task5: 语料曲线 + 血甲档位表 ----------------

from trainer.sim import (ENEMY_LADDER, enemy_hp_curve, ladder_totals,
                         survive_curve)


def test_enemy_ladder_totals_constant():
    assert ENEMY_LADDER == (26, 30, 40, 48, 56)
    assert ladder_totals(30, k_max=3) == [30, 30, 30]


def _row(turn, opp_hp, game="g1", hand_n=4, result=1):
    return {"snap": {"turn": turn, "my_turn": True,
                     "me": {"hand": [{"cid": "X", "cost": 1}] * hand_n,
                            "hp": 30, "armor": 0, "mana": 3,
                            "spellpower": 0, "board": []},
                     "opp": {"hp": opp_hp, "armor": 0}},
            "src": f"{game}.power.log#g1T{turn}", "result": result}


def test_enemy_hp_curve_median_by_turn():
    rows = [_row(1, 30), _row(1, 30), _row(2, 28), _row(2, 24), _row(2, 30)]
    assert enemy_hp_curve(rows, k_max=3) == [30, 28, None]


def test_survive_curve_fraction_alive():
    rows = [_row(1, 30, game="a"), _row(2, 30, game="a"), _row(3, 30, game="a"),
            _row(1, 30, game="b"), _row(2, 30, game="b"),
            _row(1, 30, game="c")]
    assert survive_curve(rows, k_max=3) == pytest.approx([1.0, 2 / 3, 1 / 3])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `ImportError: cannot import name 'enemy_hp_curve'`。

- [ ] **Step 3: 实现(trainer/sim.py 追加)**

```python
ENEMY_LADDER = (26, 30, 40, 48, 56)   # 血甲档位表(2026-09-15 用户增补):
                                      # 产品侧(advise)逐档判定, 校准侧用真实血甲


def ladder_totals(rung: int, k_max: int = 8) -> list:
    """全回合恒定档位 —— rollout 的 enemy_totals 直填。"""
    return [rung] * k_max


def enemy_hp_curve(rows: list, k_max: int = 8,
                   quantile: float = 0.5) -> list:
    """回合 → 敌方有效血甲(hp+armor)经验分位; 无样本回合 None。"""
    by_turn: dict = {}
    for r in rows:
        snap = r.get("snap") or {}
        if snap.get("my_turn"):
            t = snap["turn"]
            by_turn.setdefault(t, []).append(
                snap["opp"]["hp"] + snap["opp"]["armor"])
    out = []
    for t in range(1, k_max + 1):
        v = by_turn.get(t)
        out.append(None if not v
                   else sorted(v)[min(len(v) - 1, int(quantile * len(v)))])
    return out


def survive_curve(rows: list, k_max: int = 8) -> list:
    """P(存活至我的第 K 回合) ≈ 有该回合行的局占比(游戏时长代理)。"""
    max_turn: dict = {}
    for r in rows:
        snap = r.get("snap") or {}
        if snap.get("my_turn"):
            g = r["src"].split("#")[0]
            max_turn[g] = max(max_turn.get(g, 0), snap["turn"])
    n = len(max_turn) or 1
    return [sum(1 for mx in max_turn.values() if mx >= t) / n
            for t in range(1, k_max + 1)]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: PASS ×16。

- [ ] **Step 5: 提交**

```bash
git add trainer/sim.py tests/test_rollout.py
git commit -m "feat: 语料曲线提取——敌方血甲分位曲线 + 存活曲线(三因子的真实语料侧)"
```

---

### Task 6: 三层一致性校准(trainer/simcal.py)

**Files:**
- Create: `trainer/simcal.py`
- Test: `tests/test_rollout.py`(追加)

**Interfaces:**
- Consumes: `rollout`/`best_line`/`initial_state`(planner)、`build_sim_pieces`/`pieces_completeness`/`enemy_hp_curve`/`survive_curve`(Task 3/5)、corpus `_meta` 行结构(`players`/`mulligan`/`session`/`game_index`/`decklist`)。
- Produces:
  - 阈值常量 `TRAJ_MAX_MEDIAN_HAND_DIFF = 1.5`、`LAUNCH_MAX_ABS_DIFF = 0.20`、`RESULT_MIN_WIN_RATE = 0.60`
  - `my_mulligan(meta: dict, battletag: str) -> tuple[tuple, tuple, bool]`(offered/kept/coin)
  - `first_lethal_turns(rows, pieces, cost_of) -> dict[str, int]`(启动层真实侧)
  - `calibrate(deck_dir, rows, *, battletag, carddb, analyzer, orders=200, k_max=8, seed=0) -> dict`(键见实现;`pass` 布尔总在)
  - `print_calibrate_report(rep: dict) -> None`
  - Task 7 CLI 依赖。

- [ ] **Step 1: 写失败测试**

`tests/test_rollout.py` 追加:

```python
# ---------------- Task6: 三层校准 ----------------

from trainer.simcal import (LAUNCH_MAX_ABS_DIFF, first_lethal_turns,
                            my_mulligan)


def test_my_mulligan_finds_battletag_side():
    meta = {"players": {"1": {"name": "别人"}, "2": {"name": "湫然#51704"}},
            "mulligan": {"2": {"offered": ["A", "B", "C", "D"],
                               "kept": ["A"]}}}
    offered, kept, coin = my_mulligan(meta, "湫然#51704")
    assert offered == ("A", "B", "C", "D") and kept == ("A",) and coin


def test_first_lethal_turns_takes_first_only():
    pieces = {("BIG", 4): Piece("BIG", 4, segments=(6,), spell_scaled=True,
                                is_spell=True)}
    rows = [
        {"snap": {"turn": 2, "my_turn": True, "spellpower": 0,
                  "me": {"mana": 4, "hand": [{"cid": "BIG", "cost": 4}],
                         "board": []},
                  "opp": {"hp": 6, "armor": 0}},
         "src": "g1.power.log#g1T2", "result": 1},
        {"snap": {"turn": 4, "my_turn": True, "spellpower": 0,
                  "me": {"mana": 4, "hand": [{"cid": "BIG", "cost": 4}],
                         "board": []},
                  "opp": {"hp": 6, "armor": 0}},
         "src": "g1.power.log#g1T4", "result": 1},
        {"snap": {"turn": 9, "my_turn": False, "spellpower": 0,
                  "me": {"mana": 9, "hand": [], "board": []},
                  "opp": {"hp": 6, "armor": 0}},
         "src": "g1.power.log#g1T9", "result": 1},
    ]
    assert first_lethal_turns(rows, pieces, {"BIG": 4}) == {"g1": 2}
```

再加一个 calibrate 的合成端到端(临时语料目录 + 假卡表;校准在完全自洽的合成数据上必须 pass):

```python
def test_calibrate_passes_on_self_consistent_synthetic(tmp_path):
    """合成自洽语料: 模拟器策略/谓词与数据生成同源 → 三层全过。"""
    from trainer.simcal import calibrate
    deck_dir = tmp_path / "奇迹德"
    deck_dir.mkdir()
    meta = {"_meta": True, "session": "S", "game_index": 1,
            "decklist": {"MOON": 2, "BIG": 1},
            "players": {"1": {"name": "湫然#51704"}},
            "mulligan": {"1": {"offered": ["BIG", "MOON"],
                               "kept": ["BIG"]}}}
    (deck_dir / "S_g01.jsonl").write_text(
        json.dumps(meta, ensure_ascii=False) + "\n[]\n", encoding="utf-8")
    # 手牌数须与模拟轨迹自洽: keep=[BIG]+补抽1 → T1 手牌2;
    # T2/T4 自然抽后 3。T4 手牌=[星火,月火,月火] 费4 → 真实侧同回合可斩。
    rows = [_row(1, 30, game="S_g01", hand_n=2),
            _row(2, 24, game="S_g01", hand_n=3),
            _row(3, 12, game="S_g01", hand_n=3),
            _row(4, 6, game="S_g01", hand_n=3)]
    rows[2]["snap"]["me"]["hand"] = [{"cid": "BIG", "cost": 4},
                                     {"cid": "MOON", "cost": 1},
                                     {"cid": "MOON", "cost": 1}]
    rows[2]["snap"]["me"]["mana"] = 4
    rows[3]["snap"]["me"]["hand"] = [{"cid": "BIG", "cost": 4},
                                     {"cid": "MOON", "cost": 1},
                                     {"cid": "MOON", "cost": 1}]
    rows[3]["snap"]["me"]["mana"] = 4
    db = _db(tmp_path, CARDS)
    rep = calibrate(deck_dir, rows, battletag="湫然#51704", carddb=db,
                    analyzer=EffectAnalyzer(db), orders=4, k_max=5, seed=0)
    assert rep["pass"] is True, rep
    assert set(rep["layers"]) == {"trajectory", "launch", "result"}
```

注意:该端到端若因合成数据与策略口径细节不吻合而失败,允许放宽**合成夹具**的手牌数/行数使其自洽——但三层结构与 pass 语义断言不许动。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trainer.simcal'`。

- [ ] **Step 3: 实现 trainer/simcal.py**

```python
"""三层一致性校准 —— 模拟器 vs 真实语料(v3.1 设计 §6)。

合成→合成的自洽只证明模拟器无 bug; 合成→真实的三层一致才证明无偏:
轨迹层(手牌规模曲线)/启动层(P(启动≤K) 曲线)/结果层(启动→胜转化)。
任一层失败 → pass=False, CLI 退出码非零, 模拟器对该卡组禁用。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from planner.dfs import best_line
from planner.pieces import Piece
from planner.rollout import rollout
from planner.simstate import initial_state

from .sim import (build_sim_pieces, enemy_hp_curve, pieces_completeness,
                  survive_curve)

TRAJ_MAX_MEDIAN_HAND_DIFF = 1.5    # 轨迹层: 手牌规模中位差上限(张)
LAUNCH_MAX_ABS_DIFF = 0.20         # 启动层: P(启动≤K) 逐点绝对差上限
RESULT_MIN_WIN_RATE = 0.60         # 结果层: 启动局胜率下限(≈1 预期, 宽容小样本)


def my_mulligan(meta: dict, battletag: str) -> tuple[tuple, tuple, bool]:
    """corpus _meta → (offered, kept, coin)。coin = offered 4 张(v1 口径,
    MULLIGAN_AI"幸运币不参与留牌"的推断; 校准容差吸收误差)。"""
    for pid, p in meta.get("players", {}).items():
        if p.get("name") == battletag:
            m = meta.get("mulligan", {}).get(pid) or {}
            offered = tuple(m.get("offered") or [])
            return offered, tuple(m.get("kept") or []), len(offered) == 4
    return (), (), False


def _game_key(row: dict) -> str:
    """行 → 局键: src 的 '#' 前缀去掉 '.power.log' —— 与 meta 的
    f"{session}_g{idx:02d}" 同域(两侧必须共用本函数, 否则键失配)。"""
    return row["src"].split("#")[0].removesuffix(".power.log")


def _piece_of(pieces: dict, cid: str, cost_of: dict) -> Piece:
    cost = cost_of.get(cid, 0)
    return pieces.get((cid, cost)) or Piece(card_id=cid, cost=cost)


def first_lethal_turns(rows: list, pieces: dict, cost_of: dict) -> dict:
    """每局首个 best_line 可斩的我方回合(启动层真实侧)。

    快照缺费的手牌宁漏勿错跳过; 返回 {局键: 回合}, 未启动局缺省由调用方
    以 None 补全。局键 = _game_key(row)。"""
    out: dict = {}
    for r in rows:
        snap = r.get("snap") or {}
        if not snap.get("my_turn"):
            continue
        g = _game_key(r)
        if g in out:
            continue
        me, opp = snap["me"], snap["opp"]
        hand = tuple((c["cid"], c["cost"]) for c in me["hand"]
                     if c["cost"] is not None)
        engines = sum(1 for b in me["board"]
                      if _piece_of(pieces, b["cid"], cost_of).engine)
        st = initial_state(me["mana"], hand, sp=me.get("spellpower", 0),
                           engines=engines)
        if best_line(st, pieces,
                     enemy_total=opp["hp"] + opp["armor"]).lethal:
            out[g] = snap["turn"]
    return out


def calibrate(deck_dir, rows: list, *, battletag: str, carddb, analyzer,
              orders: int = 200, k_max: int = 8, seed: int = 0) -> dict:
    metas = []
    for p in sorted(Path(deck_dir).glob("*.jsonl")):
        meta = json.loads(
            p.read_text(encoding="utf-8").splitlines()[0])
        if meta.get("_meta"):
            metas.append(meta)
    decklist = next((m.get("decklist") for m in metas if m.get("decklist")),
                    None)
    if not decklist:
        return {"pass": False, "reason": "语料无 decklist, 无法构建牌表",
                "layers": {}}
    pieces, cost_of = build_sim_pieces(decklist, carddb, analyzer)
    bad = pieces_completeness(decklist, carddb, pieces, cost_of)
    if bad:
        return {"pass": False, "reason": "pieces 不完备(硬门, spec §7)",
                "layers": {}, "detail": bad}

    ehp = enemy_hp_curve(rows, k_max=k_max)
    surv = survive_curve(rows, k_max=k_max)
    full = [cid for cid, n in decklist.items() for _ in range(n)]

    # 真实侧: 每局手牌规模曲线 / 首可斩回合 / 胜负
    real_hs: dict = {}
    result_of: dict = {}
    for r in rows:
        g = _game_key(r)
        snap = r.get("snap") or {}
        if snap.get("my_turn"):
            real_hs.setdefault(g, {})[snap["turn"]] = len(snap["me"]["hand"])
        if "result" in r and g not in result_of:
            result_of[g] = r["result"]
    real_launch = first_lethal_turns(rows, pieces, cost_of)
    games = sorted(real_hs)

    # 模拟侧: 每局用真实 keep + 逐局种子推演一次
    diffs: list[float] = []
    sim_launch: list[int | None] = []
    n_sim = 0
    for i, m in enumerate(metas):
        offered, kept, coin = my_mulligan(m, battletag)
        if not offered:
            continue
        rng = random.Random(seed * 1000003 + i)
        res = rollout(tuple(rng.sample(full, len(full))), offered=offered,
                      keep=kept, coin=coin, pieces=pieces, cost_of=cost_of,
                      k_max=k_max, enemy_totals=ehp)
        n_sim += 1
        sim_launch.append(res.launch_turn)
        g = f"{m['session']}_g{m['game_index']:02d}"
        for t, hs in enumerate(res.hand_sizes, 1):
            if t in real_hs.get(g, {}):
                diffs.append(abs(hs - real_hs[g][t]))

    def _median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0.0

    # 轨迹层
    traj_med = _median(diffs)
    traj_ok = traj_med <= TRAJ_MAX_MEDIAN_HAND_DIFF
    # 启动层: P(启动≤K) 双侧逐点差
    launch_diffs = []
    for K in range(1, k_max + 1):
        real_p = (sum(1 for g in games if real_launch.get(g, k_max + 1) <= K)
                  / len(games)) if games else 0.0
        sim_p = (sum(1 for x in sim_launch if x is not None and x <= K)
                 / n_sim) if n_sim else 0.0
        launch_diffs.append(abs(real_p - sim_p))
    launch_max = max(launch_diffs) if launch_diffs else 0.0
    launch_ok = launch_max <= LAUNCH_MAX_ABS_DIFF
    # 结果层: 真实启动局的胜率
    launched_games = [g for g in real_launch if g in result_of]
    win_rate = (sum(result_of[g] for g in launched_games) / len(launched_games)
                ) if launched_games else 1.0
    result_ok = win_rate >= RESULT_MIN_WIN_RATE

    layers = {
        "trajectory": {"ok": traj_ok, "median_hand_diff": traj_med,
                       "n_points": len(diffs)},
        "launch": {"ok": launch_ok, "max_abs_diff": launch_max,
                   "curve_diffs": launch_diffs},
        "result": {"ok": result_ok, "win_rate": win_rate,
                   "n_launched": len(launched_games)},
    }
    return {"pass": all(v["ok"] for v in layers.values()),
            "games": len(games), "sim_games": n_sim, "layers": layers}


def print_calibrate_report(rep: dict) -> None:
    if "reason" in rep:
        print(f"✗ 校准未运行: {rep['reason']}")
        if rep.get("detail"):
            print("  " + " │ ".join(rep["detail"][:10]))
        return
    lj = {k: ("通过" if v["ok"] else "未过") for k, v in rep["layers"].items()}
    tr = rep["layers"]["trajectory"]
    la = rep["layers"]["launch"]
    re = rep["layers"]["result"]
    print(f"── 三层校准: {'PASS' if rep['pass'] else 'FAIL'} "
          f"({rep['games']} 真实局 / {rep['sim_games']} 模拟局) ──")
    print(f"  轨迹层[{lj['trajectory']}] 手牌规模中位差 "
          f"{tr['median_hand_diff']:.2f} (≤{TRAJ_MAX_MEDIAN_HAND_DIFF}, "
          f"{tr['n_points']} 点)")
    print(f"  启动层[{lj['launch']}] P(启动≤K) 最大绝对差 "
          f"{la['max_abs_diff']:.3f} (≤{LAUNCH_MAX_ABS_DIFF})")
    print(f"  结果层[{lj['result']}] 启动局胜率 {re['win_rate']:.2f} "
          f"(≥{RESULT_MIN_WIN_RATE}, {re['n_launched']} 局)")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: PASS ×19。

- [ ] **Step 5: 提交**

```bash
git add trainer/simcal.py tests/test_rollout.py
git commit -m "feat: 三层一致性校准(轨迹/启动/结果)——模拟器 vs 真实语料的自验证通道"
```

---

### Task 7: CLI 接线(`python -m trainer sim advise|calibrate`)

**Files:**
- Modify: `trainer/sim.py`(追加 main)
- Modify: `trainer/__main__.py:56-59`(分发)
- Test: `tests/test_rollout.py`(追加)

**Interfaces:**
- Consumes: 前全部任务;`Config.load`、`CardDB(cfg.cache_dir / "cards.zh.json")`、`material.load_material`。
- Produces: `sim.main(argv=None) -> int`;子命令 `advise --hand 卡1,卡2[,卡3,卡4] [--coin] [--orders N]`、`calibrate [--orders N]`。

- [ ] **Step 1: 写失败测试**

`tests/test_rollout.py` 追加:

```python
# ---------------- Task7: CLI ----------------

from trainer import sim as sim_mod


def test_sim_main_requires_subcommand():
    with pytest.raises(SystemExit) as ei:
        sim_mod.main(["--config", "config.yaml"])
    assert ei.value.code == 2              # argparse 用法错误


def test_sim_main_unknown_subcommand():
    with pytest.raises(SystemExit) as ei:
        sim_mod.main(["frobnicate"])
    assert ei.value.code == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_rollout.py -v`
Expected: FAIL — `AttributeError: module 'trainer.sim' has no attribute 'main'`。

- [ ] **Step 3: 实现(trainer/sim.py 追加)**

```python
def _latest_decklist(corpus: Path, deck: str) -> dict | None:
    """语料该卡组目录最新一样本的 decklist(牌表 0 变化, 任取即可)。"""
    deck_dir = Path(corpus) / deck
    for p in sorted(deck_dir.glob("*.jsonl"), reverse=True):
        meta = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        if meta.get("_meta") and meta.get("decklist"):
            return meta["decklist"]
    return None


def main(argv=None) -> int:
    import argparse
    import sys
    from pathlib import Path

    from hsbot.analysis import EffectAnalyzer
    from hsbot.carddb import CardDB
    from hsbot.config import Config

    argv = list(sys.argv[1:]) if argv is None else list(argv)
    ap = argparse.ArgumentParser(prog="trainer sim",
                                 description="留牌模拟器(v3.1, 0 变化自闭卡组)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--deck", default=None)
    ap.add_argument("--battletag", default=None)
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--orders", type=int, default=200)
    ap.add_argument("--k-max", dest="k_max", type=int, default=8)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_adv = sub.add_parser("advise", help="给定起手 → CRN 模拟出组合维度建议")
    p_adv.add_argument("--hand", required=True, help="逗号分隔的卡 ID")
    p_adv.add_argument("--coin", action="store_true", help="后手")
    p_adv.add_argument("--enemy", type=int, default=30,
                       help="敌方血甲档位(默认 30; 档位表 26/30/40/48/56)")
    sub.add_parser("calibrate", help="三层一致性校准(vs 真实语料)")
    args = ap.parse_args(argv)

    cfg = Config.load({"config": args.config})
    deck = args.deck or cfg.deck_name
    battletag = args.battletag or cfg.battletag
    corpus = Path(args.corpus or cfg.training_dir)
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    analyzer = EffectAnalyzer(carddb)
    out = Path(__file__).resolve().parent / "data" / deck

    decklist = _latest_decklist(corpus, deck)
    if not decklist:
        raise SystemExit(f"语料 {corpus / deck} 无 decklist, 模拟器不适用")
    pieces, cost_of = build_sim_pieces(decklist, carddb, analyzer)
    bad = pieces_completeness(decklist, carddb, pieces, cost_of)
    if bad:
        raise SystemExit("模拟器硬门未过(spec §7, 该卡组禁用):\n  "
                         + "\n  ".join(bad))

    if args.cmd == "advise":
        from .material import load_material
        rows = load_material(out)
        offered = tuple(c.strip() for c in args.hand.split(",") if c.strip())
        rep = sim_mulligan(decklist, offered, pieces=pieces, cost_of=cost_of,
                           coin=args.coin, orders=args.orders,
                           k_max=args.k_max,
                           enemy_totals=ladder_totals(args.enemy,
                                                      k_max=args.k_max),
                           survive=survive_curve(rows, k_max=args.k_max))
        _zh = lambda cid: carddb.name(cid) or cid  # noqa: E731
        print(f"── 模拟留牌建议 ({args.orders} 牌序, CRN {2 ** len(offered)} 集) ──")
        print("留: " + "、".join(_zh(c) for c in rep["keep"]))
        for c, v in sorted(rep["per_card_marginal"].items(),
                           key=lambda kv: -kv[1]):
            print(f"  {_zh(c)} 边际 {v:+.1%}")
        for (a, b), v in rep["pair_synergy"].items():
            if abs(v) >= 0.02:
                tag = "同留" if v > 0 else "不宜同留"
                print(f"  {_zh(a)}+{_zh(b)} {tag} {v:+.1%}")
        if rep["reject"]:
            print("换: " + "、".join(_zh(c) for c in rep["reject"]))
        return 0

    from .simcal import calibrate, print_calibrate_report
    from .material import load_material
    rep = calibrate(corpus / deck, load_material(out), battletag=battletag,
                    carddb=carddb, analyzer=analyzer, orders=args.orders,
                    k_max=args.k_max)
    print_calibrate_report(rep)
    return 0 if rep["pass"] else 1
```

注意:`carddb.name(cid)` 若 CardDB 无此方法,改用仓库现存的卡名查询函数(以 `grep -n "def name" hsbot/carddb.py` 为准),不新造第二套。

`trainer/__main__.py` 在 mulligan 分发块后追加:

```python
    if "sim" in argv:                       # 留牌模拟器(v3.1, 独立子命令族)
        i = argv.index("sim")
        from . import sim
        return sim.main(argv[:i] + argv[i + 1:])
```

`trainer/sim.py` 顶部补 `import json` 与 `from pathlib import Path`(若尚未导入)。

- [ ] **Step 4: 跑测试确认通过 + 全套回归 + 零触碰验证**

Run: `python3 -m pytest tests/ -q && git diff --stat HEAD~7 -- hsbot/ | tail -1`
Expected: 210+新增 全绿;`hsbot/` 无 diff 输出(live 零触碰的结构性证明)。

- [ ] **Step 5: 提交**

```bash
git add trainer/sim.py trainer/__main__.py tests/test_rollout.py
git commit -m "feat: trainer sim CLI(advise/calibrate)——组合维度建议与三层校准入口; live 零触碰"
```

---

### Task 8: 真实语料最小可信验证 + 文档回填

**Files:**
- Modify: `docs/MULLIGAN_AI.md`(追加 v3.1 节)
- 无新代码;执行 + 解读 + 记录。

**Interfaces:**
- Consumes: Task 7 的 CLI;`data/training/奇迹德`(83 局)、`trainer/data/奇迹德/material.jsonl`(223 行,若过期先 `python -m trainer material --deck 奇迹德` 重建)。

- [ ] **Step 1: 重建素材(确保口径新鲜)**

Run: `python3 -m trainer material --deck 奇迹德`
Expected: `素材: N 个决策点(...局...)` 无重放失败。

- [ ] **Step 2: 跑三层校准**

Run: `python3 -m trainer sim --deck 奇迹德 --orders 200 calibrate`(注意: 旗标须在子命令前,与 trainer/mulligan CLI 同惯例)
Expected 输出形如:

```
── 三层校准: PASS/FAIL (N 真实局 / M 模拟局) ──
  轨迹层[...] 手牌规模中位差 X.XX (≤1.5, NNN 点)
  启动层[...] P(启动≤K) 最大绝对差 0.XXX (≤0.20)
  结果层[...] 启动局胜率 0.XX (≥0.60, NN 局)
```

退出码 0=PASS。记录耗时(分钟级预算内;超出→记入报告,开后续优化任务,不在本计划内擅自改算法)。

- [ ] **Step 3: 解读三分支(按实际结果走其一)**

- **PASS** → Step 4;顺手跑 `python3 -m trainer sim --deck 奇迹德 --orders 10 advise --hand <从任一 meta 的 offered 取真实 3 卡>`(顶层旗标在子命令前,**子命令旗标 --hand/--coin/--enemy 在子命令后**)人工 sanity(建议方向与卡组常识不悖)。
- **轨迹层未过** → 策略偏倚:检查模拟手牌规模系统性偏大/偏小,调 `_policy_playable` 优先级(只动策略,不动 play/dfs);每次调整后重跑本步,记入 v3.1 节。
- **启动/结果层未过** → 谓词或血甲口径:检查 `enemy_hp_curve` 分位选择、`first_lethal_turns` 的 engines 快照重建;若真实启动局胜率低(<0.60),逐局列出启动却输的样本归因(冰甲/破坏/斩杀计算口径),把发现写成 v3.1 节的"已知偏差"清单。**禁止**为凑 PASS 放宽阈值常量——阈值改动=设计变更,须回 spec 立案。

- [ ] **Step 4: MULLIGAN_AI.md 回填 v3.1 节**

在文末追加(沿用现有文风,数值填实测):

```markdown
## v3.1 自闭卡组模拟器(2026-09-15 设计, 本日实施)

0 变化自闭卡组(奇迹德 83/85 局同一 deck_code)的留牌从学习问题重构为计算
问题: CRN 蒙特卡洛 rollout 直接算 P(启动≤K│keep集), 组合维度输出
(per_card/pair/anti/reject)沿用 v3 §5.3 契约。设计全文见
docs/superpowers/specs/2026-09-15-mulligan-simulator-design.md。

用法: `python -m trainer sim advise|calibrate`。
三层校准实测(2026-09-15, 83 局): 轨迹层 <填>, 启动层 <填>, 结果层 <填>,
耗时 <填>。已知偏差: <填 or 无>。

分级级联中的位置: 模拟器(0 变化卡组) → TabPFN v2 → LR → 统计表;
live 接线(蒸馏系数)待 v3 §6 落地后另立计划。live 进程零改动。
```

- [ ] **Step 5: 全套回归 + 提交**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿。

```bash
git add docs/MULLIGAN_AI.md
git commit -m "docs: v3.1 模拟器上线记录——三层校准实测结果与已知偏差回填 MULLIGAN_AI"
```

---

## Self-Review 记录

- **Spec 覆盖**:§3 rollout→Task 1/2;§4 谓词→Task 2(best_line 复用)+Task 5(血甲分布);§5 CRN/组合输出→Task 4;§6 三层校准→Task 6/8;§7 硬门→Task 3;§10 切片 7 文档→Task 8。切片 6(蒸馏/live)按"范围裁定"推迟,spec §8 config 键随其推迟——已在 Global Constraints 备案。
- **占位符扫描**:无 TBD/TODO;所有代码步骤含完整代码;两个"执行时按实测调整"点(Task 3 编译文本、Task 8 解读分支)均给出判定规则与禁区,非占位。
- **类型一致性**:`rollout`/`RolloutResult`/`build_sim_pieces`/`pieces_completeness`/`sim_mulligan`/`combo_outputs`/`enemy_hp_curve`/`survive_curve`/`my_mulligan`/`first_lethal_turns`/`calibrate` 各任务间签名与字段名逐一核对一致;`cost_of` 含 COIN 的约定在 Task 2 Interfaces 与 Task 3 实现两端对齐。
