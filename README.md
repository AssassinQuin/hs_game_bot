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
python examples/parse_demo.py
```

输出每局:玩家、胜负、英雄与血量、场面随从(攻击/血量)、手牌与牌库张数、回合数。

## 常用对象速查

| 对象 | 获取方式 | 说明 |
| --- | --- | --- |
| `parser.games[i]` | 数据包树 PacketTree | 传给 `EntityTreeExporter` |
| `exporter.game` | 实体树 Game | `.players` / `.entities` / `.tags[GameTag.TURN]` |
| 玩家名 | `parser.player_manager.get_player_by_entity_id(pid).name` | 日志匿名时可能为 None |
| 实体控制者 | `entity.controller.id` | 返回 Player 对象,取 `.id` 才是数字 |
| 攻击力 | `entity.tags[GameTag.ATK]` | 是 `ATK`,不是 ATTACK |
| 卡牌名 | 需要 CardDefs(卡牌数据库) | 仅日志只有 cardId(如 `CS2_042`) |
