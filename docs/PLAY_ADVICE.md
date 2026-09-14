# 推荐打法 —— 出牌建议施工图（下阶段定版）

> 上游文档：[DESIGN.md §6](DESIGN.md)（DFS+MC 算法设计，本文不重复其公式）· [PROJECT.md §4](PROJECT.md)（里程碑）。
> 2026-09-13 算法定版（外部调研 + 项目实况对齐）。前一阶段同款文档：[MULLIGAN_AI.md](MULLIGAN_AI.md)。

## 0. 定版结论：算法分层

一句话：**AI 价值模型 × 浅层世界节点推演 = 推荐打法主干；DFS = 确定性内核（斩杀/伤害线）；MC 抽牌外壳 = 第 3 段；MCTS = 第 4 段，暂缓。**
四段是同一条谱系，分界线 = 推演深度 × 转移模型保真度，不是四种对立方案：

| 段 | 引擎 | 前提 | 落点 |
|---|---|---|---|
| 1 | 特征差分世界节点 × 价值模型 P(胜\|局面) | trainer 模型产物 | `hsbot/play_ai.py`（新） |
| 2 | DFS 记忆化 + 上界剪枝（封闭伤害子空间） | effects IR | `planner/`（新包） |
| 3 | MC 抽牌采样外壳，采样线内 = 段 2 | 段 2 + 台账牌库序 | `planner/mc`（新） |
| 4 | MCTS（UCT 引导树，ISMCTS 形态） | 段 3 + IR 广谱覆盖 + C 速性能 | 达 §1-T5 门槛才议 |

**MCTS 暂缓的依据（2026-09-13 调研）：**
1. 全量模拟器不存在——hslog 只解析不模拟；新版 CardDefs.xml 已剥离 Power/Task 脚本（SabberStone 式任务树不可行，见 [EFFECTS_DESIGN.md](EFFECTS_DESIGN.md) v2 调研）；SabberStone 本身止步 2017 年鉴后停维护。Hearthstone AI Competition（2018–2020）冠军全是 MCTS 系，但全部跑在自带全量模拟器上——深度搜索的隐藏成本 = 先重造引擎。
2. 暗牌确定化搜索有已证实的系统性偏差：strategy fusion / non-locality（Frank & Basin 1998；ISMCTS, Cowling et al. 2012 修补）。
3. `effects.py` IR 实测覆盖 291 张里未覆盖 266 张——转移模型保真度不足时，搜得再深也是垃圾进垃圾出。MCTS 相对均匀 MC 的增益只是搜索效率，答案上限由转移模型决定。

## 1. 任务拆解

### T1 `planner/` 包：斩杀 DFS —— 立即可做，不依赖数据量

与 `trainer/` 同款解耦：独立目录、单向依赖 planner→hsbot、接合点=文件。

- `planner/simstate.py`：`SimState` 可哈希轻量状态 `(mana, hand 多重集, spell_damage, sd_carriers, engine_on, discount, drawn_pos)`，与 GameStore 完全解耦（记忆化 key 即它，DESIGN §6.1）。
- `planner/dfs.py`：`best_line(state)`，记忆化 + 上界剪枝（公式照 DESIGN §6.1：`当前伤+Σ剩余牌最大伤+剩余抽牌最大伤 ≤ 已记录最优 → 砍`）。
- `planner/plan.py`：`Plan(actions 有序, total, lethal)`——输出结构，渲染素材。
- 转移规则全走 `effects.py` IR：Damage 逐段×法强 / ManaGain 回费 / CostDown 减费；engine_on=每打一张法术 `drawn_pos+=1`。单牌事实源 = `analysis.burst_damage(card_id, spellpower)`，不另写解析。
- 抽牌 v1：台账已知顶/底牌 = 确定值，未知牌 = 期望伤害折算（精确 MC 留 T3）。
- 场面攻击 v1 忽略（DESIGN 已注明字段预留），v2 再加。
- **验收**：构造局面人工验证最优线（原 M3 验收）；`python -m hsbot replay <切片>` 端到端零异常；planner 只回结构，"可斩：X→Y→Z 总伤 N" 措辞由 render 出。

