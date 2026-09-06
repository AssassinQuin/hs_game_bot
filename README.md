# hs_game_bot

炉石传说(Hearthstone)对局日志分析与机器人项目。

## 核心依赖

- [hslog](https://github.com/HearthSim/hslog):解析 `Power.log` 为对局对象树(hsreplay.net 同款解析器)
- [hearthstone](https://github.com/HearthSim/python-hearthstone):卡牌数据库与游戏枚举(Zone / CardType / GameTag 等)

```bash
pip install -r requirements.txt
```

## 日志文件位置

游戏端日志默认在:

```
C:\Program Files (x86)\Hearthstone\Logs\hub-<版本号>\<对局时间>\Power.log
```

## 使用流程(hslog)

1. 按 `GameState.DebugPrintPower() - CREATE_GAME` 把日志切成"每局一段";
2. 每段用独立的 `LogParser` 解析(PlayerManager 全局共享,同一玩家多局间 PlayerID 可能不同,混解析会抛 `InconsistentPlayerIdError`);
3. `EntityTreeExporter` 把数据包树重建为实体树;
4. 从实体树读胜负 / 英雄 / 场面 / 手牌 / 牌库等。

示例(使用 `examples/Power.log` 真实日志):

```bash
python examples/parse_demo.py          # 基础:逐局解析,输出双方概况
python examples/full_game_info.py      # 进阶:最新一局的全部关键信息
python examples/realtime_demo.py --replay   # 实时:回放日志模拟实时监听
python examples/realtime_demo.py       # 实时:tail 正在进行的游戏日志
```

## full_game_info.py —— 最新一局的完整关键信息

- 对局元信息:模式(天梯/休闲/战棋)、标准/狂野、客户端 BuildNumber、对局时长
- 玩家:战网名、是否本机玩家(FriendlyPlayerExporter 识别)、先手、胜负
- 英雄:英雄卡、血量、护甲、英雄技能、区域(阵亡进 GRAVEYARD)
- 牌张:手牌列表(未揭示的对手牌显示占位)、牌库张数、场面随从(攻/血/关键词)、武器、奥秘
- 过程统计:出牌序列(回合/玩家/卡牌)、攻击事件、起手调度
- 数据来源:遍历 `packet_tree` 里的 `packets.Block`(BlockType.PLAY / ATTACK),官方测试同款用法

## realtime_demo.py —— 实时对战信息设计

```
PowerWatcher ──行/心跳──▶ LiveTracker ──节流重建──▶ snapshot + diff = 事件
(tail 文件追加)         (每局一个解析器)        (回合/血量/场面/胜负)
```

设计要点(踩坑总结):

1. **心跳驱动**:`follow()` 空闲时产出 `None` 心跳,主循环照样刷新快照——
   否则终局数据进了解析器却没有"下一行"来触发重建,`game_over` 永远不触发;
2. **每局独立解析器**:检测 `CREATE_GAME` 即换新 `LogParser`,规避
   `InconsistentPlayerIdError`(玩家 PlayerID 跨局会变);
3. **节流重建**:实体树重建有开销,按时间节流(默认 0.5s);重建失败沿用旧快照,绝不中断;
4. **半行处理**:客户端写日志是流式的,读到半行就回退重读;
5. **事件驱动决策**:`diff_events()` 产出 game_start / turn_change /
   hero_hp_change / board_change / game_over,机器人决策挂在这里。

## 常用对象速查

| 对象 | 获取方式 | 说明 |
| --- | --- | --- |
| `parser.games[i]` | 数据包树 PacketTree | 传给 `EntityTreeExporter` |
| `exporter.game` | 实体树 Game | `.players` / `.entities` / `.tags[GameTag.TURN]` |
| 玩家名 | `parser.player_manager.get_player_by_entity_id(pid).name` | 日志匿名时可能为 None |
| 本机玩家 | `FriendlyPlayerExporter(packet_tree).export()` | 返回 player_id(不是实体ID) |
| 实体控制者 | `entity.controller.id` | 返回 Player 对象,取 `.id` 才是数字 |
| 攻击力 | `entity.tags[GameTag.ATK]` | 是 `ATK`,不是 ATTACK |
| 卡牌名 | 需要 CardDefs(卡牌数据库) | 仅日志只有 cardId(如 `CS2_042`) |
