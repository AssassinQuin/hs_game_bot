# 架构总览(2026-09-13 分层化重构后)

> 设计原则:单一职责 + 单一状态权威 + 依赖单向。
> store 只维护状态;analysis 只解释卡牌/效果;render 只输出;
> watcher 是编排门面;行级处理是责任链。

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

## 1. 模块与类图

```
                        Power.log(行流)
                             │
                ┌────────────▼─────────────┐
                │ pipeline.py  责任链       │
                │  StreamContext            │  流缓冲唯一维护点
                │   (尾包/括号线索/真名/     │  (跨局存活,局边界清空)
                │    当局切片行)             │
                │  TailState→Boundary→      │
                │  GameLines→PlayerName→    │
                │  Hint→Feed→ShowDraw       │
                └────────────┬─────────────┘
                             │ 包游标(扣包/释放/隔离)
                ┌────────────▼─────────────┐
                │ watcher.Watcher(门面)     │  会话发现/attach/循环
                │  SessionScanner           │
                │  GameScope(局作用域)      │  新局=整体替换
                │  EventRouter(_route)      │
                └────────────┬─────────────┘
                             │ 事件(dict, 不可变约定)
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                     ▼
  analysis.EffectAnalyzer  render(注册表)      snapshot_service/
  (卡牌/效果解析,           chain_renderer(k)   TrainingExporter
   enrich 实时富化)          纯格式化             corpus/persist
```

## 2. 类与职责(单一职责清单)

| 类/模块 | 职责 | 明确不做 |
|---|---|---|
| `pipeline.StreamContext` | 流缓冲唯一维护点 | 不解释语义 |
| `pipeline.LineHandler` 及子类 | 各自一行的处理 | 不跨行决策 |
| `pipeline.LinePipeline` | 链组装与逐行驱动 | 不做业务 |
| `watcher.SessionScanner` | 最新会话目录发现 | 不读文件内容 |
| `watcher.GameScope` | 局作用域状态容器 | 无行为 |
| `store.GameStore` | 唯一状态权威(实体/标签/快照查询) | 不做卡牌/效果解释、不格式化 |
| `analysis.EffectAnalyzer` | 卡牌/效果解析($N/预估/指纹) | 不改状态 |
| `analysis.lethal_plan` | 斩杀线编排: store/台账事实 → planner 纯函数 → 机读 facts | 不改状态; 措辞零(结论词归 render) |
| `planner/(pieces/simstate/dfs/plan)` | 出牌线纯函数搜索(编译→记忆化 DFS→Plan) | 无 IO/无全局态; 仅依赖 hsbot.consts; 不 import store/watcher |
| `effects.EffectCache/GRAMMAR` | 效果 IR 编译与增量缓存 | 不参与状态权威 |
| `watcher.EventRouter(_route)` | 事件消费决策(标题/快照触发/链路) | 不维护游戏事实(事实在 store) |
| `watcher.SnapshotService` | 快照构建+分发+持久化 | 不做游戏决策 |
| `watcher.TrainingExporter` | 训练样本+原始切片导出/收尾 | 不做游戏决策 |
| `corpus.CorpusExporter` | 语料落盘+Decks 归因+import-all | 不平行推导对局事实(读 store) |
| `overlay.OutputHub/OverlayWindow` | 输出三路分发+三区悬浮窗(上信息区/中上部推荐区/中下日志流, 上两区实底不透明)+分色+几何记忆+局终清空重置 | 不做游戏决策(机读字段驱动, 不从文本反推) |
| `analysis.assemble_snapshot` | store/knowledge → (SimSnapshot, pieces) 装配投影 | 不改状态; planner 依赖铁律不破 |
| `watcher.AuditExporter` | 预测-实测对账 JSONL 落盘 | 不做游戏决策; 零干扰(失败仅 WARNING) |

## 3. 设计模式落位

| 模式 | 落位 |
|---|---|
| 责任链 | `pipeline.py`:TailState→Boundary→GameLines→PlayerName→Hint→Feed→ShowDraw |
| 观察者(带隔离) | `store._emit_event` → 订阅者逐个 try/except(`EventRouter`/测试收集器) |
| 策略+注册表 | `render.chain_renderer(kind)` 渲染器注册表 |
| 门面 | `watcher.Watcher`(run_live/run_replay/_attach 对外入口) |
| 作用域/状态对象 | `GameScope`(局)、`StreamContext`(流)、`MulliganState`(留牌) |
| 模板方法 | `pipeline.LineHandler.handle`(process 子类实现) |
| 增量编译 | `effects.GRAMMAR`→`CardEffect`→`EffectCache`(指纹失效) |

## 4. 不变式(改代码前必读)

1. 状态只经 store 变更入口(apply/note_friendly/hint_*/settle)变化;
   store 事件是只读事实, 订阅者不得改 store。
2. 行链顺序不可调换: Boundary 必须先于 Feed(换局 manager 重置/切片边界);
   Hint 先于 Feed(线索在本批 flush); ShowDraw 在 Feed 之后(读解析前状态)。
3. 局作用域替换(_new_game)即全部重置; 新增每局字段 = 在 GameScope 加字段,
   不在 Watcher 加属性。
4. 渲染层零游戏规则; 解析层零状态维护; 两者都零 IO。
5. 对局事实唯一来源 = store; 训练语料/快照的 meta 全部读 store
   (corpus 不再平行推导)。
6. (2026-09-14 补记) overlay → render.plan_line/advice_rows 为单向措辞复用边
   (结论词单一出处; 可斩线自上区移入中上部推荐区, 推荐打法 top3 封顶);
   planner 为 analysis.lethal_plan 委托的纯函数包,
   依赖仅 hsbot.consts。

## 5. 已知债务(docs/AUDIT_2026-09-13.md)

- 常量/阈值未完全集中(consts 模块待建);
- GRAMMAR 语言耦合(zhCN 专用, 待 locale 参数化);
- 快照重建全量扫实体(实体数上千时的性能边界);
- overlay 行分类兜底仍依赖 render 行首格式(tag 显式化已为主路径)。
