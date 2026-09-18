# 可斩伤害口径修正设计 —— 法力可行链替代理论上限粗估

> 状态: 设计已获用户批准(2026-09-18, 三选一定版: 法力可行链 / 颜色亮橙 #ff9500)
> 关联: docs/superpowers/specs/2026-09-15-simsnapshot-unification-design.md §0.5 两通道总纲;
>       斩杀线接口契约 §2/§3(PLAY_ADVICE); docs/ARCHITECTURE.md §2 职责表

## 0. 问题: 理论上限虚高误报"可斩"(现状证据)

`render.stat_fields`(render.py:322) 的伤害格现状:

```
lethal = burst_hand + burst_deck + burst_board
```

- 全链路**无法力约束**——docstring 自认「理论上限粗估, 非出牌链搜索」;
- `burst_deck` 把台账剩余全部伤害牌都计入(还在牌库、本回合根本抽不到);
- `can_kill = lethal >= enemy_total` → 悬浮窗该格转金色"可斩"。

误报实例: 5 水晶 + 手里 25 费烧牌 + 牌库还有烧 → 照样显示"可斩"。

而**正确的数字已经在算**(`watcher._emit_stat` 批尾, 我方回合):
`analysis.lethal_plan → assemble_snapshot → planner.dfs.best_line`
是法力可行的真出牌链搜索(逐张付费含实时减费、已知抽牌队列、场上引擎抽牌),
`plan.total` 即"本回合实际可打出总伤"——但它目前只在恰好可斩时展示第三行/
推荐区, 主数字与判定仍用虚高粗估。

## 1. 口径定版(2026-09-18 用户三选一)

| 项 | 定版 |
|---|---|
| 主数字 | `plan.total`(法力可行链, 已含可打脸场攻与期望抽牌成分) |
| 判定 `can_kill` | **仅 plan 口径**: 复用 `plan.lethal`(planner 统一斩杀算式, 2026-09-17 审计口径; render 不重算, 避免双口径); `plan=None` 时恒 `False`(宁漏勿错——粗估不再触发可斩报警, 即本设计修的 bug) |
| 牌库剩余伤害 | 保留原计算(台账剩余×当前法强单卡期望), 降级为**"潜力"注记**, 不进主数字、不进判定 |
| fallback(plan=None) | 对手回合 / lethal_plan 开关关 / 无手牌 → 粗估原式 + **"理论"标注**, 不出可斩标记 |
| 可斩颜色 | 新色键 `lethal: "#ff9500"`(亮橙)。与 advice 金 #ffd700 分工: 金=建议看一眼, 橙=这回合能杀 |

## 2. 数据层改动(`render.stat_fields`)

**签名不变**(plan 透传, `watcher._emit_stat` 调用点零改动)。字段口径:

| 字段 | plan 存在(我方回合+开关开) | plan=None(fallback) |
|---|---|---|
| `lethal` | `plan.total` | 粗估原式 `burst_hand+burst_deck+burst_board` |
| `lethal_board` | `plan.board_atk`(可打脸场攻) | `st.board_attack(me)`(现状) |
| `lethal_hand` | `plan.total − plan.board_atk`(出牌链伤害, 含已知抽/引擎抽成分) | `burst_hand`(现状) |
| `lethal_deck` | 原计算保留(潜力注记) | 原计算保留 |
| `can_kill` | `plan.lethal` | 恒 `False` |
| `lethal_est`(新增) | `False` | `True`(供措辞区分"理论") |

## 3. 展示层

- `stat_text` line1:
  - plan 口径: `斩杀 {total}(场X+线Y{kill_mark})` —— 措辞对齐既有 `_lethal_line` 的"线 伤X+场Y"(Y 含抽牌链成分, 用"线"不用"手", 诚实);
  - fallback: `斩杀 理论{N}(手A+库B+场C)` —— "理论"二字明示口径, **无 kill_mark**;
  - 潜力注记进细节位(库{burst_deck}), 法强照旧并入细节。
- overlay `_StatPanel`:
  - lethal 格标签「理论伤害」→「**可斩伤害**」;
  - `can_kill` 时数值 `fg=colors["lethal"]`(新键, 默认 #ff9500), 否则 `stat` 色; 细节行同步新口径;
  - 兜底: 旧 config 无 `lethal` 键 → 回退 `stat` 色(向后兼容)。
- `_DEFAULT_COLORS`(overlay.py)与 config.yaml 注释各加一行 `lethal: "#ff9500"`。
- 控制台无彩色, 颜色只影响悬浮窗信息区。

## 4. 不变式(改代码前必读)

- `stat_fields` 仍是唯一事实来源: `stat_text` 与悬浮窗分格面板都从它渲染, 不做平行计算;
- 原「plan=None 时零变化」铁律**立法变更**为「理论标注 + can_kill=False」——这是口径修正的本体, 契约测试同步改钉, 不留隐式行为;
- planner 不 import store、无 IO; `assemble_snapshot`/`lethal_plan`/planner 全部**零改动**(纯消费侧改动);
- 措辞归 render; overlay 从机读字段渲染, 不从文本反推;
- `plan.total`/`plan.lethal` 字段口径 = 斩杀线接口契约 §2, 本设计不重定义。

## 5. 测试与验收

契约钉死(更新/新增于 stat_fields 契约测试):

1. plan 可斩 → `lethal == plan.total`、`can_kill == plan.lethal == True`、`lethal_est=False`;
2. **粗估 > 敌血但 plan 不可斩 → `can_kill=False`**(虚高修复的回归钉);
3. plan=None → `lethal_est=True`、`can_kill=False`、stat_text 含"理论"且无",可斩";
4. overlay lethal 色键渲染 + 缺键回退 stat 色;
5. 既有 stat_fields/overlay 契约测试按新口径同步(逐条改钉, 不删覆盖)。

验收: `python -m pytest` 全绿(基线 376 passed, 2026-09-17); `replay examples/Power.log` 输出人工核对可斩格与理论 fallback; live 一局我方回合目视悬浮窗。

## 6. 非目标(明确不做)

- 不接 `mc_plan` 到 stat 区(牌库采样分布受性能墙约束, 归 P2 谓词重设计一并考虑);
- 不动 pipeline #1(状态清空收口)/#4(逐行故障隔离)——独立小任务, 不混入本次(R3 外科手术);
- 不改 `lethal_plan`/`assemble_snapshot`/planner 任何代码。
