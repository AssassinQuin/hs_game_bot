"""实时对战信息 demo —— tail Power.log,边打游戏边输出局面

设计(三层管道):

  PowerWatcher ──行/心跳──▶ LiveTracker ──节流重建──▶ snapshot + diff = 事件
  (tail 文件追加)         (每局一个解析器)         (回合/英雄血量/场面/胜负)

关键设计决策:
  1. Power.log 是游戏客户端实时追加写入的,用"tail -f"式轮询读取新行,
     半行(正在写入)回退重读;
  2. ★ 心跳驱动,事件只喂解析器,状态刷新靠节拍:空闲(EOF)时 follow()
     产出 None 心跳,主循环照样调 snapshot() —— 否则终局的 WON/LOST 已进
     解析器却永远等不到一次重建,game_over 事件就丢了(真实跑出来的教训);
  3. 检测到 CREATE_GAME 就换全新的 LogParser —— 多局共用会因玩家 PlayerID
     跨局变化而崩(InconsistentPlayerIdError,见 parse_demo.py 说明);
  4. 重建实体树有开销,按时间节流(默认 0.5s);重建失败(块未闭合等)沿用
     上一份快照,绝不中断监听;
  5. 用 FriendlyPlayerExporter 识别"我"是哪一方,快照按"我/对手"视角输出;
  6. 事件 = 相邻两次快照的 diff:game_start / turn_change / hero_hp_change /
     board_change / game_over。机器人决策逻辑挂在 diff_events 的回调上。

运行:
  python examples/realtime_demo.py --replay   # 回放 examples/Power.log 模拟实时
  python examples/realtime_demo.py            # tail 真实游戏日志(需要游戏在跑)
  python examples/realtime_demo.py --path 某个/Power.log
"""
import argparse
import os
import time

from hearthstone.enums import CardType, GameTag, PlayState, Zone
from hslog import LogParser
from hslog.export import EntityTreeExporter, FriendlyPlayerExporter

CREATE_GAME_MARK = "GameState.DebugPrintPower() - CREATE_GAME"
HEARTHSTONE_LOG_DIRS = [
    r"C:\Program Files (x86)\Hearthstone\Logs",
    r"D:\Program Files (x86)\Hearthstone\Logs",
    r"C:\Program Files\Hearthstone\Logs",
]
POLL_INTERVAL = 0.2        # 文件轮询间隔(秒)
SNAPSHOT_INTERVAL = 0.5    # 实体树重建节流(秒)


# ---------------------------------------------------------------- 第一层:tail 文件
class PowerWatcher:
    """tail -f 式日志监听:产出新追加的完整行;空闲时产出 None 心跳。"""

    def __init__(self, path, from_end=False):
        self.path = path
        self.from_end = from_end  # True=只看新写入;False=从头回放

    def follow(self):
        with open(self.path, encoding="utf-8", errors="replace") as fp:
            fp.seek(0, os.SEEK_END) if self.from_end else fp.seek(0)
            while True:
                pos = fp.tell()
                line = fp.readline()
                if line.endswith("\n"):
                    yield line
                elif line:  # 半行:客户端正在写,回退等它写完
                    fp.seek(pos)
                    time.sleep(POLL_INTERVAL)
                else:       # 没有新内容:心跳节拍,驱动下游刷新快照
                    time.sleep(POLL_INTERVAL)
                    yield None


def find_latest_power_log():
    """在常见安装目录里找最近修改的 Power.log。"""
    candidates = []
    for root in HEARTHSTONE_LOG_DIRS:
        for dirpath, _dirs, files in os.walk(root):
            if "Power.log" in files:
                p = os.path.join(dirpath, "Power.log")
                candidates.append((os.path.getmtime(p), p))
    if not candidates:
        raise SystemExit("没找到 Power.log,用 --path 指定,或检查 HEARTHSTONE_LOG_DIRS")
    return max(candidates)[1]


