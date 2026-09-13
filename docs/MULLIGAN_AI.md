# MULLIGAN_AI —— 起手留牌 AI(离线训练)

> 目标: 从 `data/training/` 语料学习「**按对手职业 × 先/后手**」的起手留牌策略,
> 回答"这手牌该留哪几张"。训练器 = [scripts/train_mulligan.py](../scripts/train_mulligan.py),
> 全部产物落 `data/models/mulligan/<卡组>/`, 供 `report`/`advise` 复用,
> 也是后续军师(M4)开局自动建议的数据源。
>
> v2 核心逻辑: **结论有出处, 建议按集合, 探索有方向, 证据可配对**。
> 观察数据只能反映你自己的习惯(见 §5), 所以这套逻辑的关键是让
> "留其他牌可能更好"能从 先验 → 猜测 → 数据结论 逐级升级。

## 1. 数据流

```
Power.log ─(hsbot corpus.py: import-all / auto_training)→ data/training/<卡组>/*.jsonl
jsonl ─(train_mulligan: load_game)→ Game 样本 ─┬─ 平滑统计表 stats.json
                                               ├─ 逻辑回归 model.json(可选)
                                               ├─ 同情境匹配索引 games_digest.json
                                               └─ meta.json / games_seen.json
data/mulligan_prior.yaml(人工维护, 入库)───────┘(专家先验, 每次训练/建议时读取)
```

每局 JSONL 首行 `_meta` 已含 玩家/胜负/留牌(offered/kept/replaced)/卡组清单,
对手职业**统一从事件流提取**(英雄实体 FullEntity 的 `CLASS` 标签)——
2026-09-13 前导入的旧样本 `_meta.heroes` 大面积为空, 事件流永远可靠, 不依赖 meta。

### 样本提取规则(每局的取舍)

| 规则 | 原因 |
|---|---|
| 我方 = config `battletag` 匹配; 兜底取唯一广播留牌决定的一方 | 友方判定首选战网名(跨局稳定), 见 M1_MONITOR §1 |
| 无结果(PLAYING)/未广播决定/起手为空 → 跳过并计数 | 无标签或无动作的数据不可训练; 跳过原因打印在报告里 |
| 幸运币(`is_coin`: GAME_005 / *COIN*)从 offered/kept 剔除, 只留作"后手"标记 | 币不可换, 不参与决策 |
| 起手可含同名两张 → 按逐张计(Counter), 各自计入留/换 | 洗牌可抽到双张 |

## 2. 模型

### 2.1 平滑统计表(主力, 零依赖)

对每张卡按 `(对手职业, 先/后手)` 计四元组 `keep:{w,l}` / `drop:{w,l}`,
查询走**三级级联**(取样本最足的格子, 报告标明出处):
`同先手(n≥3) → 本职业(n≥5) → 全体`。

```
留→胜率 = (keep_w + BETA_PRIOR_N × 均值) / (keep_n + BETA_PRIOR_N)   # 换侧同理
增益    = w × (留→胜率 − 换→胜率) + (1−w) × 先验                      # w = min(留n,换n)/(min+SHRINK_N)
```

结论阈值: 增益 ≥ +3% 建议留 │ ≤ −3% 建议换 │ 其余中性; 样本 <6 局标"样本不足"
(有专家先验的卡除外, 见 §2.2)。

### 2.2 专家先验文件(人工维护, 入库)

`data/mulligan_prior.yaml`, 按卡组分节; `keep` 是留牌增益先验,
`keep_coin`/`keep_first` 覆盖特定手, `pairs` 是两张同留的协同加成,
卡名/`card_id` 均可。作用:

- **无数据兜底**: 你从没留过/没换过的卡, 由先验给出初始结论(出处标"专家先验"),
  而不是一律"样本不足";
- **有数据收缩**: 数据侧的 Beta 均值与收缩目标向先验偏移, 话语权随样本衰减
  (`w = n/(n+4)`, 数据越来越多 → 先验自动让位);
- **协同注入**: `pairs` 加成计入集合枚举总分, 让"拍卖师+过牌"这类组合知识
  在数据不足时也参与决策。

`engine` 列出引擎卡(预留, 供近似手牌配对加权)。奇迹德一节是 AI 填的初始草稿,
**请按手感改**; 删掉整节 = 回到纯数据驱动。

