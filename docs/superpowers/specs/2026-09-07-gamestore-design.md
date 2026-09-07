# GameStore 重构设计 —— packet 直投的每局唯一状态权威

> 状态:设计稿 v1(2026-09-07)。
> 上游文档:[DESIGN.md](../../DESIGN.md)、[M1_MONITOR.md](../../M1_MONITOR.md)。
> 本设计**推翻** M1 的"不维护增量状态"决策(见 §1.2),实施时需同步修订两份上游文档。

## 1. 背景与决策

### 1.1 动机(问题诊断)

**直接动因**:为后续算法层(M2 决策/M3 规划)供状态 —— 测试中已实际出现大量数据冲突
(快照导出与链路影子字典两套平行状态互相打架,近 5 个 fix 均属此类)。
目标方式:hslog 提供游戏事件(packet)→ 注入状态机 → 一局游戏数值统一维护。

M1 现状:对局状态散落三层 —— hslog 解析器内部的实体树(权威)、每次全量重建的 `GameState` 快照视图、
以及 Watcher 上约 12 个散装增量字典(`shadow`/`_mana`/`_mulligan`/`_discover`/`_choice_pid`/`_player_turn`/
`_ent2pid`/`_hint_cid`/`_hint_ctrl`/`_pending_play`/`_mulligan_emitted`/`_discover_emitted`)。
packet 处理器(如 `_on_tag`,85 行)就地 mutate `self.xxx`,状态所有权不清、到处传参。

### 1.2 本次决策(推翻旧决策)

**旧决策**(watcher.py 模块注释、M1_MONITOR.md §3):"不维护增量状态:快照每次全量导出实体树;
shadow 表只是链路显示用的元数据,不是权威状态。"

**新决策**:

1. 自建状态机:新模块 `store.py` 的 `GameStore` 直接消费 hslog 原始 packet
   (TagChange/FullEntity/ShowEntity/HideEntity/Choices/SendChoices/ChosenEntities/ShuffleDeck/Block 边界),
   内部维护全部对局状态(双方玩家、水晶、手牌、牌库、墓地、场面、英雄、秘密)。
   hslog 仍负责行→packet 解析,但 `EntityTreeExporter` 全量导出退出运行时路径。
2. 事件粒度为**原始 packet 直投**:链路事件(抽牌/出牌/回合开始/血甲水晶变化…)由状态迁移衍生发出,
   不再是独立追踪的平行链路。
3. 查询 API **上移 store**:`GameState` 退化为纯导出 DTO。
4. "单例"的形态:**每局一个实例,组件共享引用**(CREATE_GAME 时创建,局终归档)。
   不用模块级全局 —— 回放模式连续多局、测试隔离都需要多实例并存。

### 1.3 被否决的备选

- **领域事件层**(packet→领域事件→状态仓):多一层翻译,而翻译层恰是现在 bug 最密集的一层;
  领域事件改为状态迁移的衍生品,不再是状态的输入。
- **只收拢不重建**(Watcher 散装字典合并但保留快照导出):不解决双状态机漂移,Watcher 仍是 God class。
- **永久双轨**(每快照点全量导出回同步 store):8efa97e 的做法永久化,与"唯一权威"目标相悖。
- **活视图**(store.gs 直接可查,消费方零改动):渲染层改动小,但查询 API 留在 DTO 上职责不清,已否决。

## 2. 架构

```
Power.log ──tail──> hslog LogParser(行→packet,平铺游标保留)
                          │
                          ▼  逐包 apply
                    ┌─────────────┐
                    │  GameStore  │  每局一个实例,唯一可变状态权威
                    └─────────────┘
                      │       │        │
                      ▼       ▼        ▼
                 链路事件   查询 API   snapshot()
              (状态迁移     (hand/board/  → GameState 导出 DTO
               衍生发出)    mana/hero/…)  → JSONL 持久化/回测
                 │
                 ▼
          订阅者: render→hub(控制台/会话文件/悬浮窗);M2 bot(未来)
```