### T2 `hsbot/play_ai.py`：世界节点 × 价值模型排序骨架

与 `mulligan_ai.py` 完全同款模式（已验证的热更新军师）：

- `PlayAdvisor`：`models_root_for(data_dir, deck)` + `read_latest()` mtime 热更新；模型 = `python -m trainer train` 产物（`data/models/play/`）。
- 打分：候选 = 深度 1 动作（每张可支付手牌 + 关键二连如"法强牌→法术"）；节点 = `trainer/states.flatten` 向量**特征差分**（费−、手牌数−1、场面攻血+、法强+、牌库−1 并入手牌期望），不需要模拟 GameStore；`score = trainer.value.predict(model_dir, snap')`。
- 事件链加环不加分发（责任链契约）：store 发 `play_offer`（我方 `turn_start`；每次 `play` 后批尾重发）→ `analysis.enrich` 挂 advisor 富化 → `render` 加 `@chain_renderer("play_offer")` → KIND_ADVICE 金色高亮。
- **开口门槛**：语料 <300 局不出排序，只出 T1 斩杀线（沿 mulligan `N_ADVICE_MIN` 思想，门槛随语料自动伸缩）。
- **时序**：`turn_start` 出主建议；每次打出后等抽牌/回费事件到齐（复用 pend 批尾派发）再重算；对手回合静默。
- **验收**：回放切片上建议行与真实操作时刻对齐（`live-output-timing` 同款标准）；无模型/无卡表优雅降级（退化为只出斩杀线，卡名显示 card_id）。

### T3 MC 抽牌外壳（原 M4 内容）

- N≈2000 采样线：remaining 多重集不放回抽 k → 每条线内跑 T1 DFS → `DamageDistribution`；P(斩杀) = P(分布 ≥ 血+甲)。已知顶牌不进抽样（降方差——牌库序台账的算法层回报，DESIGN §6.2）。
- 预备模式：`enumerate_setup_options()` 各候选的下回合 P 对比（DESIGN §6.3）。

**落地状态（2026-09-14）**：`planner/mc.py` 纯函数层已落——`mc_plan()` 样牌进
known_draws 队尾不直入手（防 P(斩杀) 虚高，宁漏勿错）、known_draws 从抽样池
扣 1 防双计、cost=(cid,面值) 预映射、k 缺省=hand_draws+engines×手牌数、
budget_ms 硬墙钟诚实降级；超几何闭式解对拍+seed 逐位一致 14 测绿。
**渲染/预备模式接线未做**（P(斩杀) 进悬浮窗与 `enumerate_setup_options` 挂
后续切片）；典型中局 27.6ms/线，恶意最坏 ~1s/线，预算内跑。

### T4 TabPFN 基座切换（数据达标后）

- 接进 `trainer.value.predict` 后端，接口不变；与 GBM 分组 CV 对照后拍板（环境与版本钉见 windows-env-quirks 记忆）。

### T5 MCTS 重启门槛（远期，全满足才排期）

- IR 未覆盖率降到可接受线（以 `python -m hsbot parse-cards` 效果分类统计为准；口径见 [MCTS_RESEARCH.md](MCTS_RESEARCH.md)：应区分"全卡池"与"己方卡组"两个口径，重启 MCTS 的最小门槛 = 己方卡组 16 卡条目全原语可执行 + 结算引擎）；
- 性能预算成立（纯 Python 不可行，需 C 扩展/多进程方案论证）；
- 价值模型在 300+ 局上 AUC 稳定达标。

## 2. 输出语义：两级"最优"，措辞必须区分

| 级别 | 引擎 | 可证明性 | render 措辞 | 例 |
|---|---|---|---|---|
| 确定性最优 | DFS | 可证明 | "可斩" | `可斩：月光射线→法强→… 总伤 24 ≥ 敌 22` |
| 统计最优 | 价值模型 top-k | 仅期望 | "期望更优" | `推荐：拍卖师(ΔP +2.3%) > 滋养(ΔP +0.8%) > 不动` |