### 2.3 逻辑回归(可选, sklearn 训练 / 纯 JSON 推理)

每局一行: `手牌onehot + 留牌onehot + 留牌组合特征 + 先手 + 对手职业onehot`。
组合特征 = 共同留过 ≥`LR_PAIR_SUPPORT`(4) 次的卡对 conjunction, 可捕捉
"拍卖师+廉价法术"式配合; 正则 C=0.3; 语料 <20 局不训。

- **评估防泄漏**: 时间序前 80% 拟合 → 后 20% 留出打分(AUC / LogLoss),
  服务模型再用全量重拟合;
- **拍板权门控**: 留出 AUC ≥ `LR_AUC_GATE`(0.55) 时 LR 才对最终建议有拍板权,
  否则降为参考(几十局的 LR 常是噪声)。系数落 `model.json`, 推理零 sklearn。

### 2.4 建议按集合: 枚举 2^n 个候选留牌组合

留牌是**选集合**, 不是 n 个独立开关(单卡逻辑看不见"拍卖师+过牌"配合)。
`advise` 在全部候选集合上取分最高(升序枚举+严格比较 → 平分取最小集):

```
统计表路径: score(set) = Σ card_advice 增益 + Σ 专家 pairs 加成
LR 路径(过门控): score(set) = LR P(胜 │ 手牌, set, 职业, 先/后手)
```

起手最多 4 张(≤16 组合), 理论穷举无压力; 卡数超过 `ENUM_MAX_CARDS`(10) 退回贪心。

### 2.5 Thompson 探索: 往最缺证据的方向打

观察数据是"只观察自己决定"的离线数据——**从不验证的方向永远没有数据**
(§5)。`advise --explore` 不用增益均值, 而是从每张卡的留牌增益**后验抽一次样**
(Beta: 留侧/换侧各自 `Beta(胜 + a×均值, 负 + a×(1−均值))`, 取差)按抽样值决策:

- 数据足的卡: 后验窄 → 抽样≈均值 → 行为稳定(利用);
- 没验证过的卡: 后验宽 → 抽样经常翻面 → 自动产生"故意留/故意换"的探索局。

探索预算自动分配给最缺数据的地方, 无需手调 ε。`--seed` 可复现;
输出标注探索偏差("均值 −2% → 抽样 +4%"), 这些对局落库后, 统计表对
被习惯锁死的卡就有了两侧样本。

### 2.6 同情境匹配证据: 给对比找可比对象

"换→胜率 23%" 混淆了手牌上下文(它只在"这卡没用的手"里被换过)。
训练时把每局压缩成 `{职业, 先/后手, 起手集合, 留牌集合, 胜负}` 存入
`games_digest.json`; `advise` 逐卡给出**可比对局**的对照:

```
近似手牌(同职业同手 且 与当前起手共享≥2张, ≥3局) → 同职业同手(≥3局) → 无可比对局
近似手牌9局: 留4(3胜1负) 换5(1胜4负)
```

来源、样本、可比性全透明; 没有可比对象就直说, 不退回先验装作有结论。

## 3. 结论的三层来源(逐卡"出处"列)

```
同先手数据 > 本职业数据 > 全体数据 > 专家先验 > 相似卡迁移(预留) > 费用启发
```

费用启发(≤2费 +3% │ 3费 0 │ ≥4费 −4%/费, 封底 −15%)是**最弱档**,
只在无任何数据且无先验时兜底, 结论标"样本不足"。

## 4. 已知偏差(诚实条款)

- **选择偏差**: 只观察自己的决定。"留→胜率高"可能是"手牌好才留它"。
  缓解: 看**留/换差值**与双侧样本量、匹配证据限定可比对局、LR 整手牌条件化;
  无法根治——Thompson 探索(§2.5)是唯一产生真反事实数据的途径。
- **偏差方向 = 确认现有习惯**: 从不换的卡, 换侧无样本 → 增益退为先验;
  从不留的卡同理。所以模型天然倾向保守, 探索局是打破习惯锁的钥匙。
- **对手不可观测**: 对手留牌不广播, 不建模对手手牌; 同职业不同形态
  (快攻/控制)暂不区分, 后续可用对手前几回合行为聚类扩充条件维度。

## 5. 增量策略

