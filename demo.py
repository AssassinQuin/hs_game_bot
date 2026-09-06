from hsreplay.document import HSReplayDocument
from pathlib import Path

log_path = Path('examples') / 'Power.log'

# 将 Path 对象转为字符串
replay = HSReplayDocument.from_log_file(str(log_path))

# 或者直接使用字符串路径
# replay = HSReplayDocument.from_log_file('examples/Power.log')

if replay.games:
    last_game = replay.games[-1]
    print(f"游戏ID: {last_game.game_id}")
    print(f"当前回合: {last_game.tags.get('TURN')}")
    
    for entity in last_game.entities:
        print(f"实体ID: {entity.id}, 卡牌: {entity.card_id}, 标签: {entity.tags}")
else:
    print("日志中未找到游戏数据")