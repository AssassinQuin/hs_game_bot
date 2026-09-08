# M1 详细设计 —— Power.log 监控与状态维护层

> 范围：监控 Power.log + 按用户所选卡组维护台账 + 两类输出（**回合结束完整快照**、**回合内出牌链路**）。
> 不包含：任何算法/概率/建议。本层唯一美德是**不会错**。
> 上游文档：[DESIGN.md](DESIGN.md)（本文是其 §10 中 M1 的施工图）。

---

## 1. 输入与配置

配置三来源（优先级从高到低）：**命令行 > `config.yaml`（仓库根目录）> 内置默认**。`logs_dir` 留空时按平台自动探测候选目录（Windows 常见安装位 / macOS Library / Linux Proton、Wine 前缀）。

```yaml
# config.yaml
deck_name: 奇迹德
battletag: 湫然#51704      # 友方判定首选
logs_dir: E:\battle\Hearthstone\Logs
# deck_code: AAEBA...     # 可选, 直接指定卡组代码, 优先于 Decks.log 匹配
```

命令行同名参数（`--deck / --deck-code / --logs-dir / --battletag / --replay / --config`）可临时覆盖。

| 输入 | 用途 | 方式 |
|---|---|---|
| `<logs_dir>\<最新会话>\Power.log` | 全部游戏事件 | 轮询大小，增量读 |
| `<logs_dir>\<最新会话>\Decks.log` | 每局实际使用的卡组 | 每次开局前客户端写入 `Finding Game With Deck: ### 卡组名` + deck code |
| `data/decks/decklist.json` | 用户所选卡组的 30 张多重集 | 首次从 Decks.log 解码生成并缓存 |
| `data/cache/cards.zh.json` | card_id → 中文名/费用 | hearthstonejson，下载一次 |

**会话发现（实测修正，已实现）**：最新会话目录按**目录名排序**（`Hearthstone_YYYY_MM_DD_HH_MM_SS` 字典序=时间序），不用目录 mtime——NTFS 目录 mtime 不随 Power.log 追加更新，按 mtime 选会选错。**bot 先于游戏启动**时，直接挂上最新会话目录等待 Power.log 出现，文件一创建即接管；每 10 秒检查一次新会话目录并自动切换。

> **友方判定实测结论（2026-09-06，已实现）**：hslog 的 `FriendlyPlayerExporter`（"第一条 SHOW_ENTITY 属于友方"启发式）在同一会话的不同对局里可能给出不同答案——当对手手牌被提前揭示（镜像对局/衍生揭示）时即翻转。因此实现中**战网名匹配是首选**（跨局稳定的硬事实），exporter 仅作未配置时的兜底。

**卡组选择如何生效**：程序常驻监听 Decks.log，每次出现 `Finding Game With Deck: ### 奇迹德` 即认为"接下来的一局是所选卡组"；若下一局实际是别的卡组（如你切了萨满），进入**通用模式**——只输出快照，不输出组件台账，并在快照头部标注"非所选卡组"。

---

## 2. 事件链路（日志行 → 类型化事件）

M1 需要识别的完整日志模式清单（hslog 已把它们解析成 packet，表里同时给原始行特征与 hslog packet 的对应关系，两列等价，实现取其一）：

