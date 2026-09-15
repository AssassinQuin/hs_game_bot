# 统一模拟快照层设计 —— SimSnapshot: 一切模拟与建议的单一状态载体

> 状态:设计定稿,待实施(实施计划用 writing-plans 另行展开;代码时机与分支由用户届时裁定)。
> 决策记录:2026-09-15 与用户定版——两通道架构原则(§1);形态选型 SimSnapshot 统一状态
> (否决"仅统一装配层"——不产生一等模拟快照;否决"对齐 store 快照 schema"——破坏
> planner 依赖铁律且哈希推演冲突);增补预测-实测对账通道(分歧日志, §5, 用户指定)。
> 上游文档:[ARCHITECTURE.md](../../ARCHITECTURE.md)(分层不变式)、
> [留牌模拟器 v3.1](2026-09-15-mulligan-simulator-design.md)(rollout/校准现状)、
> [EFFECTS_DESIGN.md](../../EFFECTS_DESIGN.md)(效果 IR 编译层)。

---

## 0. 问题:模拟状态的三处分裂(现状证据)

| # | 证据 | 位置 |
|---|---|---|
| 1 | live 斩杀线的 store→SimState 投影 ~70 行内联在编排函数里 | `analysis.lethal_plan`(analysis.py) |
| 2 | rollout 多回合推演用 5 个散装循环变量(hand/stream/engines/sp/disc_hand)在回合间手动搬运,每回合重建 initial_state | `planner/rollout.py` 回合循环 |
| 3 | 推演输出全是专用结论(Plan / RolloutResult.launch_turn),没有"模拟后的游戏快照"一等产物 | 全部消费方 |

后果:每个新模拟需求都要重新发明装配与跨回合状态搬运;模拟结果与真实快照不同构,无法做字段级 diff——这正是对账(§5)做不起来的根因。

## 1. 两通道架构总纲

```
通道 1(真实)  Power.log → pipeline/watcher → store(唯一状态权威)
                ├→ 快照持久化 / 训练语料 / 真实结果
                └→ 角色:通道 2 的初始条件供给 + 对账靶(§5)

通道 2(模拟)  效果 IR(effects 编译,转移语义) + 游戏快照(初始条件)
                → SimSnapshot 推演(play / advance_turn)
                → 一切下游模拟与建议:live 斩杀线 / 留牌模拟器 /
                  未来通用预测器(快照+出牌序列→下一刻快照)
```

后续一切模拟、建议类功能只长在通道 2 上;通道 1 的角色 = 初始条件供给 + 对账靶(纯展示类输出直读 store,不属于模拟/建议,不受本原则约束)。ARCHITECTURE.md 增补此总纲。

## 2. 数据模型:SimSnapshot

演进 `planner/simstate.py`(模块名保留——import 面与改动面重合,不做无谓改名;类 `SimState` → `SimSnapshot`)。frozen + 全 tuple,可哈希纪律不变。

```python
@dataclass(frozen=True)
class SimSnapshot:
    # ── 新增游戏事实(原先散在 kwargs / 循环变量) ──
    turn: int                  # 当前回合(1-based),推演期只增
    # ── 现有 9 字段,语义零变化 ──
    mana: int
    hand: tuple                # ((cid, cost), ...) 升序规范化
    sp: int                    # 当前法强
    disc_hand: int             # 在身的手牌法术减费余额
    disc_next: int             # "下一张"减费面值(一次性)
    engines: int               # 在场施法抽牌引擎数
    drawn: int                 # 已消耗的未知抽牌数(期望折算用)
    known_draws: tuple         # ((cid, cost), ...) 队头=最先抽到(消耗式)
    face: int                  # 本回合内累计确定伤害
    enemy_total: int | None    # 本回合敌方有效血甲(live=store;sim=曲线/档位表)
    board_atk: int = 0         # 我方场面可打脸攻击
    dealt_total: int = 0       # 跨回合累计已造成确定伤害
```

## 3. 转移函数与 memo_key(全部纯函数)

| 函数 | 语义 |
|---|---|
| `play(snap, i, pieces)` | 完全沿用现行 simstate.play 契约(含热路径 `__new__` 构造);`turn/enemy_total/board_atk/dealt_total` 原样透传 |
| `advance_turn(snap, *, enemy_total)` | `turn+1`;`mana=min(10, turn+1)`;`face → dealt_total` 累计后清零;T≥2 自然抽 1(known 队头入手,空则 `drawn+1`);`disc_next` 清零(回合级一次性余额,随回合过期);`sp/disc_hand/engines/known_draws` 跨回合保留(口径=rollout 现行 v1)。`enemy_total=None` 即该回合无数据(消费方跳过斩杀/启动判定) |
| `memo_key(snap)` | 七元组投影 `(mana, hand, sp, disc_hand, disc_next, engines, known_draws)`;后缀不变性论证沿用 dfs.py 模块注释——face/drawn/dealt_total/turn/enemy_total/board_atk 均不进转移,故不进键 |

`dfs.best_line` 签名随之简化:`board_atk/enemy_total` 从快照读(不再 kwargs);斩杀判定口径统一为

```
lethal ⇔ face_det + board_atk ≥ enemy_total − dealt_total − face
```

(rollout 启动判定的 `replace(face=0)` 技巧被该统一算式吸收)。`exp_per_draw` 保留 kwarg——台账派生的期望量,不是状态事实。

## 4. 装配与消费方迁移

装配按数据源分置(依赖铁律不破):

- **live 装配**:lethal_plan 内 ~70 行投影抽成 analysis.py 模块级
  `assemble_snapshot(st, knowledge, analyzer) → (SimSnapshot, pieces)`——glue 留在 hsbot 侧,planner 仍不 import store;lethal_plan 变薄编排。