# ---------------------------------------------------------------- 第二层:增量解析
class LiveTracker:
    """持有"当前局"的解析器,产出节流后的局面快照。"""

    def __init__(self, snapshot_interval=SNAPSHOT_INTERVAL):
        self.interval = snapshot_interval
        self.game_no = 0
        self.parser = LogParser()
        self.friendly_pid = None
        self._snapshot = None
        self._built_at = 0.0

    def _reset(self):
        """新对局:换全新解析器(PlayerManager 也随之独立)。"""
        self.game_no += 1
        self.parser = LogParser()
        self.friendly_pid = None
        self._snapshot = None
        self._built_at = 0.0

    def feed(self, line):
        if CREATE_GAME_MARK in line:
            if self.parser.games:  # 旧局有内容才播报归档(首个 CREATE_GAME 不算)
                print(f"\n######## 第 {self.game_no} 局已归档 ########", flush=True)
            self._reset()
        try:
            self.parser.read_line(line)
        except Exception:  # noqa: BLE001 单行脏数据直接丢
            pass

    def snapshot(self, force=False):
        """当前局面快照;按 interval 节流重建,失败沿用旧快照。"""
        if not self.parser.games:
            return None
        now = time.monotonic()
        if not force and now - self._built_at < self.interval:
            return self._snapshot
        try:
            pt = self.parser.games[-1]
            if self.friendly_pid is None:
                self.friendly_pid = FriendlyPlayerExporter(pt).export()
            exporter = EntityTreeExporter(
                pt, player_manager=self.parser.player_manager,
                tolerate_missing_entities=True).export()
            self._snapshot = self._build_snapshot(exporter.game)
            self._built_at = now
        except Exception:  # noqa: BLE001 中途状态(块未闭合等)沿用旧快照
            pass
        return self._snapshot

    def _build_snapshot(self, game):
        snap = {"turn": game.tags.get(GameTag.TURN, 0), "players": {}}
        for p in game.players:
            pid_tag = p.tags.get(GameTag.PLAYER_ID)
            hero = p.hero or next((e for e in game.entities
                                   if getattr(e, "controller", None) is not None
                                   and e.controller.id == p.id
                                   and e.tags.get(GameTag.CARDTYPE) == CardType.HERO), None)
            board = [e for e in game.in_zone(Zone.PLAY)
                     if getattr(e, "controller", None) is not None
                     and e.controller.id == p.id
                     and e.tags.get(GameTag.CARDTYPE) == CardType.MINION]
            in_zone = lambda e, z: (getattr(e, "controller", None) is not None
                                    and e.controller.id == p.id and e.zone == z)
            snap["players"][pid_tag] = {
                "friendly": pid_tag == self.friendly_pid,
                "playstate": PlayState(p.tags.get(GameTag.PLAYSTATE, 0)).name,
                "hero": getattr(hero, "card_id", None),
                "hp": hero.tags.get(GameTag.HEALTH) if hero else None,
                "armor": hero.tags.get(GameTag.ARMOR, 0) if hero else None,
                "hand": sum(1 for e in game.entities if in_zone(e, Zone.HAND)),
                "deck": sum(1 for e in game.entities if in_zone(e, Zone.DECK)),
                "board": [(e.card_id, e.tags.get(GameTag.ATK, 0), e.tags.get(GameTag.HEALTH, 0))
                          for e in board],
            }
        return snap


# ---------------------------------------------------------------- 第三层:事件 = 快照 diff
def render_side(info):
    mark = "我" if info["friendly"] else "对手"
    board = " ".join(f"{cid}({a}/{h})" for cid, a, h in info["board"]) or "-"
    return (f"[{mark}] {info['hero']} 血{info['hp']}+{info['armor']}甲 "
            f"手{info['hand']} 牌库{info['deck']} 场面:{board}")


def diff_events(old, new):
    """相邻快照对比,产出事件列表 —— 机器人决策在这里挂回调。"""
    events = []
    if old is None:
        return ["game_start"] if new else []
    if new["turn"] != old["turn"]:
        events.append(f"turn_change -> {new['turn']}")
    for pid, now in new["players"].items():
        before = old["players"].get(pid)
        if before is None:
            continue
        label = "我" if now["friendly"] else "对手"
        if before["hp"] is not None and now["hp"] != before["hp"]:
            events.append(f"hero_hp_change [{label}] {before['hp']} -> {now['hp']}")
        if len(now["board"]) != len(before["board"]):
            events.append(f"board_change [{label}] {len(before['board'])} -> {len(now['board'])}")
        if (now["playstate"] in ("WON", "LOST", "TIED")
                and before["playstate"] not in ("WON", "LOST", "TIED")):
            events.append(f"game_over [{label}] {now['playstate']}")
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", help="Power.log 路径")
    ap.add_argument("--replay", action="store_true", help="回放已有日志,模拟实时追加")
    ap.add_argument("--tail", action="store_true", help="从文件末尾开始,只看新写入")
    args = ap.parse_args()

    if args.replay:
        path = args.path or "examples/Power.log"
        watcher = PowerWatcher(path, from_end=False)
    else:
        path = args.path or find_latest_power_log()
        watcher = PowerWatcher(path, from_end=args.tail)
        print(f"监听: {path}", flush=True)

    tracker = LiveTracker()
    last_print, last_snap = 0.0, None
    for line in watcher.follow():
        if line is not None:            # 新行:喂解析器
            tracker.feed(line)
        # 心跳/新行之后都刷新快照(节流在 snapshot 内部)
        snap = tracker.snapshot()
        if snap is None:
            continue
        events = diff_events(last_snap, snap)
        last_snap = snap

        now = time.monotonic()
        # 事件发生立刻打印;无事件也最多每 3 秒刷一次局面
        if events or now - last_print > 3.0:
            me = next((v for v in snap["players"].values() if v["friendly"]), None)
            opp = next((v for v in snap["players"].values() if not v["friendly"]), None)
            print(f"\n== 第{tracker.game_no}局 回合{snap['turn']} ==", flush=True)
            if me and opp:
                print(" " + render_side(me), flush=True)
                print(" " + render_side(opp), flush=True)
            for ev in events:
                print(f" ⚡ 事件: {ev}", flush=True)
            last_print = now

        if any(ev.startswith("game_over") for ev in events):
            break  # 回放模式:演示完一局就退出;真实监听可去掉这行


if __name__ == "__main__":
    main()