分层铁律不变：**推理层只回机读事实（分数/排序/ΔP/线结构），结论词与阈值归 render。**

## 3. 数据闭环

`play_offer` 建议与玩家实际 `play` 事件同带 `game_id` 入 rich_events/JSONL → 回测对账"建议 vs 实际选择"，偏差样本即下一轮训练素材（M5 回测精神）。建议本身不改变用户操作，无反馈污染。

## 4. 依赖与顺序

```text
scripts/fetch_cards.py(恢复卡表) ──► T1 素材精度 · T2 特征质量
T1(斩杀 DFS) ──► T3(MC 外壳) ──► T5(MCTS 门槛)
T2(排序骨架) ──► T4(TabPFN 换基座)
```

开工顺序：**T1 → T2 →（300 局语料达标）→ T3 → T4**；T5 未达门槛不排期。
跨包依赖：planner→hsbot（复用 effects/analysis/knowledge），与 trainer→hsbot 同向，bot 不感知。

## 5. 明确不做

- 全回合 MCTS / 自建对局模拟器（§0 依据 1）；
- 端到端 RL（无模拟器，既定决策）；
- 多卡组通用出牌 AI（只训奇迹德，既定决策）。

---

## 6. T1 详细开发方案（第一阶段施工详图，2026-09-13）

### 6.0 目标与非目标

**目标**：给定我方回合任意决策点的实况状态，搜索手牌出牌序列，输出 ①确定性可斩线（可证明：出牌顺序→总伤≥敌血甲）②无斩时的最优伤害线与 mana/回费/减费交互说明。DFS 状态空间封闭（≤10 费/≤10 手牌），毫秒级，挂现有信息区批尾重算点。

**非目标**（留 T3/T2）：MC 抽牌采样（v1 只做期望折算注记）、场面随从交换/攻击目标选择（场攻作常量加成）、对手响应建模、价值模型排序。

### 6.1 前置（P0）：effects IR 两处扩展

现状缺口（16 卡条目实测：0/16 完全可模拟，见 [MCTS_RESEARCH.md](MCTS_RESEARCH.md)）：IR 只有 Damage/Heal/ManaGain/CostDown/Mechanic/Unknown，**无法强增益、无施法抽牌**——奇迹德 OTK 的两个引擎（虚灵改装师/顺水漂流法强附魔、拍卖师施法抽一张）都没表达。两条都是数据驱动扩展（加语法规则，不逐卡写死）：

1. **SpellPower IR 族**：`@dataclass SpellPower(amount:int)`；语法规则候选措辞"你的法术伤害+N"（正则以语料实测措辞定）。影响：`compile_card` 加 family、法强求值消费方、**COMPILER_VERSION 5→6** 强制缓存重编译。
2. **cast_draw 机制标记**：MECHANICS 文本表加"每当你施放…抽一张牌"→`Mechanic("cast_draw")`；piece 编译时据此置 `engine=True`。

### 6.2 planner/ 包结构与数据契约

独立包，单向依赖 planner→hsbot（与 trainer 同款解耦；analyzer/carddb/knowledge 事实经参数传入，不 import watcher/store）：

```text
planner/
  pieces.py    Piece 编译层: (card_id, 实际费) → Piece
  simstate.py  SimState 不可变状态 + play() 转移(纯函数)
  dfs.py       best_line(state) 记忆化+上界剪枝
  plan.py      Plan 输出结构
```