数据流方向单一:日志 → packet → store →(事件/查询/快照)→ 消费方。消费方永不回写状态。

## 3. GameStore 规格

### 3.1 状态字段(吸收 Watcher 全部散装字典,改为有类型字段)

- `entities: dict[int, Entity]`、`game_tags: dict[GameTag, int]`、
  `players: dict[PlayerKey, PlayerInfo]`、`friendly_key: PlayerKey | None`、`meta`
  (即现 gamestate.py 的同构结构,由 store 就地维护)
- 留牌:`mulligan: dict[PlayerKey, MulliganState]`(offered/kept/已发标)
- 发现/选择:`_choice_pid`、`_discover`、发标去重集合
- PLAY 块延迟:`_pending_play`(子树结束再发出牌事件)
- 行级兜底线索:`_hint_cid`/`_hint_ctrl`(由 watcher 的括号扫描器喂入)
- 回合/标题显示态:`_player_turn`、`_first_player`、`_title_done`
- `lines_consumed`(游标与事件去重)

`Entity`/`PlayerInfo` 数据结构沿用 gamestate.py 现有定义,`PlayerKey = PLAYER_ID` 命名空间铁律不变
(adapter 负责翻译,CONTROLLER 标签值 = PLAYER_ID)。

### 3.2 变更入口(仅此三个)

```python
store.apply(packet)        # packet 分发到内部处理器(_on_tag/_on_full_entity/…,自 watcher 迁移)
store.hint_cid(eid, cid)   # 行级括号兜底(watcher 的行扫描器调用)
store.hint_ctrl(eid, pid)
```

外部只允许这三个入口改状态。处理器在状态迁移点调用 `_emit_event(evt)` 衍生链路事件。

### 3.3 查询 API(自 GameState 上移,签名不变)

`hand/board/deck_entities/deck_count/graveyard_count/secret_count/mana_fields/mana_now/
mana_next_turn/hero/hero_hp/hero_armor/hero_total_hp/current_key/is_my_turn/turn/
friendly_turn_number/name/playstate/opponent_key/get/player_keys`。

契约:查询返回的是**活引用**,消费方只读、不得 mutate(渲染层现状即如此,写入代码注释明示)。

### 3.4 事件订阅

```python
store.subscribe(callback)   # callback(evt: dict),同步调用
```

- 事件 dict 形状与今日 `_chain_event` 载荷一致(kind/actor/card_id/…),渲染层零适配
- 同步分发,apply 调用栈内触发;订阅者**不得重入 apply**(写入契约注释)
- 单线程模型:watcher 轮询线程独占 store;悬浮窗线程隔离由既有 OutputHub 负责,不变

### 3.5 快照导出

- `store.snapshot(reason) -> GameState`:deepcopy 出不可变 DTO(备忘录,DESIGN.md §5)。
- `store.to_dict(reason) -> dict`:JSONL 载荷,产出结构 = 今日 `GameState.to_dict()` 的输出,
  **字段级兼容**(data/sessions 既有数据可继续消费)。to_dict 依赖查询,故与其实现一起上移 store。
- `GameState` 退化为纯数据字段(entities/game_tags/players/friendly_key/meta/lines_consumed),
  查询方法与 to_dict 全部删除 —— DTO 就是哑数据,组装逻辑唯一宿主是 store。

### 3.6 生命周期

`Watcher._new_game()` → `GameStore()`;局终(PLAYSTATE→WON/LOST/TIED)→ 归档(写终局快照后丢弃引用)。
跨局残留防泄漏沿用现有 FSM 切局逻辑。

## 4. 与 M3 规划层的边界(模拟不用单例)

可变 GameStore **绝不直接参与模拟**。DFS/蒙特卡洛需要分支、回溯、并行,共享可变状态会造成
模拟污染实况、回溯不可撤销、并发 MC 数据竞争。

