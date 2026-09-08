# GameStore 重构设计 —— 库驱动的每局唯一状态权威

> 状态:设计稿 v3(2026-09-07;v2 改库驱动不造轮子,v3 补事件收录范围 + demo 定位推倒重来、去兜底)。
> 上游文档:[DESIGN.md](../../DESIGN.md)、[M1_MONITOR.md](../../M1_MONITOR.md)。
> 本设计**推翻** M1 的"不维护增量状态"决策(见 §1.2),实施时需同步修订两份上游文档。

## 1. 背景与决策

### 1.1 动机(问题诊断)

**直接动因**:为后续算法层(M2 决策/M3 规划)供状态 —— 测试中已实际出现大量数据冲突
(快照导出与链路影子字典两套平行状态互相打架,近 5 个 fix 均属此类)。
目标方式:hslog 提供游戏事件(packet)→ 注入状态机 → 一局游戏数值统一维护。

M1 现状:对局状态散落三层 —— hslog 解析器内部的实体树(权威)、每次全量重建的 `GameState` 快照视图、
以及 Watcher 上约 12 个散装增量字典(`shadow`/`_mana`/`mulligan`/`_discover`/`_choice_pid`/`_player_turn`/
`_ent2pid`/`_hint_cid`/`_hint_ctrl`/`_pending_play`/`_mulligan_emitted`/`_discover_emitted`)。
packet 处理器(如 `_on_tag`,85 行)就地 mutate `self.xxx`,状态所有权不清、到处传参。

### 1.2 本次决策(推翻旧决策)

**旧决策**(watcher.py 模块注释、M1_MONITOR.md §3):"不维护增量状态:快照每次全量导出实体树;
shadow 表只是链路显示用的元数据,不是权威状态。"

**新决策**:

1. **库驱动的增量状态机**:`GameStore` 每局一个实例,packet 平铺游标每出一包就喂一包,
   实体/标签/区域状态由 **hslog `EntityTreeExporter` 维护在 `hearthstone.entities`(Game/Player/Card)
   模型上**(经 adapter 的容错子类,见 §3.2)。hslog 只做行→packet 解析与状态应用;
   全量重导出的 `export_game_state` 退出运行时路径。
2. **不造轮子铁则**:上游库已定义/已维护的定义与数据格式直接用,项目内只留一份 —— 删除
   `gamestate.py` 的自研平行模型(Entity/GameState/PlayerInfo)。自研仅限库没有的部分(§2.2)。
3. 事件粒度为**原始 packet 直投**:链路事件(抽牌/出牌/回合开始/血甲水晶变化…)由状态迁移衍生发出,
   不再是独立追踪的平行链路。
4. 查询 API **上移 store**;导出字典 = `store.to_dict()`(JSONL 字段级兼容)。
5. "单例"的形态:**每局一个实例,组件共享引用**(CREATE_GAME 时创建,局终归档)。
   不用模块级全局 —— 回放模式连续多局、测试隔离都需要多实例并存。
6. **事件收录补全**:回合开始/结束生效的卡牌效果(现链路对 `BlockType.TRIGGER` 完全无事件,
   `_on_block` 只处理 ATTACK)、疲劳、死亡清理等漏收内容,新事件层必须收录(§3.4)。
7. **demo 定位**:M1 代码是 demo,授权推倒重来 —— watcher 整体重写而非增量修改,
   不做兼容补丁、不设计兜底路径。

### 1.3 被否决的备选

- **领域事件层**(packet→领域事件→状态仓):多一层翻译,而翻译层恰是现在 bug 最密集的一层;
  领域事件改为状态迁移的衍生品,不再是状态的输入。
- **只收拢不重建**(Watcher 散装字典合并但保留快照导出):不解决双状态机漂移,Watcher 仍是 God class。
- **永久双轨**(每快照点全量导出回同步 store):8efa97e 的做法永久化,与"唯一权威"目标相悖。
- **活视图**(store.gs 直接可查,消费方零改动):渲染层改动小,但查询 API 留在 DTO 上职责不清,已否决。
- **自研 packet→实体应用**(v1 方案,store 自己写 _on_tag 全套应用逻辑):重复实现
  `EntityTreeExporter.handle_*`/`Card.reveal/hide/change/tag_change` 已完善的轮子,且旧 watcher
  的自伤案例(`_is_hero(sh)` 门槛丢血量标签)证明自研应用逻辑易漏 —— 弃。

