# 架构总览(2026-09-13 分层化重构后)

> 设计原则:单一职责 + 单一状态权威 + 依赖单向。
> store 只维护状态;analysis 只解释卡牌/效果;render 只输出;
> watcher 是编排门面;行级处理是责任链。

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
| `effects.EffectCache/GRAMMAR` | 效果 IR 编译与增量缓存 | 不参与状态权威 |
| `watcher.EventRouter(_route)` | 事件消费决策(标题/快照触发/链路) | 不维护游戏事实(事实在 store) |
| `watcher.SnapshotService` | 快照构建+分发+持久化 | 不做游戏决策 |
| `watcher.TrainingExporter` | 训练样本+原始切片导出/收尾 | 不做游戏决策 |
| `corpus.CorpusExporter` | 语料落盘+Decks 归因+import-all | 不平行推导对局事实(读 store) |
| `overlay.OutputHub/OverlayWindow` | 输出三路分发+半透明窗+分色+几何记忆 | 不做游戏决策 |

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

## 5. 已知债务(docs/AUDIT_2026-09-13.md)

- 常量/阈值未完全集中(consts 模块待建);
- GRAMMAR 语言耦合(zhCN 专用, 待 locale 参数化);
- 快照重建全量扫实体(实体数上千时的性能边界);
- overlay 行分类兜底仍依赖 render 行首格式(tag 显式化已为主路径)。