```python
@dataclass(frozen=True)
class Piece:                      # pieces.py 产出, 进程内按 (cid, cost) 缓存
    card_id: str
    cost: int                     # 真实手牌费: COST 标签优先(减费在身), 否则基础费
    segments: tuple[int, ...]     # 伤害段基础值; scaled=False 段不进(斩杀既定口径)
    spell_scaled: bool            # 法术/英雄技能吃法强(consts.SPELLPOWER_TYPES)
    mana_gain: int                # ManaGain 合计
    spellpower_gain: int          # SpellPower 合计(打出后的法强增量)
    discount_hand: int = 0        # CostDown hand:法术 面值(入 disc_hand 余额)
    discount_next: int = 0        # CostDown next:* 面值(v1 类目近似)
    engine: bool = False          # cast_draw: 每施放一法术抽一张
    is_spell: bool = False

@dataclass(frozen=True)
class SimState:                   # 记忆化 key 即本结构(全 tuple/frozen)
    mana: int
    hand: tuple                   # ((card_id, cost), ...) 排序规范化
    sp: int                       # 当前法强
    disc_hand: int                # 在身的手牌法术减费余额
    disc_next: int                # next:* 减费剩余次数
    engines: int                  # 在场引擎数(施法抽牌张数)
    drawn: int                    # 已消耗的未知抽牌数(上界剪枝用)
    known_draws: tuple            # 已知顶/底待抽队列(确定值, 消耗式)
    face: int                     # 已累计伤害
```

### 6.3 算法规格

- **转移**（`simstate.play(state, i)`）：扣费 `max(0, cost − disc_hand(若法术) − disc_next)`；**disc_next 是一次性面值**（2026-09-14 审计修正：伺机待发"下一法术-3"=下一张全额减 3、溢出浪费、用后清零；旧实现按"-1×N 张"会让线内多打一张，误报可斩）；`is_spell and engines>0` → 引擎抽牌（`known_draws` 非空则弹出队头为新可支付牌，否则 `drawn+=1`）；伤害段结算 `(base + sp·spell_scaled) × len(segments)` 累进 face；`spellpower_gain` 加 sp；`mana_gain` 加 mana。
- **抽牌两态**（2026-09-14 审计修正）：台账顶牌恒为确定抽；**底牌仅在 `unknown_middle==0`（全库已知）时进确定队列**（且队列剔除顶牌、按牌位升序——rebuild 的 bottom_map 含 pos=1 顶牌，直接拼会双计），否则该抽位实际是未知牌，底牌降级走期望通道（remaining 仍含底牌；宁漏勿错）。未知牌 v1 **期望折算**——`E[dmg]=Σ(remaining×burst)/Σremaining`，只在 Plan 注记为期望分量，**不计入可斩判定**（确定性可斩只用已知牌；现有信息区粗估口径不动，两者并存）。
- **记忆化**：dict[七元组决策点 → (后缀伤害, 后续动作)]（face/drawn 不进键，后缀不变性）；实测 10 异牌 28703 → 5631 状态。
- **上界剪枝**：已退役（2026-09-13 落地时裁定：incumbent 型上界与记忆化组合会投毒或退化为重算；毫秒级出线由键坍缩达成，dfs.py 模块注释有论证）。
- **场攻**：`st.board_face_attack(me)` ——**可直击脸的场攻**（2026-09-14 审计修正：敌方嘲讽在场 → 0；剔除冻结/本回合已攻击/本回合下场无冲锋；旧 `board_attack` 全额计入会在"有嘲讽"局面误报可斩。粗估行的"场Z"仍用 board_attack 总攻，两者并存）。不进 DFS 分支（攻击不耗费、无目标交换语义）。
- **效果口径**（2026-09-14 审计修正）：随从-only AoE（scope=all_minions）打不了脸不进 segments；random_split 段进但不吃法强（§8 定版）；临时法强（"下一个法术伤害+N"）与"双方玩家的法术伤害+N"不编译（宁漏勿错）。
- **终止**：手牌无可支付 → 叶；`total = face + board_atk`；`lethal = total ≥ 敌血+甲`。
- **输出 Plan**：`actions[(card_id, 实付费)], total, face_det(确定), face_exp(期望注记), lethal, uncovered_n(无伤害路径张数+卡表缺牌), mana_trace`——推理层只回结构，措辞归 render。

### 6.4 接入与输出