## 2. 架构

### 2.1 数据流

```
Power.log ──tail──> hslog LogParser(行→packet,平铺游标保留)
                          │
                          ▼  逐包 export_packet
   StoreExporter(adapter 内,_TolerantExporter 子类)
       ├─ super().handle_*()        ← 库维护 hearthstone.entities 状态(轮子,不自研)
       └─ 挂钩回调 on_applied(packet) ← GameStore 衍生链路事件
                          │
                          ▼
                    ┌─────────────┐
                    │  GameStore  │  每局一个实例,唯一状态权威
                    └─────────────┘
                      │       │        │
                      ▼       ▼        ▼
                 链路事件   查询 API   to_dict()
              (状态迁移     (hand/board/  → JSONL 持久化/回测
               衍生发出)    mana/hero/…)   字段级兼容
                 │
                 ▼
          订阅者: render→hub(控制台/会话文件/悬浮窗);M2 bot(未来)
```

数据流方向单一:日志 → packet → store →(事件/查询/导出)→ 消费方。消费方永不回写状态。

### 2.2 轮子清单(复用边界)

| 层 | 谁维护 | 说明 |
|---|---|---|
| 行→packet 解析 | hslog `LogParser` | 不碰;脏行逐行跳过沿用 adapter 现状 |
| packet→实体状态应用 | hslog `EntityTreeExporter`(经 `_TolerantExporter` 容错子类) | `export_packet(p)` 公开可逐包驱动;reveal/hide/change/tag_change 全套库实现 |
| 实体/标签/区域模型 | `hearthstone.entities`(Game/Player/Card) | 升格为项目共享状态模型;`gamestate.py` 自研平行模型删除 |
| 枚举(GameTag/Zone/CardType/BlockType) | `hearthstone.enums` | 已在用,继续 |
| 留牌/发现/PLAY延迟/标题显示态 | 无库,**自研**(store 有类型字段) | 库不追踪的选择与显示层状态 |
| 行级括号兜底(`_hint_cid`/`_hint_ctrl`) | **自研**(库的空白) | hslog 会丢弃括号内 cardId/player 信息,这是补库丢损,不是平行解析器 |
| 卡名(中文) | `CardDB`(自研,无现成库) | 保留 |
| 友方判定 | `FriendlyPlayerExporter` + 战网名优先 | 沿用 adapter 现状 |

### 2.3 铁律修订(DESIGN.md §3)

原:"adapter 是全项目唯一允许 import hslog/hearthstone 实体类的模块"。
修订为两条:
- **hslog 解析/导出类**(LogParser、packets、exporter 及其子类)仍限 adapter —— `StoreExporter`
  定义在 adapter,通过对构造函数传入的挂钩回调与 store 通信,store 不 import hslog;
- **`hearthstone.entities` 与 `hearthstone.enums`** 为共享状态模型,store 可直接使用
  (enums 本就全项目在用;entities 的实例经 store 查询 API 流出,渲染层鸭子类型访问属性,
  无需 import)。

## 3. GameStore 规格

### 3.1 状态字段

**库持有**(store 经 `store.game` 只读访问,由 StoreExporter 维护):
实体表、game 标签、玩家(Player.player_id 即 PlayerKey 语义,CONTROLLER 标签值 = PLAYER_ID 命名空间不变)。

**store 自有**(吸收 Watcher 散装字典,有类型字段):
- `friendly_key: PlayerKey | None`、`meta`、`lines_consumed`(游标与事件去重)
- 留牌:`mulligan: dict[PlayerKey, MulliganState]`(offered/kept/已发标)
- 发现/选择:`_choice_pid`、`_discover`、发标去重集合
- PLAY 块延迟:`_pending_play`(子树结束再发出牌事件)
- 行级兜底线索:`_hint_cid`/`_hint_ctrl`(watcher 的括号扫描器喂入)
- 回合/标题显示态:`_player_turn`、`_first_player`、`_title_done`

watcher 的 `shadow` 表整体删除 —— 其全部信息(cid/ctrl/cost/zone/ctype)都在库的实体标签里;
`_mana` 同理(直查玩家实体标签)。

### 3.2 变更入口(仅此四个)