- **sim 装配**:rollout 头部 full_order/keep/coin/换牌补抽(抽样语义)保留,产物直接构造 `SimSnapshot(turn=1, enemy_total=曲线[0], …)`。

| 消费方 | 迁移后 |
|---|---|
| `dfs.best_line` | 吃 SimSnapshot;memo_key 做坍缩键,坍缩效率资产不动 |
| `rollout` | 5 个散装循环变量消失,全部寄居快照;每回合快照入 `snapshots` 序列 |
| 启动判定/脚本策略 | 成为快照序列的消费方(逻辑不变,换读法) |
| `RolloutResult` | **公共契约保留**:launch_turn/hand_sizes/engines 字段不变(simcal/测试零改动),新增 `snapshots` 字段,由快照序列派生 |

## 5. 预测-实测对账通道(分歧日志)

**目标**:真实运行与模拟结果不一致时,产出带溯源的结构化日志,让分歧可直接定位到 effects 语法表 / Piece 语义 / play 转移,形成收敛闭环:

```
发现分歧 → 改规则 → COMPILER_VERSION+1 → 缓存全量重编译 → 分歧消失
```

**机制**(transition 级对账,非整线终态对比——玩家未必照建议出牌,逐牌对账对偏差免疫):

```
我方打出的每张牌结算后(power.log 事件落地)
   ├─ 模拟侧:SimSnapshot + play() 重放该牌 → 预测转移
   │          (实付费/脸伤/抽牌数/回费/引擎触发)
   ├─ 真实侧:store 快照演化 → 实测转移(同字段口径)
   ▼
diff 纯函数(hsbot/recon.py,零 IO)
   │ 不一致
   ▼
data/logs/sim_divergence.jsonl ← watcher 侧 AuditExporter 落盘
```

JSONL 条目:`{ts, game_id, card_id, piece:{…全字段}, ir_source_hash, predicted:{…}, actual:{…}, diffs:[…]}`。

**设计要点**:

- **对账字段 ↔ Piece 字段一一对应**:mana 消耗↔cost/减费、脸伤↔segments×(sp)、抽牌数↔engine/draw_n、回费↔mana_gain——每条分歧天然指向要改的那条规则;
- **IO 边界守 ARCHITECTURE 纪律**:diff 是 analysis 侧纯函数(只读 store);JSONL 写盘归 watcher 侧 AuditExporter(与 TrainingExporter/SnapshotService 同型);
- **离线复用**:同一 diff 函数可跑语料回放(import 切片时批量对账历史对局);与 simcal 三层校准互补——simcal 答"差多少",对账日志答"哪张卡哪条规则差";
- **消费入口**:v1 只出 JSONL + 轻量 CLI 汇总(top 分歧卡/规则族计数);不做自动改规则;
- **零干扰**:对账/落盘失败不影响 live 主链(记 WARNING,不阻断建议输出)。

## 6. 铁律与兼容契约

1. 公共契约不变:`Plan`、`RolloutResult` 既有字段、`sim_mulligan`/`combo_outputs`/simcal 接口全不动;
2. live 斩杀线输出零变化(钉子测试背书);
3. planner 依赖铁律:不 import store/watcher(除 `hsbot.consts`);SimSnapshot 纯数据可哈希;
4. live 进程零依赖 trainer(沿用 v3.1 spec);
5. 对账通道零干扰(§5);recon diff 零 IO。

## 7. 成功标准

- 既有测试(test_planner/test_rollout/test_lethal_plan/test_trainer)断言不改全绿;
- **纯重构可证**:同 seed 下 simcal 三层校准逐点完全一致(不是"不退化",是逐点相同);
- memo 坍缩:现有 10 异牌用例对照,状态数不增;
- live 斩杀线钉子测试零变化;
- 对账通道自有 fixture 测试(构造已知分歧,断言日志字段完整)。

## 8. 测试矩阵(增补)

| 层 | 用例 |
|---|---|
| advance_turn | dealt_total 累计与 face 清零;T1 不抽/T≥2 抽 1;mana=min(10,turn);空库自然抽→drawn+1 |
| memo_key | 与现行 dfs._key 逐状态等价(同推演树枚举对照) |
| rollout | RolloutResult 公共契约回归(launch_turn/hand_sizes/engines 逐点等旧实现) |
| live | 斩杀线钉子(输出零变化);assemble_snapshot 投影单测 |
| recon | 已知分歧 fixture(伤害/回费/抽牌三类)→ JSONL 字段断言;一致时零输出 |

## 9. 明确不做

- 不改 effects 语法表/Piece 语义(对账日志是发现问题的通道,修规则是后续独立迭代);
- 不做自动改规则 / 自动调参;
- 不对齐 store 快照 schema(实体/标签口径,已否决);
- 不做对手行为建模(敌方只进数值:enemy_total);
- rollout v1 简化口径(换牌洗回统一抽牌流、忽略手牌上限/疲劳)不在本次范围;
- 不动 simcal 接口与三层校准口径。

## 10. 实施切片建议(供 writing-plans 展开)

1. simstate → SimSnapshot + advance_turn + memo_key + 单测(纯增,无消费方变化);
2. dfs 改投影与签名(钉子测试背书);
3. rollout 迁移(RolloutResult 变派生)+ test_rollout 回归;
4. analysis.assemble_snapshot 抽取 + lethal_plan 薄化(live 钉子);
5. recon.py diff 纯函数 + watcher AuditExporter + JSONL 落盘;
6. CLI 汇总命令 + ARCHITECTURE.md 两通道总纲增补。