| 事件 | Power.log 行特征 | hslog packet | 用途 |
|---|---|---|---|
| 开局 | `GameState.DebugPrintPower() - CREATE_GAME` | `packets.CreateGame` | 切局、重置游标、新 jsonl |
| 玩家注册 | `PlayerName=...` 行 | `packets.Player` | 名字 ↔ PLAYER_KEY 绑定 |
| 出牌 | `BLOCK_START BlockType=PLAY` … `BLOCK_END` | `Block(type=BlockType.PLAY)` | **链路输出** |
| 抽牌 | `BLOCK_START BlockType=DRAW` | `Block(type=DRAW)` | 链路输出 + 台账 seen |
| 攻击 | `BLOCK_START BlockType=ATTACK` | `Block(type=ATTACK)` | 链路输出 |
| 回合切换 | `TAG_CHANGE Entity=<玩家> tag=CURRENT_PLAYER value=1` | `TagChange(tag=GameTag.CURRENT_PLAYER)` | **快照触发点** |
| 回合计数 | `TAG_CHANGE Entity=GameEntity tag=TURN value=N` | `TagChange(tag=TURN)` | 回合号 |
| 过载/费用 | `tag=OVERLOAD_OWED / RESOURCES_USED / TEMP_RESOURCES` | 同名 TagChange | 快照里的费用字段 |
| 手牌明示 | `FULL_ENTITY - ... CardID=xxx`（zone=HAND，己方） | `FullEntity` | 台账 seen + 链路"抽到" |
| 衍生牌 | `FULL_ENTITY` 且实体随后带 `tag=CREATOR` | `TagChange(tag=CREATOR)` | 台账**不**计 seen |
| 手牌减费 | `TAG_CHANGE Entity=<手牌实体> tag=COST value=N`（由附魔实体挂出，`tag=ATTACHED`） | `TagChange(tag=COST)` | 链路行 + 快照实付费用 |
| 探底/发现揭示 | `FULL_ENTITY` zone=SETASIDE（探底 3 张 / 发现 3 张），未选者随后 `TAG_CHANGE zone=DECK` 且带 `ZONE_POSITION` | `FullEntity` + `TagChange(ZONE/ZONE_POSITION)` | **牌库序已知段**（见 §3.1） |
| 卡牌位置 | 各区域实体的 `tag=ZONE_POSITION` | `TagChange(tag=ZONE_POSITION)` | 手牌顺序①②③ / 牌库位置 |
| 局终 | `tag=PLAYSTATE value=WON/LOST/TIED` | `TagChange(tag=PLAYSTATE)` | 收尾输出 |
| 回合开始生效的卡牌效果 | `BLOCK_START BlockType=TRIGGER Entity=<卡>` | Block(TRIGGER) | 链路行"触发 <卡名>" |
| 疲劳 | `BLOCK_START BlockType=FATIGUE Entity=<玩家>` | Block(FATIGUE) | 链路行"疲劳" |
| 随从死亡 | `TAG_CHANGE tag=ZONE value=GRAVEYARD`(自 PLAY) | TagChange(ZONE) | 链路行"阵亡 <卡名>" |

触发规则（谁触发快照）：

```text
CURRENT_PLAYER 切到对手   → "我的回合结束" → 完整快照 + 本回合链路汇总
CURRENT_PLAYER 切到我     → 新回合开始（M1 只打印一行标题，不算快照）
PLAYSTATE → WON/LOST/TIED → 终局快照（含胜负）
CREATE_GAME               → 重置一切，旧局 jsonl 关闭
COST / ZONE_POSITION 变更 → 不触发快照，只刷新链路行（显示层信息）
```

> 为什么选 CURRENT_PLAYER 而不是 STEP=MAIN_READY：CURRENT_PLAYER 直接指明行动方，不需要再反查 STEP 顺序，且切局后首次出现即意味着调度阶段结束。

---

## 3. 模块与骨架（与 DESIGN.md §5 五组件一一对应）

包名与最终项目结构统一为 `hsbot/`（完整目录树见 [PROJECT.md §1](PROJECT.md)），M1 阶段只创建其中这 6 个文件：

```text
hsbot/
├─ config.py       # 路径、卡组名、节流参数（纯数据）
├─ gamestate.py    # Entity / GameState 数据类（协议忠实）
├─ watcher.py      # 轮询 + 行读取 + FSM(IDLE/IN_GAME/GAME_END) + 事件生成
├─ adapter.py      # hslog packet树 → GameState（唯一允许 import hslog 的模块）
├─ knowledge.py    # decklist 加载 + seen 台账 + remaining 计算 + deck_order 牌库序视图（CREATOR 牌不入台账）
├─ render.py       # 两个渲染器：链路行 / 快照块
├─ persist.py      # sessions jsonl 追加
└─ main.py         # 装配：以上各件 + 轮询循环（支持 --replay 重放模式）
```

要点回顾（详情见 DESIGN.md）：

- **库驱动唯一状态权威**:packet 平铺游标逐包喂 `GameStore.apply()`,实体/标签/区域
  由 adapter.StoreExporter(hslog EntityTreeExporter 容错子类)维护在
  hearthstone.entities 上;shadow 表/`_mana` 等散装字典全部退役。
- **链路事件 = 状态迁移的衍生品**:apply 挂钩对比新旧标签衍生抽牌/出牌/血甲水晶/区域
  事件;PLAY 块延迟到子树结束再发;TRIGGER/疲劳/死亡为补全收录(§2 表)。
- 脏行：`read_line` 逐行 try/except；某局导出失败 → 整局放弃并提示，不跨局带病。

### 3.1 牌库序模型（探底 / 置底 / 牌位）