```python
store.apply(packet)          # → adapter.StoreExporter.export_packet(packet)
                              #   库 handle_* 维护实体状态 → 挂钩回调 → store 衍生链路事件
store.apply_block_end(block)  # 游标在块子树走完时调用(PLAY 延迟发/attack 归属需要块边界;
                              #   块结构只有游标知道,必须显式通知)
store.hint_cid(eid, cid)     # 行级括号兜底(watcher 的行扫描器调用)
store.hint_ctrl(eid, pid)
```

> 实现细化(2026-09-08 计划采纳):`apply_block_end(block)` 落地为
> `apply(p, depth)`(游标给深度, 遇不深于挂起 PLAY 的包先冲刷)+ `settle()`(批尾冲刷
> ended 块);另补 `hint_draw`(行级抽牌直通)与 `note_friendly`(友方探测回填)。

StoreExporter 挂钩形态:子类覆写各 `handle_*`,先 `super()`(库维护状态),再调
`self.hooks[tag_change](packet)` 类回调;容错行为沿用 `_TolerantExporter`(脏包跳过不炸)。

### 3.3 查询 API(自 GameState 上移,签名不变)

`hand/board/deck_entities/deck_count/graveyard_count/secret_count/mana_fields/mana_now/
mana_next_turn/hero/hero_hp/hero_armor/hero_total_hp/current_key/is_my_turn/turn/
friendly_turn_number/name/playstate/opponent_key/get/player_keys`。

实现直读库实体(`card.controller.player_id` 取 PlayerKey;hp_total 等计算属性在 store 查询里就地算)。
契约:查询返回**活引用**,消费方只读、不得 mutate(写入代码注释明示)。

### 3.4 事件订阅与收录范围

```python
store.subscribe(callback)   # callback(evt: dict),同步调用
```

- 事件 dict 形状与今日 `_chain_event` 载荷一致(kind/actor/card_id/…),渲染层零适配
- **收录范围**(含补全,当前 `_on_block` 只处理 ATTACK,TRIGGER 类整体漏收):
  - 既有:play(PLAY 块延迟发)/ attack / draw(三路径去重)/ gain / discover / mulligan /
    mana 变化 / hp·armor 变化 / shuffle / 结束回合 / 回合开始标题
  - **补全**:`trigger` —— 回合开始/结束等生效的卡牌效果(`BlockType.TRIGGER` 块,
    事件带来源实体 card_id 与效果概要)、`fatigue` 疲劳、`death` 阵亡清理
- **全量收录原则(2026-09-08 补)**:每个 packet 类型必须有处置——衍生为链路事件,
  或记录为 `raw` 事件(packet 类型 + 完整载荷,入 `store.unhandled` 并随 JSONL 的
  `unhandled` 字段落盘,**不渲染**)。不允许静默丢弃;未解释类型以 raw 可见,
  后续逐个升级为正式事件("可以暂时不处理,但不能没记录")。
- 同步分发,apply 调用栈内触发;订阅者**不得重入 apply**(写入契约注释)
- 单线程模型:watcher 轮询线程独占 store;悬浮窗线程隔离由既有 OutputHub 负责,不变

### 3.5 导出

- `store.to_dict(reason) -> dict`:JSONL 载荷,产出结构 = 今日 `GameState.to_dict()` 的输出,
  **字段级兼容**(data/sessions 既有数据可继续消费)。
- 备忘录:M2/M3 需要不可变时刻态时 `copy.deepcopy(store.game)`(库实体为纯对象,可整体拷);
  v1 的 `GameState` DTO 删除,不再有第二份状态定义。
- 回放能力:packet 树前缀稳定,任意历史状态 = 重放前缀(既有性质,不变)。

### 3.6 生命周期

`Watcher._new_game()` → `GameStore()`(内含新 StoreExporter);局终(PLAYSTATE→WON/LOST/TIED)→
写终局快照后丢弃引用。跨局残留防泄漏沿用现有 FSM 切局逻辑。

## 4. 与 M3 规划层的边界(模拟不用单例)

可变 GameStore **绝不直接参与模拟**。DFS/蒙特卡洛需要分支、回溯、并行,共享可变状态会造成
模拟污染实况、回溯不可撤销、并发 MC 数据竞争。

```
GameStore(实况真相,可变) --投影(只读一次性)--> PlannerState(值对象:可哈希/可 deepcopy)
                                              ├─ 分支 copy → 模拟 → 弃
                                              └─ N 路 MC 并行,不接触 store
```