**每次 train 全量重扫语料**(每局只读 `_meta` + 英雄段, 成本恒小, 无状态漂移),
不做计数增量合并——全量重扫无状态漂移、可重现。"增量"体现在:

- 对比上一版 `games_seen.json`(文件名 + mtime/size) → 报告"较上一版新增 N 局";
- 产物按版本目录 `vNNN` 留档, `LATEST.json` 指向最新, 旧版可回溯对比;
- 打几局 → 跑一次 train, 模型即吸收新对局(建议每 10~20 局或换环境后重训)。

```
data/models/mulligan/<卡组>/
  v001/ meta.json(局数/新增/跳过/对手分布/LR指标) · stats.json(计数表+全局胜率)
        model.json(LR 系数, 可选) · games_digest.json(匹配证据索引)
        games_seen.json(增量游标)
  LATEST.json
```

## 6. 用法

```bash
python scripts/train_mulligan.py                    # 训练+报告(config 默认卡组)
python scripts/train_mulligan.py report             # 查看已保存模型
python scripts/train_mulligan.py advise --vs 圣骑士 --coin \
    --hand 水栖形态,黑市拍卖师,顺水漂流              # 建议(集合枚举)
python scripts/train_mulligan.py advise --explore --seed 7 \
    --hand 水栖形态,黑市拍卖师                       # Thompson 探索局
```

先验文件改完即生效(每次 train/advise 都重新读取), 无需重训。
`advise` 的 `--vs` 接受 中文名/CLASS 标签/英雄卡/`*`; `--hand` 接受中文名或
`card:ID`; `--coin`/`--first` 指定后手/先手(缺省=不限, 统计表走"本职业"级联,
LR 取先/后手交集)。

## 7. 实时集成(已落地)

留牌建议作为责任链上的一环进入实时军师, 每层各加一段、职责不变:

```
store   发结构化事件 mulligan_offer{actor, offered[cid], msg兜底文本}
        → analysis.enrich   我方事件才富化: 对手职业(事件流英雄CLASS)+幸运币剔除
          → MulliganAdvisor.advise()  读 LATEST 模型+专家先验(mtime 失配自动重读)
        → render            @chain_renderer("mulligan_offer"): 有 advice 出建议行,
          无建议器/无模型 → 回退"起手可留"
        → watcher._route    事件→输出种类映射表(_MSG_KIND_BY_EVENT), advice 独立分色
        → overlay           KIND_ADVICE 金色高亮(config overlay_colors.advice 可调)
```

输出形态: `[T1·我] 【留牌建议·vs牧师·后手】留 黑市拍卖师(+20.0%)、顺水漂流(+8.3%) │ 换 月火术(+4.0%)`。
开关 `mulligan_advice`(config.yaml, 默认开); 推理零 sklearn 依赖 ——
`hsbot/mulligan_ai.py` 是纯 stdlib 推理层, 训练器(scripts/train_mulligan.py)
与实时军师共用同一份结论函数与先验文件, 训练出新版本即自动生效。

## 8. 业界参照

- [HSReplay: The Art of the Mulligan](https://articles.hsreplay.net/2019/04/15/the-art-of-mulligan/)
  与 [卡牌留牌数据](https://articles.hsreplay.net/2020/07/23/card-mulligan-data/):
  留/换胜率表是行业标准口径, 留牌选择影响胜率 5~10%。
- [Approaching Hearthstone as a Combinatorial Multi-Armed Bandit Problem
  (Maastricht U., 2019)](https://project.dke.maastrichtuniversity.nl/games/files/msc/Valkenberg_Thesis.pdf):
  起手/选牌 = 组合多臂老虎机 —— 本工具的 集合枚举 + Thompson 采样 正是其
  实用化落地。
- [Predicting Hearthstone game outcome with ML (elie.net)](https://elie.net/blog/hearthstone/predicting-hearthstone-game-outcome-with-machine-learning)
  与 [r/competitivehs 对单卡留牌胜率偏差的批评](https://outof.games/devtracker/hearthstone/reddit/r-competitivehs/3171):
  "训练胜率模型再反事实评估留牌位"正是 §2.3 的做法与 §4 的偏差来源。
- Thompson sampling: Thompson(1933) 的贝叶斯多臂老虎机方案, 工程综述见
  [A Tutorial on Thompson Sampling](https://arxiv.org/abs/1707.02038)。