- **编排点**：`analysis.py` 新增 `lethal_plan(st, knowledge, analyzer) -> Plan | None`（analysis 是唯一效果解析层，piece 口径裁决在此）；planner 保持纯函数。
- **触发**：watcher 信息区批尾重算点（"变化才发"门延伸到 plan 字段）——我方 `turn_start` 及每次打出后批尾重算；对手回合不算。
- **输出通道**：扩展 `render.stat_fields` dict（`"plan": {...}` 子字段），`stat_text` 追加第三行 `可斩: A(2费)→B(0费)→… 伤24+场0 ≥ 22`，无斩时不追加（**粗估行一字不动**）；overlay 侧可斩线自上区移入中上部推荐区首行（2026-09-14 三区布局定版）。不新开 KIND_ADVICE 事件（T2 的 play_offer 才走那通道）。
- **开关与降级**：config `lethal_plan: true`；无卡表→piece 无伤害并诚实计 uncovered；无台账→只用现状手牌；`knowledge is None`（通用模式）→手牌线。

### 6.5 测试矩阵

| 层 | 测试 | 断言 |
|---|---|---|
| IR 扩展 | tests/test_effects* | 新措辞→SpellPower/cast_draw；COMPILER_VERSION 6 全量重编译 |
| pieces | 假卡文本构造(无网络) | 费/段/回费/减费/引擎位编译正确 |
| dfs 正确性 | 随机小状态 brute-force 交叉验证(全排列) | DFS 最优==暴力最优（剪枝不砍最优） |
| dfs 记忆化 | 同状态二次进入 | 结果一致、memo 命中 |
| 抽牌语义 | known_top 队列/期望折算 | 确定性可斩不含期望分量 |
| 时延 | 最坏构造局面 | <100ms |
| 接入 | tests/test_render_knowledge 同款 | `lethal_plan: false`→输出逐字节零变化；开→粗估行不变仅追加行 |
| 端到端 | `python -m hsbot replay <真实斩杀局切片>` | 可斩线与实际制胜操作一致（人工验收） |

### 6.6 实施切片（每片可运行可验收，严格串行）

1. **IR 扩展**：SpellPower 族 + cast_draw 标记 + COMPILER_VERSION 6 + 测试 —— 0.5 天
2. **pieces.py + simstate.py**：编译层与状态/转移 + 单测 —— 0.5 天
3. **dfs.py**：记忆化+剪枝 + brute-force 交叉验证 —— 0.5 天
4. **analysis.lethal_plan + watcher 接线 + config 开关** —— 0.5 天
5. **render 第三行 + overlay 加格 + 降级路径** —— 0.5 天
6. **真实切片对账 + 全量回归 + 文档回填** —— 0.25 天

合计 ≈2.5 人日。

### 6.7 既定近似（全部挂 M5 回测校准）

- `random_split`/AoE 段全计打脸（沿信息区既定粗估口径）；
- CostDown `next:*` 类目近似为"下一张任意牌"、`hand:法术` 不分细类；
- 未覆盖效果牌：费/回费/减费照常参与，伤害=0 计 `uncovered_n`——可斩**只可能漏报不可能误报（宁漏勿错）**；
- 过载不建模（本回合内无影响；T3 预备模式才需要）；
- 无目标选择：伤害默认全打脸。

### 6.8 验收清单（2026-09-13 T1 落地验收）

- [x] 构造局面最优：planner 27 测含 1200 组随机状态 brute-force 全排列交叉验证零失配（强于人工验证）
- [x] 真实斩杀局切片回放：g02 中局可斩线随实际出牌逐步收敛（`26+4≥30`→`8+6≥12`）、g04 破甲局 `40+场8≥48` 恰好击穿 48 血甲池，均与制胜操作一致；g10 属期望致胜局，按"宁漏勿错"设计不出确定性线（粗估与 DFS 线并存口径符合预期）
- [x] `lethal_plan: false` 零变化：render golden 逐字节断言（单测级）；e2e 整局开关对照未单跑
- [x] 降级路径：卡表缺→inert Piece、knowledge=None→手牌线、enabled=False→None 各有测试
- [x] 最坏局面 <100ms（10 异牌混合减费/回费/法强/引擎构造断言过）；"变化才发"门按文本比对不受 plan 字段影响