- 接触面唯一:决策时刻从 store 查询/导出投影出初始 PlannerState(DESIGN.md §3.3 原则)
- PlannerState 维持"纯函数核心":可哈希(记忆化)、可拷贝(分支)、无共享(并发)
- 单例化的只是**实况状态的所有权**,模拟层自成闭环

## 5. 各文件去向

| 文件 | 变化 |
|---|---|
| `hsbot/store.py`(新增,~300 行) | GameStore:挂钩回调接收库状态迁移 → 衍生链路事件(含 §3.4 补全项);查询 API;to_dict;留牌/发现/PLAY延迟/标题字段。吸收 watcher ~400 行逻辑 |
| `hsbot/watcher.py`(732→~280 行,**整体重写**) | 只剩 tail/游标/FSM/行级括号扫描(喂 hint)/输出管线/Decks.log。不再持有对局状态 |
| `hsbot/gamestate.py` | **删除**(自研平行模型退役);`PlayerKey` 等类型别名迁入 store.py |
| `hsbot/render.py` | 查询调用点 GameState→GameStore(约 23 处,机械替换);输出格式不变 |
| `hsbot/knowledge.py` | `rebuild(gs)`→`rebuild(store)`,全量重建哲学不变 |
| `hsbot/adapter.py` | 保留 new_parser/feed_line/resolve_friendly;新增 `StoreExporter`(= _TolerantExporter + 挂钩);`export_game_state` 删除 |
| `hsbot/main.py` | 基本不变(watcher 内部持有 store) |
| `hsbot/corpus.py` / `overlay.py` / `persist.py` | 不变 |
| `tests/store_test.py`(新增) | packet 序列 fixture → 断言状态与事件 |

## 6. 迁移与验收(demo 重写,无兼容补丁)

1. **基线语义对照**:当前 HEAD 跑 25 局语料留存输出(链路行+快照块+JSONL);新架构同语料回放,
   **既有事件逐条核对仍然正确**(不要求字节级零差异 —— 本次新增事件,输出本来就会增行)
2. **新增事件核对**:trigger/fatigue/death 在语料中抽查来源实体与效果内容正确
   裁定(2026-09-08 验收): 英雄首次掉血收录与 [T1] 链路回合戳为新行为(旧代码缺陷), 语义 diff 按此豁免。
3. **单测**:packet 序列 → 状态/事件断言;脏形态(括号引用、PlayerReference、HideEntity 重隐、
   抽牌三路径、PLAY 块延迟、回合开始触发链)全部做成 fixture —— 近 5 个 fix 的全部案发现场
4. **真实验收**:重跑 M1_MONITOR §6(真实对局人工核对血/费/手牌/牌库数/过载)
5. 全部通过后,同步修订 DESIGN.md(§3 铁律、§3.1 数据结构、§4.2 主循环、§5 模式表)与
   M1_MONITOR.md(§3 设计要点、§2 事件链路表补 TRIGGER 行)—— 文档不留谎言

## 7. 风险(不设兜底补丁,按 demo 定位直面)

| # | 风险 | 应对 |
|---|---|---|
| R1 | "寻求指引类 hslog 不产包"的血量基数(8efa97e)在新链路丢信息 | 根因假设:旧 `_on_tag` 的 `_is_hero(sh)` 门槛在 shadow 未登记时丢标签,库的 `handle_tag_change` 无条件应用,预期自愈 —— 语料对照验证;若确认日志本身无信息,记为已知缺口;已验证:库无条件应用 + 批尾快照使终局行双方 PLAYSTATE 完整(09d05c1)。 |
| R2 | 重写无运行时对账,中间态错误难发现 | §6.3 单测 fixture 覆盖案发现场 + §6.1 语料对照覆盖全部链路行;链路行即中间态的渲染投影 |
| R3 | 消费方误 mutate 查询返回的活引用 | §3.3 契约注释;单测不对抗此条(约定优于防御性拷贝,YAGNI) |
| R4 | 事件订阅者重入 apply 导致状态错乱 | §3.4 契约注释;调试断言(apply 重入即 raise,dev 模式) |
| R5 | 库 exporter 对脏包/缺实体的容错面与旧路径不同 | `_TolerantExporter` 容错沿用;语料对照全量兜住 |

## 8. 范围外

- M2 bot 决策层(只保证 store 可被其查询/订阅)
- knowledge 层吸收(仍全量重建,仅换数据源)
- corpus 语料格式、悬浮窗、持久化格式
- DESIGN.md 之外的任何新功能