牌库不是无序多重集，而是一个**部分已知的序列**（顶→底）。本卡组里波涛形塑（置底）、水栖形态（探底）、未来可能加入的任何洗牌都会改写它。实现上**不维护历史，直接从当前快照重建**——这是全量重建哲学的红利：

```text
已知牌 = DECK 区实体中 card_id 非空者
        （被探底/发现展示过的实体，card_id 一经揭示永久保留，之后 zone 回 DECK 仍可读）
未知牌 = DECK 区其余实体（card_id=None）→ 只知数量，组成 = DeckKnowledge.remaining()
牌位   = 各实体的 ZONE_POSITION；牌库总数 N → 位置 1 = 顶牌（下次抽），位置 N = 底牌
派生   = 下一抽已知？ / 已知底部 k 张 / 未知中段（多重集）
```

三条来源对应的识别规则：

| 效果 | 日志表现 | 对序模型的影响 |
|---|---|---|
| 水栖形态（探底+可达费抽走） | 3 实体 SETASIDE 揭示 → 选中者 `ZONE→HAND` | 另 2 张回 DECK 底部，位置已知 |
| 水栖形态（探底但费不够） | 3 实体揭示 → 选中者回 `ZONE→DECK` 且 `ZONE_POSITION=1` | **下一抽变成已知牌**（高价值信息） |
| 波涛形塑（发现+置底） | 3 实体 SETASIDE 揭示 → 取 1 张，其余 2 张 `ZONE→DECK` 高位置 | 底部新增 2 张已知牌 |

洗牌兜底：本卡组无洗牌，但模型不写死——万一发生，洗牌后所有 DECK 实体的 ZONE_POSITION 会重排，快照重建出来的序**自动就是新序**，无需任何特殊代码（又一次全量重建的好处）。

---

## 4. 输出规范

### 4.1 回合内链路（流式，每有事件打一行）

```text
[T8·我] 打出 光子炮台 SC_753 (2费→实付0 [星灵-2])          剩余费用 7→7
[T8·我] 抽到 建造水晶塔 SC_755 [衍生·黑市拍卖师]            手牌 5→6
[T8·我] 打出 雷霆绽放 SCH_427 (0费, 过载+2)                剩余费用 7→9
[T8·我] 探底 揭示: [底2 米斯塔·维斯塔][底1 光子炮台][底3 生物计划]
         → 选 光子炮台 置顶 (当前费不够, 未抽) → 下一抽已知: 光子炮台
[T8·我] 波涛形塑 发现3选1: 取 月光射线, 置底 [活体根须][月火术]
[T8·我] 手牌减费: 月火术 1费→0费 [生命缚誓者的礼物·抉择]
[T8·我] 打出 月火术 CS2_008 (面板1费→实付0) → 敌方英雄      剩余费用 9→9
[T8·对] 打出 明澈圣契 GDB_137 (3费)
[T8·对] 攻击 奥尔多侍从 → 我的英雄
```

格式约定：`[回合·行动方] 动作 卡名 卡ID (费注明细) 上下文`。实付费用与剩余费用取自 packet 前后的 RESOURCES_USED 差值；手牌减费直接读实体 COST 标签（显示 `tags[COST]`，无该标签才回退卡表费用），**不自行叠加计算**，算不出就显示 `?`，不要猜。

### 4.2 回合结束完整快照（我的回合结束时打印一整块）

```text
════════ 完整快照 ════════ 回合T8(我的第4回合结束) │ 第3局 │ 排名·狂野
我  湫然#51704 [HERO_06bi 玛法里奥] 30血0甲 │ 牌库12 │ 本回合已用7费
    下一回合: 8费 (含过载-2 → 雷霆绽放×1)
    手牌6(位置序): ①光子炮台(2费) ②月火术(0费⬇) ③SC_755建造水晶塔(0费)
                   ④雷霆绽放(0费) ⑤月光射线(1→0费⬇) ⑥米斯塔·维斯塔(5费)
    场面: (无)   墓地: 9张   奥秘: 无
对手 notwind#5141 [HERO_04a 乌瑟尔] 24血0甲 │ 牌库9 │ 手牌4(不可见)
    场面: (无)
── 牌库序(已知部分) ──────────────
    下一抽: 光子炮台 [探底置顶]          ← 顶牌已知, 抽到即确定
    底部3张: [生物计划][月火术][活体根须] ← 置底/探底揭示
    中段: 8张未知(组成见组件台账)
── 奇迹德组件台账 ──────────────────
  在手    : 光子炮台1/2 · 米斯塔·维斯塔1/1
  已消耗  : 黑市拍卖师1/2 · 雷霆绽放2/2 · 激活2/2 · 生物计划1/2
  牌库剩余: 光子炮台1 · 冷齿矿洞1 · 生物计划1 · 月光射线2 · 活体根须2 …
本回合链路(共11张): 拍卖师 → 雷霆绽放×2 → 水栖形态 → 建造水晶塔 → 光子炮台×2 → …
═══════════════════════════
```