T1 落地：`planner/`（pieces/simstate/dfs/plan，纯记忆化+平手取短，engines 循环抽牌）、
`analysis.lethal_plan`（facts dict）、watcher 信息区批尾接线、`render.plan_line` 第三行+
overlay 面板行、config `lethal_plan`(默认开)。

2026-09-14 审计修正波（细节见 AUDIT_2026-09-14 与 §6.3 更新）：
- **engines 接线补全**：`lethal_plan` 从场面统计我方 cast_draw 随从数传入
  `initial_state(engines=…)`（审计发现该通道曾整体未接线，引擎线不可达）；
- **board_atk 合法性**：`board_face_attack`（嘲讽→0、冻结/已攻击/召唤失调剔除）；
- **disc_next 面值语义、AoE scope 消费、临时法强守卫、顶牌双计修正**（§6.3）；
- COMPILER_VERSION 7→8（语法规则序+守卫变更，缓存全量重编译）。
测试 187→207。

2026-09-14 晚二次修复波（用户实测 g12-g17 反馈，systematic-debugging 定根因）：
- **一次性减费类目化+0 水晶不消耗**（假可斩根修）：`next:X` 类目词入 Piece
  （星灵=卡表 set=SPACE），原费已 0 不作用不消耗、类目不符保留余额——g12
  敌 27 血"可斩伤 30"实为虚减费线（诚实 25），11 个假可斩快照清零、零反向翻转；
- `predict_damage` 补 scaled 门（裸数字固定伤不吃法强，与 burst 对齐）；
- 法强值链审计：store.spellpower(CURRENT_SPELLPOWER_BASE) 与 6 局实况逐一核对
  无误，法伤"有误"实感主因即上述虚减费线；
- 回费格去虚（"0 水晶不需要"显示侧）与 facts 补 mana_trace、推荐区常驻
  （可斩/最优两级+数据行）见对应提交。
测试 283（本波前）→307（含 T2/T3）。

遗留（deferred minors）：exp_per_draw 用当前法强口径待注释固化、`_carddb` 活对象进机读
dict 的序列化负债、cast_draw 触发句被 Draw 族误标一条 Draw(1)（T1 不消费，无害）。

## 7. T2 落地状态（2026-09-14 骨架落地）

### 7.1 已落地（按 §1-T2 规格逐条）

- **`hsbot/play_ai.py`**（与 `mulligan_ai.py` 完全同款模式）：
  `models_root_for(data_dir, deck)` → `data/models/play/<卡组>/`；
  `read_latest()` 读 `LATEST.json` → 版本目录的 `value.pkl` + `value_meta.json`，
  按 LATEST mtime 热更新（`PlayAdvisor._refresh`，先例 = 留牌 advisor）；
  无产物/坏产物 → warning 一条 + 静默降级（advise 恒 None，只出 T1 斩杀线）。
- **打分**：候选 = `candidate_actions`（深度 1：每张可支付手牌[实付费 =
  COST 标签优先] + 关键二连"法强牌→法术"，法强在前）；节点 = `apply_candidate`
  对 `trainer/states.flatten` 基线快照做**特征差分**（费−、手牌摘牌、随从
  入场攻血+嘲讽、法强+、抽牌期望=牌库−n 并以匿名牌入手[按余牌封顶]），
  不模拟 GameStore；`score = P(胜)`（模型对象来自 trainer 产物，flatten 布局
  单点复用训练侧）；`delta_p = p − baseline("不动")`；平分取更省；top3。
- **责任链接线**（加环不加分发）：store 发 `play_offer`（我方 `turn_start`
  出主建议；每次我方打出后块尾重发——复用 `_pending_play` 扣留/批尾冲刷，
  抽牌/回费事件到齐后才重算；对手回合静默）→ `analysis.enrich` 挂 advisor
  富化（同 mulligan"只富化不改事件事实"，历史追平 live=False 不推理）→
  watcher 路由 `play_offer`→KIND_ADVICE 金色通道（仅我方，同留牌"金色专属
  我方建议"口径），`Msg.data` = 建议机读字段 → `render` 加
  `@chain_renderer("play_offer")`（无 advice 判弃=整环显示静默）→ overlay
  推荐区按 `data.kind` 分流入 `_AdvicePanel.set_play`。
