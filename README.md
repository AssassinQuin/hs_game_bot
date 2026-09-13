# hs_game_bot

炉石传说「奇迹德」实时军师 —— **只读**监听游戏日志，实时输出出牌链路、回合快照与卡组台账。

不注入任何操作、不自动出牌；真人操控，程序只看 `Power.log` 说话。

> 当前进度：**M1 监控层已完成**并通过回放验收；知识层（组件台账 / 牌库序）与卡牌字典已落地。
> 规划建议（DFS / 蒙特卡洛）与离线回测尚未开工，排期见 [docs/PROJECT.md](docs/PROJECT.md) §4。

## 核心依赖

- [hslog](https://github.com/HearthSim/hslog)：把 `Power.log` 解析为对局数据包树（hsreplay.net 同款解析器）
- [hearthstone](https://github.com/HearthSim/python-hearthstone)：卡牌枚举与 deckstrings 解码
- [PyYAML](https://pyyaml.org/)：配置文件
- tkinter（标准库）：半透明置顶日志窗；不可用时自动降级为纯控制台输出

```bash
pip install -r requirements.txt        # Python 3.10+（开发环境 3.11）
```

**卡牌字典（可选，建议配置）**：`python scripts/fetch_cards.py` 一键下载 HearthstoneJSON
中文**全量**卡表（`cards.json`，含附魔/衍生物/英雄技能等不可收藏卡）并精简后存到
`data/cache/cards.zh.json`。注意别手动下 `cards.collectible.json` 顶替——那是可收藏子集，
附魔/token 不在内，触发与衍生实体会显示原始 card_id。缺失时程序照常运行，只是卡牌名
显示为原始 card_id（如 `CS2_042`）。

## 快速开始

配置一律走 [config.yaml](config.yaml)（优先级：子命令注入 > config.yaml > 内置默认），
至少确认 `battletag`（本机战网名，友方判定依据）与 `logs_dir`（Hearthstone Logs 根目录，留空则按平台自动探测）。

```bash
启动hsbot.bat                                    # 一键启动(双击即可; 停止=关窗口)
python -m hsbot                                  # 实时监控：自动定位最新会话目录，控制台 + 悬浮窗
python -m hsbot replay examples/Power.log        # 重放静态日志（开发/验收用，自动关悬浮窗）
python -m hsbot import-all                       # 批量解析 logs_dir 全部历史会话 → 训练语料
python scripts/train_mulligan.py                 # 留牌AI: 训练+报告(见 docs/MULLIGAN_AI.md)
python scripts/train_mulligan.py advise --vs 圣骑士 --coin --hand 水栖形态,黑市拍卖师
python -m hsbot --config my.yaml replay x.log    # 任意子命令均可 --config 指定配置文件
```

不开游戏就能跑：`replay` 把静态日志当作实时流喂完整条管线，M1 验收六条全部可在样本日志上核对。

### 运行时行为

- **会话发现**：按目录名字典序选最新 `Hearthstone_*` 会话目录（NTFS 目录 mtime 不可靠）；
  bot 先于游戏启动时直接挂上等待 `Power.log` 出现，每 10 秒检查新会话并自动切换。
- **卡组识别**：常驻监听 `Decks.log`，出现 `Finding Game With Deck: ### 所选卡组` 后，
  下一局按 deck code 生成 30 张多重集（缓存到 `data/decks/decklist.json`）。
  实际对局不是所选卡组时进入**通用模式**：只输出快照，不输出台账，快照头部标注。
- **友方判定**：战网名匹配是首选（跨局稳定的硬事实）；hslog 的 FriendlyPlayer 启发式会翻转，仅作兜底。
- **诊断日志**：`data/logs/hsbot.log` 收 DEBUG 全量；控制台只出 WARNING+，不干扰对局输出。

## 输出形态

所有输出经 OutputHub 三路分发：控制台、悬浮窗、会话记录文件。示例（真实渲染格式）：

```text
[T4·我] 打出 滋养 (5费→实付3费) 剩2费
[T4·我] 抽到 野性成长
[T5·对面] 攻击 野兽 → 我的英雄
── T5 第2局(回合结束) 我:水晶7/10 │ 手牌6 │ 牌库18 │ 场上2 │ 对面场上1 ──
════════ 完整快照 ════════ 回合T5(我的第3回合·回合结束)
  …… 双方手牌/场面/英雄状态 + 组件台账(差1张组件还剩几抽) + 本回合出牌链路 ……
──── 对局结束 ──── 湫然#51704=WON / 对手=LOST
```

- **链路行**：回合内实时打出 抽牌/出牌（含抉择与实付费用）/攻击/触发/阵亡/疲劳等事件；
- **单行快照**：回合结束刷一行节奏信息（悬浮窗默认视图）；
- **完整快照**：回合结束与终局输出完整块（双方手牌/场面/牌库/台账），同步落盘 `data/sessions/<会话>/game_NNN.jsonl`；
- **悬浮窗**：tkinter 半透明置顶日志窗（炉石需用无边框/窗口化显示模式），关闭窗口即退出监控。

## 留牌 AI（离线）

`scripts/train_mulligan.py` 从训练语料学习「按对手职业 × 先/后手」的起手留牌：
平滑统计表给逐卡留/换增益，专家先验文件（`data/mulligan_prior.yaml`，人工维护）
兜底无数据卡，可选逻辑回归（sklearn）做组合修正与时序验证；建议在 2^n 个候选
留牌集合上枚举取最优，`advise --explore` 用 Thompson 采样生成探索局，逐卡附
"同情境匹配"证据。产物按版本落 `data/models/mulligan/<卡组>/`。打几局 → 重跑
一次 `train` 即增量吸收新对局。设计、偏差说明与参数见
[docs/MULLIGAN_AI.md](docs/MULLIGAN_AI.md)。

## 配置项（config.yaml）

| 配置 | 默认 | 说明 |
|---|---|---|
| `deck_name` / `deck_code` | 奇迹德 / 空 | 所选卡组；`deck_code` 直接给代码时优先于 Decks.log 匹配 |
| `battletag` | — | 本机战网名（如 `湫然#51704`），友方判定首选依据 |
| `logs_dir` | 空=自动探测 | Hearthstone Logs 根目录（会话目录的父目录） |
| `poll_interval` / `session_check_interval` | 0.5 / 10 秒 | 日志轮询与新会话检查间隔 |
| `overlay_enabled` / `alpha` / `geometry` / `font_size` / `borderless` | 见 config.yaml | 悬浮窗开关与外观；`borderless: true` 后不可拖动，只能改配置还原 |
| `console_echo` | true | 悬浮窗之外是否同步打印控制台 |
| `auto_training` / `training_dir` | true / data/training | 每局结束自动导出训练语料（`import-all` 可批量回填，增量去重） |

完整注释版见 [config.yaml](config.yaml)；字段语义与运行细节见 [docs/M1_MONITOR.md](docs/M1_MONITOR.md) §1。

## 目录布局

```text
hsbot/                    主包
├─ main.py                入口: python -m hsbot
├─ config.py              配置加载 + 跨平台日志目录探测
├─ watcher.py             监听层: tail / 会话切换 / 每局 FSM / 事件派发
├─ adapter.py             hslog 数据包树 → 状态层输入（全项目唯一 import hslog 的模块）
├─ store.py               GameStore: 每局一个的对局状态机（水晶/手牌/场面/牌库/胜负）
├─ knowledge.py           组件台账 + 牌库序视图; Decks.log 解析
├─ carddb.py              cards.zh.json 只读缓存（缺失时优雅降级显示 card_id）
├─ render.py              链路行 / 快照块 / 终局行渲染
├─ overlay.py             OutputHub 三路输出 + tkinter 悬浮窗
├─ persist.py             sessions jsonl 落盘
└─ corpus.py              训练语料归一化（packet → JSONL, 按卡组分目录）
data/                     运行时生成（sessions/cache/decks/logs 均不入库）
examples/                 解析教学样本与 Power.log 回归素材
scripts/replay_capture.py 回放输出落盘, 供新旧版本 diff 验收
tests/                    pytest 测试套（fixtures 内置样本日志）
```

两条架构铁律（详见 [docs/PROJECT.md](docs/PROJECT.md) §3）：`adapter.py` 之外任何模块不得 import hslog/hearthstone 实体类；
规划器是纯函数，绝不反向调用渲染/持久化。

## 测试

```bash
python -m pytest
```

覆盖：store 状态机、watcher 集成、渲染与台账、OutputHub、CLI、内置 fixtures 样本日志解析。
升级 hslog 版本后跑一遍即可发现解析兼容性问题（`replay examples/Power.log` 同理）。

## 文档

- [docs/DESIGN.md](docs/DESIGN.md) —— 总设计：目标/非目标、分层架构、假设表
- [docs/M1_MONITOR.md](docs/M1_MONITOR.md) —— 监控层施工图：日志模式清单、主循环、验收标准
- [docs/MULLIGAN_AI.md](docs/MULLIGAN_AI.md) —— 留牌 AI：样本提取、双模型设计、偏差与增量策略
- [docs/PROJECT.md](docs/PROJECT.md) —— 项目蓝图：目录规划、里程碑 M1~M5、Git 策略