（`⬇` 标记 = 该牌当前处于手牌减费状态，读实体 COST 标签。）

快照字段与 `GameState`/`DeckKnowledge` 的对应（即验收时的自检清单）：

| 输出字段 | 来源 |
|---|---|
| 血/甲 | 英雄实体 `HEALTH + ARMOR − DAMAGE` |
| 本回合已用费 | `RESOURCES_USED`；下回合费 = `min(10, RESOURCES + 1 − OVERLOAD_OWED − OVERLOAD_LOCKED)` |
| 手牌列表 | 己方 HAND 区实体 card_id（必有值；出现 None 即异常，打印 `?` 并记日志）；顺序 = `ZONE_POSITION`；`(费)` 与 `⬇` = 实体 `COST` 标签 |
| 牌库序 | DECK 区 `card_id` 非空实体（已揭示）+ 各自 `ZONE_POSITION`；见 §3.1 |
| 对手手牌 | 只显示张数（HAND 区实体计数），不显示内容 |
| 组件台账 | `decklist − seen_from_deck`；CREATOR 非空的实体不入 seen |
| 回合换算 | `TURN` 半回合制 + `FIRST_PLAYER` → "我的第 N 回合" |

### 4.3 终局行

```text
──── 对局结束 ──── 胜(湫然#51704) │ 用时~14回合 │ 快照已写入 data/sessions/20260906_0015/game_003.jsonl
```

M1 的 jsonl 内容 = 4.2 快照块的机器可读版（字段即上表来源，不含 advice 节）。

---

## 5. 边界情况处理表

| 情况 | 行为 |
|---|---|
| 一份日志多局（会话内连续对战） | CREATE_GAME 切局；注意该行在日志里出现两次（GameState 与 PowerTaskList 两个打印机），只认 `GameState.DebugPrintPower()` 前缀 |
| 中途切卡组（萨满局） | 通用模式：无台账，快照头部标注 |
| 对局中途程序启动 | 从头解析当前文件，只在第一个"未来事件"（游标之后）开始输出，避免把历史当实时 |
| 脏行/导出异常 | 跳行；导出失败放弃该局；FSM 回 IDLE 等下一个 CREATE_GAME |
| 客户端重开（新会话目录） | 每 30s 检查 logs-dir 下最新目录，切换后重置游标 |
| 己方手牌出现 card_id=None | 打印 `?`，计数入台账张数但不入卡名台账 |
| SETASIDE 里的瞬时实体（探底/发现选项界面） | 不算手牌、不算牌库；等其 ZONE 最终落定再入序模型（快照在回合结束时取，此时已落定） |
| 减费附魔随牌打出消失 | 不做任何清理——COST 标签跟着实体走，全量重建天然正确 |

---

## 6. 验收标准（跑一局真实对局）

1. 对局中实时逐行打印出牌/抽牌/攻击链路，费用明细正确（含星灵减费的 0 费显示）。
2. 我每个回合结束时输出完整快照一块，字段值与游戏画面人工核对一致（血/费/手牌/牌库数/下回合过载）。
3. 组件台账与实际一致：打过的组件从"牌库剩余"正确消失；衍生牌（建造水晶塔）出现在手牌但**不**减少台账。
4. 连续打两局（含中途切一次卡组）：切局干净，无上一局残留状态；萨满局正确进入通用模式。
5. 程序中途重启：恢复后从当前局继续，不重复输出历史。
6. 牌库序三件事各验证一次：水栖形态探底后"下一抽已知"提示与下回合实际抽到的牌一致；波涛形塑置底的 2 张出现在"底部已知"；生命缚誓者减费后手牌费用带 `⬇` 且数值与游戏画面一致。

> 2026-09-08 GameStore 重构后: fixture 语义 diff 零差异(两项裁定豁免: 首掉血收录/[T1] 链路戳); 真机日志 diff 与人工验收待游戏机执行。