- **措辞归 render**：`play_offer_text` = `推荐: 名A(ΔP +x.x%) > 名B(ΔP
  +x.x%) > 不动`（二连名按出牌序 `→` 相连；负 ΔP 照实显示）；
  `play_offer_rows` = 主行(advice 色) + 证据行(dim：`语料N局 · 依据 价值模型
  vXXX`)。控制台 stat_text 两行版逐字节零变化（play_offer 不进 stat_text）。
- **overlay 优先级定版**：`_AdvicePanel` 四行源三行封顶，
  **可斩/最优线行 > play_offer 主行 > 留牌行 > 数据行**（play 证据行与 plan
  数据行同为 dim 支撑行，恒殿后让位）。依据：线行是确定性引擎（可证明、
  当拍可执行）必须居首；play_offer 是本回合的统计最优，时效以回合为限，
  优先于早已过期的开局留牌行（留牌只服务 T1，之后是陈旧事实）；支撑小字
  恒殿后。恰好 top3 = 线行/play 主行/留牌主行。值驻留与局终 reset 语义不变。
- **开口门槛**：`PLAY_N_GAMES_MIN = 300`（依据见 §1-T2 原文与模块 docstring；
  统计功效教训 = 留牌 v3 评审"门控不过就退回低风险输出"）。门槛数据源 =
  产物 `value_meta.json` 的 `n_games`（trainer/value.py 训练时写入，随语料
  自动伸缩，达标自动开口，无需配置；缺键按 0 计=诚实静默）。
- **开关**：config `play_ai`（默认开，同 `lethal_plan` 模式）；false →
  watcher 不建建议器，整条环静默。
- **验收**（tests/test_play_ai.py，24 测；全套 283→307 绿）：候选枚举
  （可支付/超费剔除/二连构成/抽牌量含引擎/cast_draw 触发句误标 Draw 剔除）；
  特征差分向量逐特征断言（费/手牌/场面/法强/牌库/期望抽封顶）；score 排序
  （桩模型）；门槛（83 局静默/299 静默/300 开口/缺 n_games 静默）；无模型/
  坏 pickle 降级；非我方回合静默；mtime 热更新（打分与门槛同走）；store
  发环时机（turn_start 后/打出块尾且在块内抽牌之后/对手回合与对手打出
  静默）；enrich 门控（仅我方+live）；render 措辞/负值/无建议判弃；overlay
  优先级与整窗分流；**整局回放逐字节零变化钉子**（play_ai=false 且达标
  模型在位 == 无模型基线；同模型开开关出"推荐"行=非空转对照）；**回放切片
  建议时刻对齐**（回合标题 < 建议行 < 实际打出，全程恰一条）。

### 7.2 还差（deferred）

- **trainer 产物落点未接线**：`python -m trainer train` 现产物在
  `trainer/data/<卡组>/`（无 LATEST.json 版本化），尚未指向
  `data/models/play/<卡组>/`——产物形态已兼容（value.pkl + value_meta.json
  + n_games），接线后 advisor mtime 热更新即自动生效；当前实跑语料 83 局
  < 300，即便在位也按门槛静默（设计如此，宁缺毋滥）。
- **二连仅"法强牌→法术"**：回费/减费链式二连、英雄技能候选、场面随从
  交换与攻击目标选择未入候选（骨架口径，留给更深搜索/后续迭代）。
- **抽牌期望=匿名牌**：flatten 聚合特征不含卡 ID，具名抽牌（台账已知顶牌）
  无法进差分向量——ID 特征版（语料上千）自然解决。
- **§3 数据闭环未建**：建议 vs 实际 `play` 的回测对账/偏差样本回流，等
  语料达 300 局、建议真实开口后才有数据可回。
- **value 训练侧 AUC 门控未挂**：advisor 只设语料局数门槛；AUC 门控
  （同 LR_AUC_GATE 思想）待 T4 换基座时一并定版。