```
GameStore(实况真相,可变) --投影(只读一次性)--> PlannerState(值对象:可哈希/可 deepcopy)
                                              ├─ 分支 copy → 模拟 → 弃
                                              └─ N 路 MC 并行,不接触 store
```

- 接触面唯一:决策时刻从 store 查询/snapshot 投影出初始 PlannerState(DESIGN.md §3.3 原则)
- PlannerState 维持"纯函数核心":可哈希(记忆化)、可拷贝(分支)、无共享(并发)
- 单例化的只是**实况状态的所有权**,模拟层自成闭环

## 5. 各文件去向

| 文件 | 变化 |
|---|---|
| `hsbot/store.py`(新增,~400 行) | GameStore:packet 应用 + 查询 + 事件 + 快照,吸收 watcher ~450 行状态逻辑 |
| `hsbot/watcher.py`(732→~300 行) | 只剩 tail/游标/FSM/行级括号扫描(喂 hint)/输出管线/Decks.log。不再持有对局状态 |
| `hsbot/gamestate.py`(231→~120 行) | `GameState` 纯数据 DTO(备忘录);`Entity`/`PlayerInfo` 沿用;查询方法与 to_dict 上移 store |
| `hsbot/render.py` | 查询调用点 GameState→GameStore(约 23 处,机械替换);输出格式不变 |
| `hsbot/knowledge.py` | `rebuild(gs)`→`rebuild(store)`,全量重建哲学不变 |
| `hsbot/adapter.py` | 行→packet、friendly 探测保留;`export_game_state` 退出运行时(去留见 §7 风险 R1) |
| `hsbot/main.py` | 基本不变(watcher 内部持有 store) |
| `hsbot/corpus.py` / `overlay.py` / `persist.py` | 不变 |
| `tests/store_test.py`(新增) | packet 序列 fixture → 断言状态与事件 |

## 6. 迁移与验收(直接切换,无双轨)

1. **基线**:当前 HEAD 跑 25 局语料回放,留存输出(链路行 + 快照块 + JSONL)为基线文件
2. 实现新架构后同语料回放,与基线**逐行 diff,要求零差异**(输出格式本次不变;
   diff 前剥离会话路径等环境相关字段,只比对语义内容)
3. **单测**:packet 序列 → 状态/事件断言;脏形态(括号引用、PlayerReference、HideEntity 重隐、
   抽牌三路径、PLAY 块延迟)全部做成 fixture —— 这些是近 5 个 fix 的全部案发现场
4. **真实验收**:重跑 M1_MONITOR §6(真实对局人工核对血/费/手牌/牌库数/过载)
5. 全部通过后,同步修订 DESIGN.md(§3.1/§4.2/§5)与 M1_MONITOR.md(§3 设计要点)——文档不留谎言

## 7. 风险与应急预案

| # | 风险 | 应对 |
|---|---|---|
| R1 | "寻求指引类 hslog 不产包"的血量基数(8efa97e)在新状态机里丢信息 | 语料 diff 若命中:先排查被旧 watcher 忽略的 packet 形态(store 修);若确认无包可吃,在 adapter 保留 `export_game_state` 仅供 `store.realign_from_export(parser)` 窄兜底(仅英雄血/甲/伤害三标签,同 8efa97e 范围),默认路径不走 |
| R2 | 直接切换无运行时对账,快照点之外的中间态错误难发现 | §6.3 单测 fixture 覆盖案发现场 + §6.2 输出 diff 覆盖全部链路行;链路行即中间态的渲染投影 |
| R3 | 消费方误 mutate 查询返回的活引用 | §3.3 契约注释;单测不对抗此条(约定优于防御性拷贝,YAGNI) |
| R4 | 事件订阅者重入 apply 导致状态错乱 | §3.4 契约注释;调试断言(apply 重入即 raise,dev 模式) |

## 8. 范围外

- M2 bot 决策层(只保证 store 可被其查询/订阅)
- knowledge 层吸收(仍全量重建,仅换数据源)
- corpus 语料格式、悬浮窗、持久化格式
- DESIGN.md 之外的任何新功能
