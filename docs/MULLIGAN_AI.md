# MULLIGAN_AI —— 起手留牌 AI(离线训练)

> 目标: 从 `data/training/` 语料学习「**按对手职业 × 先/后手**」的起手留牌策略,
> 回答"这手牌该留哪几张"。训练器 = [scripts/train_mulligan.py](../scripts/train_mulligan.py),
> 全部产物落 `data/models/mulligan/<卡组>/`, 供 `report`/`advise` 复用,
> 也是后续军师(M4)开局自动建议的数据源。

## 1. 数据流

```
Power.log ─(hsbot corpus.py: import-all / auto_training)→ data/training/<卡组>/*.jsonl
jsonl ─(train_mulligan: load_game)→ Game 样本 ─(统计表 + 逻辑回归)→ data/models/mulligan/<卡组>/vNNN/
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

## 2. 模型(双层)

留牌是**组合决策 + 只观察到自己行为**的数据(对手不广播决定), 业界主流做法
(HSReplay 留牌胜率表、组合多臂老虎机、胜率模型反事实翻位)在"单卡增益"层面
是同构的: 比较 **P(胜│留) 与 P(胜│换)**。本工具两层各司其职:

### 2.1 平滑统计表(主力, 零依赖)

对每张卡按 `(对手职业, 先/后手)` 计 四元组 计数:

```
keep: {w, l}   该卡被留下时    的胜负局数
drop: {w, l}   该卡被换掉时    的胜负局数
```

查询走**三级级联**(取样本最足的格子, 报告里标明出处):

```
职业×先手(n≥3) → 本职业(n≥5) → 全体
```

增益估计(参数集中在脚本头部, 可调):

```
留→胜率 = (keep_w + BETA_PRIOR_N × 全局胜率) / (keep_n + BETA_PRIOR_N)   # Beta 平滑, 换侧同理
增益    = w × (留→胜率 − 换→胜率) + (1−w) × 费用启发先验                  # w = min(留n,换n)/(min+SHRINK_N)
费用启发先验: ≤2费 +3% │ 3费 0 │ ≥4费 −4%/费(封底 −15%)                  # 弱先验, 数据主导
```

结论阈值: 增益 ≥ +3% 建议留 │ ≤ −3% 建议换 │ 其余中性; 样本 <6 局只报"样本不足"。
留/换任一侧无样本时 w=0 → 增益退化为费用先验(如"从未留过"的卡不会凭对手侧
数据直接翻成建议留), 两侧胜率照常展示供人工判断。

### 2.2 逻辑回归(可选, sklearn 训练 / 纯 JSON 推理)

每局一行: `手牌onehot + 留牌onehot + 先手 + 对手职业onehot`, 正则 C=0.3,
可捕捉单卡表看不到的手牌组合效应; 语料 <20 局不训。

- **评估防泄漏**: 按 时间序 前 80% 拟合 → 后 20% 留出打分(AUC / LogLoss),
  服务模型再用全量重拟合。
- **建议**: 贪心反事实翻位——从全换出发, 逐卡试"留下能否提高 P(胜)",
  复核到不动点; 不限先/后手时取先/后手两套建议的交集。
- **拍板权门控**: 留出 AUC ≥ 0.55 时 LR 才对最终建议有拍板权, 否则降为参考
  (几十局的 LR 常是噪声, 小样本下统计表更稳)。系数落 `model.json`,
  推理只用 `math.exp`, 不依赖 sklearn。

### 2.3 已知偏差(诚实条款)

- **选择偏差**: 只观察自己的决定。"留→胜率高"可能是"手牌好才留它"而非"它好"。
  缓解 = 看**留/换差值**与双侧样本量, 而非单侧胜率; 报告始终展示两侧。
- **离线策略数据**: 没有反事实(同一手牌留/换都打一遍)。样本过万前,
  结论是"倾向"不是"定理"; 想加速可故意换掉个别高置信卡做 ε-探索(人工操作)。
- 对手留牌不可观测 → 不建模对手手牌; 同职业不同形态(快攻/控制)暂不区分,
  后续可用对手前几回合行为聚类扩充条件维度。

## 3. 增量策略

**每次 train 全量重扫语料**(每局只读 `_meta` + 英雄实体前段, 成本恒小),
不做计数增量合并——全量重扫无状态漂移、可重现, 语料再大一个量级也够快。
"增量"体现在:

- 对比上一版 `games_seen.json`(文件名 + mtime/size) → 报告"较上一版新增 N 局";
- 产物按版本目录 `vNNN` 留档, `LATEST.json` 指向最新, 旧版可回溯对比;
- 打几局 → 跑一次 train, 模型即吸收新对局(建议每 10~20 局或换环境后重训)。

```
data/models/mulligan/<卡组>/
  v001/ meta.json(局数/新增/跳过/对手分布/LR指标) · stats.json(计数表+全局胜率)
        model.json(LR 系数, 可选) · games_seen.json(增量游标)
  LATEST.json
```

## 4. 用法

```bash
python scripts/train_mulligan.py                    # 训练+报告(config 默认卡组)
python scripts/train_mulligan.py --deck 奇迹德      # 指定卡组
python scripts/train_mulligan.py report             # 查看已保存模型
python scripts/train_mulligan.py advise --vs 圣骑士 --coin \
    --hand 水栖形态,黑市拍卖师,顺水漂流              # 给一个起手出建议
python scripts/train_mulligan.py --config my.yaml ...   # 配置参数须在子命令前
```

`advise` 的 `--vs` 接受 中文名(圣骑士)/CLASS 标签(PALADIN)/英雄卡(HERO_04)/`*`;
`--hand` 接受中文名或 `card:ID`; `--coin`/`--first` 指定后手/先手(缺省=不限,
统计表走"本职业"级联, LR 取先/后手交集)。

## 5. 与 hsbot 的集成路径

军师(M4)在收到 `mulligan` 事件时: 读 `LATEST.json` → `stats.json`/`model.json`
→ `card_advice()` 出逐卡结论, 经 render 层进悬浮窗。推理零 sklearn 依赖,
`train_mulligan.py` 的 `card_advice/lr_advice` 即推理 API(后续如需常驻,
再把这两个纯函数提升进 `hsbot/analysis/`)。

## 6. 业界参照

- [HSReplay: The Art of the Mulligan](https://articles.hsreplay.net/2019/04/15/the-art-of-mulligan/)
  与 [卡牌留牌数据](https://articles.hsreplay.net/2020/07/23/card-mulligan-data/):
  留/换胜率表是行业标准口径, 留牌选择影响胜率 5~10%。
- [Approaching Hearthstone as a Combinatorial Multi-Armed Bandit Problem
  (Maastricht U., 2019)](https://project.dke.maastrichtuniversity.nl/games/files/msc/Valkenberg_Thesis.pdf):
  起手/选牌 = 组合多臂老虎机, 支持本工具"单卡增益 + 组合微调"的分解。
- [Predicting Hearthstone game outcome with ML (elie.net)](https://elie.net/blog/hearthstone/predicting-hearthstone-game-outcome-with-machine-learning)
  与 [r/competitivehs 对单卡留牌胜率偏差的批评](https://outof.games/devtracker/hearthstone/reddit/r-competitivehs/3171):
  "训练胜率模型再反事实评估留牌位"正是 §2.2 的做法与 §2.3 的偏差来源。
