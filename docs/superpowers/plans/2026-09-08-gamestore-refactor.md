# GameStore 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用库驱动的每局唯一状态仓 GameStore 替换 Watcher 散装状态与全量快照导出:packet 直投、链路事件由状态迁移衍生、补全 TRIGGER/疲劳/死亡事件。

**Architecture:** hslog LogParser 只做行→packet;adapter 新增 `StoreExporter`(EntityTreeExporter 容错子类 + 挂钩回调)把每个 packet 应用到 hearthstone.entities 实体树;`GameStore` 通过挂钩接收状态迁移、衍生链路事件、提供查询/导出;watcher 整体重写为纯采集外壳;gamestate.py 删除。

**Tech Stack:** Python 3.11(miniconda3)、hslog>=1.20、hearthstone、pytest 8.x。

**Spec:** `docs/superpowers/specs/2026-09-07-gamestore-design.md`(v3)。本计划把 §3.2 变更入口细化为 `apply(p, depth)` + `settle()` + `hint_draw` + `note_friendly`(等价于 spec 的 apply_block_end),Task 8 回写 spec。

## Global Constraints

- **不造轮子**:实体/标签/区域状态只由 hslog exporter 系 `handle_*` 维护;项目内不得出现第二份实体标签状态。
- **铁律**:`hslog.*` 的一切 import 只允许出现在 adapter.py;`hearthstone.enums`/`hearthstone.entities` 全项目可用。
- **事件形状兼容**:链路事件 dict 的 kind/字段与现版 `_chain_event` 载荷一致;新增 kind:`trigger`/`fatigue`/`death`/`turn_start`/`game_end`。
- **JSONL 字段级兼容**:`store.to_dict()` 输出键结构与现 `GameState.to_dict()` 完全一致。
- **demo 推倒重来**:不做兼容补丁;watcher 重写而非增量修改。
- **单线程**:store 只被 watcher 轮询线程触碰;查询返回活引用,消费方只读。
- 每任务以 `python3 -m pytest tests/ -q` 全绿 + git commit 结束(仓库根执行)。

## 已核实的关键事实(实现者必读)

1. **本机 hslog 1.18.0 的 `EntityTreeExporter.__init__(self, packet_tree, player_manager=None)` 不接受 `tolerate_missing_entities`**,而现 adapter.py:102 传了它 → 本机旧快照路径每次 TypeError 被吞、返回 None。requirements.txt 要求 `hslog>=1.20`。Task 0 先升级;**升级失败则停下报告,不得继续**(旧代码基线会失真)。
2. `BaseExporter.export_packet(p)` 公开,按类型 dispatch 到 `handle_*`;`handle_create_game` 创建 `self.game` 并注册玩家——**逐包驱动可行,不用 export() 全量**。
3. `handle_hide_entity` 只调 `entity.hide()`(revealed=False),**不落 ZONE 标签**——库缺口,store 挂钩必须补 `entity.tags[ZONE]=packet.zone`(现 watcher 的 HideEntity 分支干的就是这事)。
4. hslog 包构造器:`TagChange(ts, entity, tag, value)`、`FullEntity(ts, entity, card_id)`(tags 是 `[(GameTag, int)]` 列表,构造后赋值)、`ShowEntity(ts, entity, card_id)`、`HideEntity(ts, entity, zone)`、`Block(ts, entity, type, index, effectid, effectindex, target, suboption, trigger_keyword)`(9 参数,`ended` 由 `.end()` 设置)、`Choices(ts, entity, id, tasklist, type, min, max)`(`.choices` 列表)、`SendChoices(ts, id, type)`(`.choices`)、`ChosenEntities(ts, entity, id)`(`.choices`)、`CreateGame(ts, entity)`(`.players`/`.tags`)、`ShuffleDeck(ts, player_id)`。
5. 行格式(`LogParser.read_line` 只吃 `GameState.*` 流;`PowerTaskList.*` 行被静默忽略):`D HH:MM:SS.fffffff GameState.DebugPrintPower() - <data>`;data 形如 `CREATE_GAME` / `GameEntity EntityID=1` / `Player EntityID=2 PlayerID=1 GameAccountId=[hi=2 lo=1]`(无 Name 后缀,正则以 `$` 结尾) / `FULL_ENTITY - Creating ID=64 CardID=CS2_029` + 缩进 `tag=ZONE value=HAND` 子行 / `TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1` / `SHOW_ENTITY - Updating [entityName= UNKNOWN ENTITY [cardType=INVALID] id=63 zone=DECK zonePos=0 cardId= player=1] CardID=CS2_029` / `BLOCK_START BlockType=PLAY Entity=64 EffectCardId= EffectIndex=-1 Target=0` / `BLOCK_END`。
6. `BlockType` 含 `TRIGGER/DEATHS/PLAY/FATIGUE/ATTACK`;hearthstone.entities 的 `Game.current_player/first_player`、`Card.zone/controller/type` 可直接用。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `hsbot/store.py` | 新增 | GameStore:挂钩衍生事件 + 自有字段(留牌/发现/PLAY延迟/回合计数)+ 查询 + to_dict |
| `hsbot/adapter.py` | 修改 | 新增 StoreExporter/is_play_block/game_meta;删 export_game_state(Task 6) |
| `hsbot/watcher.py` | 重写 | tail/游标/FSM/行扫描喂 hint/输出管线/快照触发 |
| `hsbot/gamestate.py` | 删除(Task 6) | 自研平行模型退役 |
| `hsbot/render.py` | 修改 | GameState→GameStore 换源,输出格式不变 |
| `hsbot/knowledge.py` | 修改 | `rebuild(store)` |
| `tests/conftest.py`、`tests/test_*.py`、`tests/fixtures/*.log`、`scripts/replay_capture.py` | 新增 | 测试与验收 |

---

### Task 0: 环境修复(hslog 升级)

**Files:** 无代码改动。

- [ ] **Step 1: 升级 hslog 满足 requirements.txt**

```bash
python3 -m pip install -U "hslog>=1.20"
```

- [ ] **Step 2: 验证 tolerate_missing_entities 已可用(旧代码快照路径的前提)**

```bash
python3 -c "
from hslog import packets
from hslog.player import PlayerManager
from hslog.export import EntityTreeExporter
import inspect, hslog
print('version:', hslog.__version__)
print('sig:', inspect.signature(EntityTreeExporter.__init__))
e = EntityTreeExporter(packets.PacketTree('2026-09-08 20:00:00'),
                       player_manager=PlayerManager(),
                       tolerate_missing_entities=True)
print('OK')
"
```

Expected: 输出 version >= 1.20、签名含 `tolerate_missing_entities`、`OK`。
**失败(网络不通/无此版本)→ 停止,报告用户,不要进入 Task 1**(基线会失真,违反 R12)。

- [ ] **Step 3: 旧测试面回归——本仓库当前无 tests/,跑一次现回放冒烟确认快照不再失败**

```bash
python3 -m hsbot --replay tests/fixtures/mini_game.log --no-overlay --data-dir /tmp/hsbot_smoke 2>&1 | head -40
```

(此命令在 Task 1 创建 fixture 后再跑一次正式版;本步只需 Step 2 通过即可。)

---

### Task 1: 测试基建(packet 构造器 + 集成 fixture + 基线捕获)

**Files:**
- Create: `tests/__init__.py`(空)、`tests/conftest.py`、`tests/fixtures/mini_game.log`、`scripts/replay_capture.py`
- Test: `tests/test_fixtures_parse.py`

**Interfaces:**
- Produces: conftest 的 `mk_*` 系列构造器与 `EventLog`(后续所有 store 测试直接 `st.apply(pkt)` 驱动,无需额外 drive 助手);`data/baseline/mini_game.txt`(Task 7 验收基线)。

- [ ] **Step 1: tests/__init__.py(空文件)与 conftest.py**

`tests/conftest.py`:

```python
"""测试基建 —— 直接构造 hslog packet 喂 store(不经日志行, 格式零风险)。

构造器签名与 hslog 1.20 packets.py 对齐; tags 一律 [(GameTag, int)] 列表。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hearthstone.enums import BlockType, CardType, ChoiceType, GameTag, Zone
from hslog import packets
from hslog.player import PlayerManager

TS = "2026-09-08 20:00:00.0000000"


def mk_pm() -> PlayerManager:
    """两个玩家: entity 2=pid1(湫然#51704), entity 3=pid2(对手)。"""
    pm = PlayerManager()
    pm.create_or_update_player(entity_id=2, player_id=1, is_ai=False)
    pm.create_or_update_player(entity_id=3, player_id=2, is_ai=False)
    return pm


def mk_create_game() -> packets.CreateGame:
    pm = mk_pm()
    p = packets.CreateGame(TS, 1)
    p.tags = []
    p.players = [
        packets.CreateGame.Player(TS, pm.get_player_by_entity_id(2), 1, 2, 1),
        packets.CreateGame.Player(TS, pm.get_player_by_entity_id(3), 2, 2, 2),
    ]
    return p


def mk_full(eid: int, cid: str | None, **tags: int) -> packets.FullEntity:
    """tags 关键字 = GameTag 成员名(如 ZONE=1 / CARDTYPE=4), 值为裸 int。"""
    p = packets.FullEntity(TS, eid, cid)
    p.tags = [(GameTag[k], v) for k, v in tags.items()]
    return p


def mk_show(eid: int, cid: str, **tags: int) -> packets.ShowEntity:
    p = packets.ShowEntity(TS, eid, cid)
    p.tags = [(GameTag[k], v) for k, v in tags.items()]
    return p


def mk_tag(entity, tag: GameTag, value: int) -> packets.TagChange:
    return packets.TagChange(TS, entity, tag, value)


def mk_block(btype: BlockType, entity, target=0) -> packets.Block:
    b = packets.Block(TS, entity, btype, None, None, None, target, None, None)
    return b


def mk_choices_mulligan(pid_entity: int, cid: int, eids: list[int]) -> packets.Choices:
    p = packets.Choices(TS, pid_entity, cid, 1, ChoiceType.MULLIGAN, 0, len(eids))
    p.choices = list(eids)
    return p


def mk_send_mulligan(cid: int, eids: list[int]) -> packets.SendChoices:
    p = packets.SendChoices(TS, cid, ChoiceType.MULLIGAN)
    p.choices = list(eids)
    return p


def mk_choices_general(pid_entity: int, cid: int, eids: list[int]) -> packets.Choices:
    p = packets.Choices(TS, pid_entity, cid, 2, ChoiceType.GENERAL, 1, len(eids))
    p.choices = list(eids)
    return p


def mk_send_general(cid: int, eids: list[int]) -> packets.SendChoices:
    p = packets.SendChoices(TS, cid, ChoiceType.GENERAL)
    p.choices = list(eids)
    return p


class EventLog:
    """store.subscribe 的收集器。"""

    def __init__(self):
        self.events: list[dict] = []

    def __call__(self, evt: dict) -> None:
        self.events.append(dict(evt))

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def by_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]


def hero_packets(eid: int, cid: str, ctrl: int) -> list:
    return [mk_full(eid, cid, CARDTYPE=CardType.HERO.value, ZONE=Zone.PLAY.value,
                    CONTROLLER=ctrl, HEALTH=30)]
```

- [ ] **Step 2: 集成 fixture 日志 tests/fixtures/mini_game.log**

覆盖:建局/玩家/英雄/起手手牌/牌库实体、括号 SHOW_ENTITY 抽牌、ZONE 变更抽牌、PLAY 块内揭示出牌、ATTACK、TRIGGER、回合切换与水晶/血量变化、终局。全部走 `GameState.DebugPrintPower()` 流(已核实正则)。

```
D 20:00:00.0000000 GameState.DebugPrintPower() - CREATE_GAME
D 20:00:00.0000000 GameState.DebugPrintPower() -     GameEntity EntityID=1
D 20:00:00.0000000 GameState.DebugPrintPower() -         tag=TURN value=1
D 20:00:00.0000000 GameState.DebugPrintPower() -     Player EntityID=2 PlayerID=1 GameAccountId=[hi=2 lo=1]
D 20:00:00.0000000 GameState.DebugPrintPower() -     Player EntityID=3 PlayerID=2 GameAccountId=[hi=2 lo=2]
D 20:00:00.0000001 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=4 CardID=HERO_01a
D 20:00:00.0000001 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO
D 20:00:00.0000001 GameState.DebugPrintPower() -     tag=ZONE value=PLAY
D 20:00:00.0000001 GameState.DebugPrintPower() -     tag=CONTROLLER value=1
D 20:00:00.0000001 GameState.DebugPrintPower() -     tag=HEALTH value=30
D 20:00:00.0000002 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=5 CardID=HERO_02a
D 20:00:00.0000002 GameState.DebugPrintPower() -     tag=CARDTYPE value=HERO
D 20:00:00.0000002 GameState.DebugPrintPower() -     tag=ZONE value=PLAY
D 20:00:00.0000002 GameState.DebugPrintPower() -     tag=CONTROLLER value=2
D 20:00:00.0000002 GameState.DebugPrintPower() -     tag=HEALTH value=30
D 20:00:00.0000003 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=10 CardID=
D 20:00:00.0000003 GameState.DebugPrintPower() -     tag=ZONE value=DECK
D 20:00:00.0000003 GameState.DebugPrintPower() -     tag=CONTROLLER value=1
D 20:00:00.0000003 GameState.DebugPrintPower() - FULL_ENTITY - Creating ID=11 CardID=
D 20:00:00.0000003 GameState.DebugPrintPower() -     tag=ZONE value=DECK
D 20:00:00.0000003 GameState.DebugPrintPower() -     tag=CONTROLLER value=1
D 20:00:00.0000010 GameState.DebugPrintPower() - SHOW_ENTITY - Updating [entityName= UNKNOWN ENTITY [cardType=INVALID] id=10 zone=DECK zonePos=0 cardId= player=1] CardID=CS2_029
D 20:00:00.0000011 GameState.DebugPrintPower() - TAG_CHANGE Entity=10 tag=ZONE value=HAND
D 20:00:00.0000012 GameState.DebugPrintPower() - TAG_CHANGE Entity=10 tag=ZONE_POSITION value=1
D 20:00:00.0000013 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES value=1
D 20:00:00.0000014 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1
D 20:00:00.0000020 GameState.DebugPrintPower() - BLOCK_START BlockType=PLAY Entity=10 EffectCardId= EffectIndex=-1 Target=0
D 20:00:00.0000021 GameState.DebugPrintPower() - TAG_CHANGE Entity=10 tag=ZONE value=PLAY
D 20:00:00.0000021 GameState.DebugPrintPower() - TAG_CHANGE Entity=10 tag=CARDTYPE value=SPELL
D 20:00:00.0000022 GameState.DebugPrintPower() - BLOCK_END
D 20:00:00.0000030 GameState.DebugPrintPower() - TAG_CHANGE Entity=5 tag=DAMAGE value=3
D 20:00:00.0000031 GameState.DebugPrintPower() - BLOCK_START BlockType=ATTACK Entity=4 EffectCardId= EffectIndex=-1 Target=5
D 20:00:00.0000032 GameState.DebugPrintPower() - BLOCK_END
D 20:00:00.0000040 GameState.DebugPrintPower() - BLOCK_START BlockType=TRIGGER Entity=10 EffectCardId= EffectIndex=-1 Target=0
D 20:00:00.0000041 GameState.DebugPrintPower() - BLOCK_END
D 20:00:00.0000050 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=0
D 20:00:00.0000051 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1
D 20:00:00.0000052 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=RESOURCES value=1
D 20:00:00.0000060 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON
D 20:00:00.0000061 GameState.DebugPrintPower() - TAG_CHANGE Entity=3 tag=PLAYSTATE value=LOST
```

- [ ] **Step 3: 解析形状测试 tests/test_fixtures_parse.py**

```python
"""fixture 必须真的被 hslog 解析成预期 packet(行格式护栏)。"""
from hslog import packets as P
from hslog import LogParser

from .conftest import TS, mk_create_game, mk_full, mk_pm


def _parse(path):
    parser = LogParser()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            parser.read_line(line + "\n")
        except Exception:
            pass
    return parser


def test_mini_game_parses(tmp_path=None):
    from pathlib import Path
    parser = _parse(Path(__file__).parent / "fixtures" / "mini_game.log")
    assert len(parser.games) == 1
    flat = list(parser.games[0].recursive_iter()) if hasattr(parser.games[0], "recursive_iter") else []
    # recursive_iter 在 1.18/1.20 均有: PacketTree.recursive_iter(cls=None) 递归产出
    kinds = [type(p).__name__ for p in flat]
    for expect in ("CreateGame", "FullEntity", "ShowEntity", "TagChange", "Block"):
        assert expect in kinds, f"fixture 缺 {expect}: {kinds}"
    assert kinds.count("Block") == 3          # PLAY + ATTACK + TRIGGER
    from hearthstone.enums import BlockType
    blocks = [p for p in flat if type(p).__name__ == "Block"]
    assert {b.type for b in blocks} >= {BlockType.PLAY, BlockType.ATTACK, BlockType.TRIGGER}


def test_constructed_packets_drive_exporter():
    """conftest 构造器喂 EntityTreeExporter 能建实体树(与库对齐的护栏)。"""
    from hearthstone.enums import CardType, GameTag, Zone
    from hslog.export import EntityTreeExporter

    pm = mk_pm()
    ex = EntityTreeExporter(_Tree(), player_manager=pm, tolerate_missing_entities=True)
    ex.export_packet(mk_create_game())
    ex.export_packet(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                             ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    hero = ex.game.find_entity_by_id(4)
    assert hero.card_id == "HERO_01a"
    assert hero.tags.get(GameTag.HEALTH) == 30
    assert hero.zone == Zone.PLAY


class _Tree:
    """exporter 只在 export() 时读 packet_tree, 手动驱动传占位即可。"""
    def __iter__(self):
        return iter(())
```

- [ ] **Step 4: 跑测试**

```bash
python3 -m pytest tests/ -q
```

Expected: 2 passed。若 fixture 断言失败,对照 tokens.py 正则修 fixture 行格式(正则清单见"已核实的关键事实"第 5 条),**不要改断言迁就**。

- [ ] **Step 5: 基线捕获脚本 scripts/replay_capture.py**

```python
"""回放采集 —— 把 watcher 输出落盘, 供新旧版本 diff 验收(spec §6)。

用法: python3 scripts/replay_capture.py <Power.log...> [-o 输出目录] [--data-dir D]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.watcher import Watcher


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("-o", "--out", default="data/baseline")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       **({"data_dir": args.data_dir} if args.data_dir else {})})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    rc = 0
    for log in args.logs:
        lines: list[str] = []
        w = Watcher(cfg, carddb, out=lines.append)
        w.run_replay(log)
        dest = out / (Path(log).stem + ".txt")
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        bad = sum(1 for l in lines if "快照导出失败" in l)
        print(f"{log}: {len(lines)} 行 -> {dest}" + (f" !!快照失败{bad}处" if bad else ""))
        if bad:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: 在未改动的 HEAD 上采集基线**

```bash
python3 scripts/replay_capture.py tests/fixtures/mini_game.log -o data/baseline
```

Expected: `1 行输出文件`,无"快照失败"(Task 0 已修)。失败则按输出修,不带病基线。

- [ ] **Step 7: Commit**

```bash
git add tests/ scripts/replay_capture.py data/baseline/
git commit -m "test: 测试基建(packet构造器/mini_game fixture/回放采集脚本)+ 旧版输出基线"
```

---

### Task 2: adapter.StoreExporter(库状态机 + 挂钩)

**Files:**
- Modify: `hsbot/adapter.py`(追加;`export_game_state` 本任务不动,Task 6 删)
- Test: `tests/test_store_exporter.py`

**Interfaces:**
- Produces: `StoreExporter(packet_tree, player_manager, hooks)`——hooks 为 duck-typing 对象,需提供 `on_tag_change/on_full_entity/on_show_entity/on_hide_entity/on_change_entity/on_create_game/on_block(packet)` 方法;`new_store_exporter(hooks, tree, player_manager) -> StoreExporter`;`is_block(p)`、`is_play_block(p)`;`game_meta(parser) -> dict[str, str]`。
- 关键语义:挂钩在 `super().handle_*()`(库已应用)之后调用;`handle_block` **不**迭代子包(子包由游标逐个驱动,避免重复应用)。

- [ ] **Step 1: 失败测试 tests/test_store_exporter.py**

```python
"""StoreExporter: 库维护状态 + 挂钩后置触发 + 子包不连带应用。"""
from hearthstone.enums import BlockType, GameTag, Zone
from hslog import packets

from hsbot.adapter import StoreExporter

from .conftest import TS, mk_block, mk_create_game, mk_full, mk_pm, mk_tag


class _Tree:
    def __iter__(self):
        return iter(())


def _exporter(hooks) -> StoreExporter:
    ex = StoreExporter(_Tree(), mk_pm(), hooks)
    ex.export_packet(mk_create_game())
    return ex


def test_hook_fires_after_state_applied():
    seen = []

    class Hooks:
        def on_tag_change(self, p):
            seen.append(p.entity and ex.game.find_entity_by_id(64).zone)

    ex = _exporter(Hooks())
    ex.export_packet(mk_full(64, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    ex.export_packet(mk_tag(64, GameTag.ZONE, Zone.HAND.value))
    assert seen == [Zone.HAND]          # 挂钩里读到的已是新状态


def test_full_entity_and_show_entity_hooks():
    seen = []

    class Hooks:
        def on_full_entity(self, p):
            seen.append(("full", p.card_id))

        def on_show_entity(self, p):
            seen.append(("show", p.card_id))

    ex = _exporter(Hooks())
    ex.export_packet(mk_full(64, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    from .conftest import mk_show
    ex.export_packet(mk_show(64, "CS2_029", ZONE=Zone.HAND.value))
    assert seen == [("full", None), ("show", "CS2_029")]


def test_block_does_not_apply_children():
    class Hooks:
        pass

    ex = _exporter(Hooks())
    b = mk_block(BlockType.PLAY, 64)
    b.packets = [mk_tag(64, GameTag.ZONE, Zone.PLAY.value)]
    ex.export_packet(b)                       # 只登记块, 不应用子包
    assert ex.game.find_entity_by_id(64) is None
    ex.export_packet(mk_full(64, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1))
    ex.export_packet(b.packets[0])            # 子包由游标单独驱动
    assert ex.game.find_entity_by_id(64).zone == Zone.PLAY


def test_dirty_packets_tolerated_and_still_notify():
    hits = []

    class Hooks:
        def on_tag_change(self, p):
            hits.append(1)

    ex = StoreExporter(_Tree(), mk_pm(), Hooks())   # 尚无 game
    ex.export_packet(mk_tag(999, GameTag.ZONE, 1))  # 找不到实体: 不炸, 仍通知
    assert hits == [1]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_store_exporter.py -q
```

Expected: FAIL(`ImportError: cannot import name 'StoreExporter'`)。

- [ ] **Step 3: 实现(adapter.py 追加;import 区补 `from hearthstone.enums import BlockType, GameTag`——GameTag 已有,补 BlockType)**

在 `hslog/adapter.py` 的 `_TolerantExporter` 类定义之后追加:

```python
class StoreExporter(_TolerantExporter):
    """库状态机 + 挂钩: super() 维护实体状态后回调 hooks(duck-typing)。

    铁律: hslog 导出类不出本模块。hooks 只需提供 on_*(packet) 方法(GameStore)。
    handle_block/handle_sub_spell 不迭代子包 —— 游标逐包驱动, 连带应用会重复。
    """

    def __init__(self, packet_tree, player_manager, hooks) -> None:
        super().__init__(packet_tree, player_manager=player_manager,
                         tolerate_missing_entities=True)
        self.hooks = hooks

    def _notify(self, name: str, packet) -> None:
        cb = getattr(self.hooks, name, None)
        if cb is not None:
            cb(packet)

    def handle_create_game(self, packet):
        super().handle_create_game(packet)
        self._notify("on_create_game", packet)

    def handle_tag_change(self, packet):
        super().handle_tag_change(packet)
        self._notify("on_tag_change", packet)

    def handle_full_entity(self, packet):
        super().handle_full_entity(packet)
        self._notify("on_full_entity", packet)

    def handle_show_entity(self, packet):
        super().handle_show_entity(packet)
        self._notify("on_show_entity", packet)

    def handle_hide_entity(self, packet):
        super().handle_hide_entity(packet)
        self._notify("on_hide_entity", packet)

    def handle_change_entity(self, packet):
        try:
            super().handle_change_entity(packet)
        except Exception as exc:  # noqa: BLE001
            log.debug("ChangeEntity 导出失败: %s", exc)
        self._notify("on_change_entity", packet)

    def handle_block(self, packet):
        if packet.type == BlockType.GAME_RESET and self.game is not None:
            self.game.reset()
        self._notify("on_block", packet)       # 不调 super(): 不迭代子包

    def handle_sub_spell(self, packet):
        self._notify("on_block", packet)       # 同上, 子包由游标驱动


def new_store_exporter(hooks, packet_tree, player_manager) -> StoreExporter:
    return StoreExporter(packet_tree, player_manager, hooks)


def is_block(p) -> bool:
    return isinstance(p, packets.Block)


def is_play_block(p) -> bool:
    return isinstance(p, packets.Block) and p.type == BlockType.PLAY


def game_meta(parser) -> dict[str, str]:
    """parser.game_meta 的字符串化(快照 meta 用, 原 export_game_state 内联逻辑)。"""
    return {str(k): str(getattr(v, "name", v))
            for k, v in (parser.game_meta or {}).items()}
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

```bash
python3 -m pytest tests/ -q
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add hsbot/adapter.py tests/test_store_exporter.py
git commit -m "feat: StoreExporter——库实体状态机+挂钩回调(块不迭代子包)"
```

---

### Task 3: GameStore 核心(apply 管线 + TagChange 衍生 + 查询)

**Files:**
- Create: `hsbot/store.py`
- Test: `tests/test_store_core.py`

**Interfaces:**
- Consumes: Task 2 的 `new_store_exporter/is_play_block`。
- Produces(Task 4/5/6 依赖):`GameStore(carddb=, battletag=, tree=, player_manager=)`;`apply(p, depth=0)`、`settle()`、`note_friendly(pid)`、`hint_cid/hint_ctrl/hint_draw`、`subscribe(cb)`、`meta`/`friendly_key`/`played_cids`/`current` 字段;查询 `hand/board/deck_entities/deck_count/graveyard_count/secret_count/mana_fields/mana_now/mana_next_turn/hero/hero_hp/hero_armor/hero_total_hp/current_key/opponent_key/player_keys/name/playstate/is_my_turn/turn/friendly_turn_number/entities/get/cid_of/ctrl_key`;模块级助手 `atk/hp_total/is_taunt/is_generated/zone_pos/is_hero/is_hero_power/is_coin`;事件 kind:`text/draw/back_to_deck/cost/death/turn_start/game_end/shuffle/raw`(Task 4 补 play/attack/trigger/fatigue/gain/discover/mulligan);字段 `unhandled: list[dict]`(raw 事件台账,上限 500 条)。
**全量收录原则(spec §3.4)**:分发表之外或未解释的 packet 类型一律记 `raw`(packet 类型+完整载荷,入库不渲染),不允许静默丢弃。

- [ ] **Step 1: 失败测试 tests/test_store_core.py**

```python
"""GameStore 核心: apply 管线 / TagChange 衍生 / 查询 API。"""
from hearthstone.enums import CardType, GameTag, Zone

from hsbot.carddb import CardDB
from hsbot.store import GameStore, is_hero

from .conftest import (EventLog, mk_create_game, mk_full, mk_pm, mk_show,
                       mk_tag)


class _Tree:
    def __iter__(self):
        return iter(())


def _store(battletag="湫然#51704"):
    carddb = CardDB("/nonexistent/cards.json")     # 降级: name()=card_id
    st = GameStore(carddb=carddb, battletag=battletag,
                   tree=_Tree(), player_manager=mk_pm())
    log = EventLog()
    st.subscribe(log)
    st.apply(mk_create_game())
    st.note_friendly(1)
    return st, log


def _heroes(st):
    st.apply(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    st.apply(mk_full(5, "HERO_02a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, HEALTH=30))


def test_turn_start_events():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))    # 首个行动方=我
    st.apply(mk_tag(1, GameTag.TURN, 1))
    st.apply(mk_tag(2, GameTag.TURN, 1))
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 0))
    st.apply(mk_tag(3, GameTag.CURRENT_PLAYER, 1))    # 切到对手
    ks = log.kinds()
    assert ks[0] == "turn_start" and log.events[0]["first"] is True
    assert "text" in ks and log.by_kind("text")[0]["msg"] == "结束回合"
    ts = [e for e in log.events if e["kind"] == "turn_start"][-1]
    assert ts["actor"] == 2 and ts["prev"] == 1 and ts["first"] is False


def test_mana_cap_event_only_friendly():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.RESOURCES, 1))
    st.apply(mk_tag(2, GameTag.RESOURCES, 2))         # 我方 1→2
    st.apply(mk_tag(3, GameTag.RESOURCES, 1))
    st.apply(mk_tag(3, GameTag.RESOURCES, 2))         # 对手: 不发
    msgs = [e["msg"] for e in log.by_kind("text") if "水晶上限" in e.get("msg", "")]
    assert msgs == ["水晶上限 1→2"]


def test_hero_damage_event():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(5, GameTag.DAMAGE, 3))            # 敌方英雄掉血
    msgs = [e["msg"] for e in log.by_kind("text")]
    assert any("敌方英雄 30血→27血" in m for m in msgs)


def test_zone_draw_and_back_to_deck_and_death():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_show(10, "CS2_029", ZONE=Zone.HAND.value))       # 揭示入手机牌
    st.apply(mk_tag(10, GameTag.ZONE, Zone.HAND.value))          # DECK→HAND
    draws = log.by_kind("draw")
    assert draws and draws[-1]["card_id"] == "CS2_029"
    # 置回牌库(SETASIDE→DECK)
    st.apply(mk_tag(10, GameTag.ZONE, Zone.SETASIDE.value))
    st.apply(mk_tag(10, GameTag.ZONE, Zone.DECK.value))
    assert log.by_kind("back_to_deck")[-1]["card_id"] == "CS2_029"
    # 随从死亡(PLAY→GRAVEYARD)
    st.apply(mk_full(11, "CS2_120", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2))
    st.apply(mk_tag(11, GameTag.ZONE, Zone.GRAVEYARD.value))
    assert log.by_kind("death")[-1]["card_id"] == "CS2_120"


def test_cost_event():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(12, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1))
    st.apply(mk_tag(12, GameTag.COST, 1))             # 面板2→实付1
    evt = log.by_kind("cost")[-1]
    assert (evt["old"], evt["new"]) == (2, 1)


def test_game_end_emitted_once():
    st, log = _store()
    _heroes(st)
    from hearthstone.enums import PlayState
    st.apply(mk_tag(2, GameTag.PLAYSTATE, PlayState.WON.value))
    st.apply(mk_tag(3, GameTag.PLAYSTATE, PlayState.LOST.value))
    assert len(log.by_kind("game_end")) == 1


def test_queries():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1,
                     ZONE_POSITION=2))
    st.apply(mk_full(11, "CS2_023t", ZONE=Zone.HAND.value, CONTROLLER=1,
                     CARDTYPE=CardType.MINION.value, ZONE_POSITION=1))
    st.apply(mk_full(12, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_tag(2, GameTag.RESOURCES, 3))
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, 1))
    assert [e.card_id for e in st.hand(1)] == ["CS2_023t", "CS2_029"]   # 按位置序
    assert st.deck_count(1) == 1
    assert st.mana_now(1) == 2 and st.mana_next_turn(1) == 4
    assert st.hero_hp(1) == 30 and st.hero(2).card_id == "HERO_02a"
    assert st.playstate(1) == "INVALID"


def test_hint_and_friendly_and_lines():
    st, log = _store(battletag="")
    st.hint_cid(77, "TTN_001")
    st.hint_ctrl(77, 1)
    assert st.cid_of(77) == "TTN_001"
    st.lines_consumed = 42
    assert st.lines_consumed == 42
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_store_core.py -q
```

Expected: FAIL(`ModuleNotFoundError: hsbot.store`)。

- [ ] **Step 3: 实现 hsbot/store.py(核心部分;Task 4 续写块/选择/to_dict)**

```python
"""状态层 —— 每局一个 GameStore: 唯一可变状态权威(spec 2026-09-07 §3)。

实体/标签/区域由 adapter.StoreExporter(hslog EntityTreeExporter 容错子类)维护在
hearthstone.entities 上(不造轮子); store 只追踪库没有的部分(留牌/发现/PLAY延迟/
回合计数/括号线索), 并从状态迁移衍生链路事件。

变更入口(spec §3.2 细化): apply(p, depth) / settle() / note_friendly(pid)
/ hint_cid / hint_ctrl / hint_draw —— 之外无人可改状态。
契约: 查询返回活引用只读; 单线程(watcher 轮询线程独占);
订阅者同步调用、不得重入 apply。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from hearthstone.entities import Player
from hearthstone.enums import CardType, GameTag, PlayState, Zone

from .adapter import is_play_block, new_store_exporter, packet_payload
from .carddb import CardDB

PlayerKey = int  # PLAYER_ID: 1=先手, 2=后手(硬币); CONTROLLER 标签值同域

_TERMINAL_PLAYSTATE = {PlayState.WON.value, PlayState.LOST.value, PlayState.TIED.value}
_MANA_TAGS = {
    GameTag.RESOURCES: "res",
    GameTag.TEMP_RESOURCES: "temp",
    GameTag.RESOURCES_USED: "used",
    GameTag.OVERLOAD_OWED: "overload",
    GameTag.OVERLOAD_LOCKED: "overload",
}
_HAND_TYPES = (CardType.SPELL, CardType.MINION, CardType.WEAPON,
               CardType.HERO, CardType.ITEM, CardType.TOKEN, CardType.INVALID)

EventCb = Callable[[dict], None]


# ---------- 实体只读助手(库 Card 缺的计算属性; 全项目唯一定义处) ----------
def atk(e) -> int:
    return e.tags.get(GameTag.ATK, 0)


def hp_total(e) -> int:
    return max(0, e.tags.get(GameTag.HEALTH, 0) + e.tags.get(GameTag.ARMOR, 0)
               - e.tags.get(GameTag.DAMAGE, 0))


def is_taunt(e) -> bool:
    return bool(e.tags.get(GameTag.TAUNT))


def is_generated(e) -> bool:
    return GameTag.CREATOR in e.tags


def zone_pos(e) -> int:
    return e.tags.get(GameTag.ZONE_POSITION, 0)


def is_hero(e) -> bool:
    return e.tags.get(GameTag.CARDTYPE) == CardType.HERO


def is_hero_power(e) -> bool:
    return e.tags.get(GameTag.CARDTYPE) == CardType.HERO_POWER


def is_coin(cid: str | None) -> bool:
    return bool(cid) and ("COIN" in cid.upper() or cid.upper() == "GAME_005")


@dataclass
class MulliganState:
    offered: list = field(default_factory=list)
    kept: list = field(default_factory=list)


class GameStore:
    """每局一个; 由 Watcher._new_game 创建, 局终丢弃(spec §3.6)。"""

    def __init__(self, *, carddb: CardDB, battletag: str = "",
                 tree=None, player_manager=None) -> None:
        self.carddb = carddb
        self.battletag = battletag
        self.exporter = new_store_exporter(self, tree, player_manager)
        self.meta: dict[str, str] = {}
        self.friendly_key: PlayerKey | None = None
        self.lines_consumed = 0
        # ---- 库没有的选择/显示态 ----
        self.current: PlayerKey | None = None
        self.player_turn: dict[PlayerKey, int] = {}
        self.mulligan: dict[PlayerKey, MulliganState] = {}
        self.played_cids: set[str] = set()
        self._choice_pid: dict[int, PlayerKey] = {}
        self._mulligan_emitted: set[int] = set()
        self._discover: dict[int, dict] = {}
        self._discover_emitted: set[int] = set()
        self._pending_play: tuple[int, object] | None = None
        self._hint_cid: dict[int, str] = {}
        self._hint_ctrl: dict[int, PlayerKey] = {}
        self._draw_ts: dict[int, float] = {}
        self._ent2pid: dict[int, PlayerKey] = {}
        self._ended = False
        self._subs: list[EventCb] = []
        self.unhandled: list[dict] = []    # raw 台账(spec §3.4 全量收录, 上限 500)

    # ================= 基础 =================
    @property
    def game(self):
        return self.exporter.game

    @property
    def turn(self) -> int:
        g = self.game
        return g.tags.get(GameTag.TURN, 0) if g is not None else 0

    def entities(self) -> list:
        g = self.game
        return list(g.entities) if g is not None else []

    def get(self, eid: int):
        g = self.game
        return g.find_entity_by_id(eid) if g is not None else None

    def cid_of(self, eid: int) -> str | None:
        e = self.get(eid)
        cid = getattr(e, "card_id", None) if e is not None else None
        return cid or self._hint_cid.get(eid)

    def ctrl_key(self, e) -> PlayerKey | None:
        c = getattr(e, "controller", None)
        return c.player_id if c is not None else None

    def player_keys(self) -> list[PlayerKey]:
        g = self.game
        return sorted(p.player_id for p in g.players) if g is not None else []

    def opponent_key(self) -> PlayerKey | None:
        if self.friendly_key is None:
            return None
        for k in self.player_keys():
            if k != self.friendly_key:
                return k
        return None

    def name(self, key: PlayerKey | None) -> str:
        if key is None:
            return "?"
        g = self.game
        if g is not None:
            p = g.get_player(key)
            if p is not None and getattr(p, "name", None):
                return p.name
        return str(key)

    # ================= 查询(活引用, 只读) =================
    def _of(self, key: PlayerKey | None) -> list:
        return [e for e in self.entities()
                if key is None or self.ctrl_key(e) == key]

    def hero(self, key: PlayerKey):
        heroes = [e for e in self._of(key) if is_hero(e)]
        heroes.sort(key=lambda e: 0 if e.zone == Zone.PLAY else 1)
        return heroes[0] if heroes else None

    def hero_hp(self, key: PlayerKey) -> int:
        h = self.hero(key)
        if h is None:
            return 0
        return max(0, h.tags.get(GameTag.HEALTH, 0) - h.tags.get(GameTag.DAMAGE, 0))

    def hero_armor(self, key: PlayerKey) -> int:
        h = self.hero(key)
        return h.tags.get(GameTag.ARMOR, 0) if h else 0

    def hero_total_hp(self, key: PlayerKey) -> int:
        h = self.hero(key)
        return hp_total(h) if h else 0

    def hand(self, key: PlayerKey) -> list:
        cards = [e for e in self._of(key) if e.zone == Zone.HAND
                 and e.tags.get(GameTag.CARDTYPE, CardType.INVALID) in _HAND_TYPES]
        cards.sort(key=zone_pos)
        return cards

    def board(self, key: PlayerKey) -> list:
        minions = [e for e in self._of(key) if e.zone == Zone.PLAY
                   and e.tags.get(GameTag.CARDTYPE) == CardType.MINION]
        minions.sort(key=zone_pos)
        return minions

    def deck_entities(self, key: PlayerKey) -> list:
        return [e for e in self._of(key) if e.zone == Zone.DECK]

    def deck_count(self, key: PlayerKey) -> int:
        return len(self.deck_entities(key))

    def graveyard_count(self, key: PlayerKey) -> int:
        return sum(1 for e in self._of(key) if e.zone == Zone.GRAVEYARD)

    def secret_count(self, key: PlayerKey) -> int:
        return sum(1 for e in self._of(key) if e.zone == Zone.SECRET)

    def _player_entity(self, key: PlayerKey):
        g = self.game
        return g.get_player(key) if g is not None else None

    def mana_fields(self, key: PlayerKey) -> dict[str, int]:
        e = self._player_entity(key)
        t = e.tags if e is not None else {}
        return {
            "res": t.get(GameTag.RESOURCES, 0),
            "used": t.get(GameTag.RESOURCES_USED, 0),
            "temp": t.get(GameTag.TEMP_RESOURCES, 0),
            "overload": t.get(GameTag.OVERLOAD_OWED, 0) + t.get(GameTag.OVERLOAD_LOCKED, 0),
        }

    def mana_now(self, key: PlayerKey) -> int:
        f = self.mana_fields(key)
        return max(0, f["res"] + f["temp"] - f["used"])

    def mana_next_turn(self, key: PlayerKey) -> int:
        f = self.mana_fields(key)
        return max(0, min(10, f["res"] + 1 - f["overload"]))

    def current_key(self) -> PlayerKey | None:
        return self.current

    def is_my_turn(self) -> bool:
        return self.friendly_key is not None and self.current == self.friendly_key

    def playstate(self, key: PlayerKey) -> str:
        e = self._player_entity(key)
        v = e.tags.get(GameTag.PLAYSTATE, 0) if e is not None else 0
        try:
            return PlayState(v).name
        except Exception:  # noqa: BLE001
            return str(v)

    def friendly_turn_number(self) -> int:
        """我的第 N 回合(TURN 是半回合制, 按 FIRST_PLAYER 换算)。"""
        raw = self.turn
        g = self.game
        first = None
        if g is not None:
            fp = g.first_player
            first = fp.player_id if fp is not None else None
        mine_is_first = first is not None and first == self.friendly_key
        return (raw + 1) // 2 if mine_is_first else raw // 2

    # ================= 订阅(spec §3.4) =================
    def subscribe(self, cb: EventCb) -> None:
        self._subs.append(cb)

    def _emit_event(self, evt: dict) -> None:
        evt.setdefault("turn", self.turn)
        evt.setdefault("friendly", self.friendly_key)
        for cb in self._subs:
            cb(evt)

    # ================= 变更入口(spec §3.2 细化) =================
    def apply(self, p, depth: int = 0) -> None:
        """游标逐包调用: 先刷挂起 PLAY → 取旧标签 → 库应用 → 衍生。"""
        if self._pending_play is not None and depth <= self._pending_play[0]:
            self._flush_play()
        old = self._pre_tags(p)
        self.exporter.export_packet(p)
        self._derive(p, old)
        if is_play_block(p):
            self._pending_play = (depth, p)

    def settle(self) -> None:
        """批尾: 块已收口(ended)的挂起 PLAY 立即发出。"""
        if self._pending_play is not None and getattr(self._pending_play[1], "ended", False):
            self._flush_play()

    def note_friendly(self, pid: PlayerKey | None) -> None:
        if pid is not None:
            self.friendly_key = pid

    def hint_cid(self, eid: int, cid: str) -> None:
        self._hint_cid[eid] = cid

    def hint_ctrl(self, eid: int, pid: PlayerKey) -> None:
        self._hint_ctrl[eid] = pid

    def hint_draw(self, eid: int, cid: str, actor: PlayerKey) -> None:
        """行级括号 SHOW_ENTITY(抽牌揭示主形态)的直通口, 走同一去重。"""
        self._emit_draw(eid, cid, actor)

    def _pre_tags(self, p) -> dict | None:
        """受影响实体应用前的标签快照(推导 区域/费用/血甲 变化用)。"""
        eid = getattr(p, "entity", None)
        if not isinstance(eid, int):
            return None
        e = self.get(eid)
        return dict(e.tags) if e is not None else None

    # ================= 状态迁移 → 事件/自有字段 =================
    def _derive(self, p, old: dict | None) -> None:
        # 按类型名分发(避免 store import hslog, 铁律)
        name = type(p).__name__
        if name == "TagChange":
            self._on_tag_change(p, old)
        elif name == "CreateGame":
            self._ent2pid = {pl.entity.entity_id: pl.player_id
                             for pl in p.players
                             if hasattr(pl.entity, "entity_id") and pl.player_id}
        elif name == "FullEntity":
            self._on_full_entity(p)
        elif name == "ShowEntity":
            self._on_show_entity(p)
        elif name == "HideEntity":
            self._on_hide_entity(p)
        elif name == "Choices":
            self._on_choices(p)
        elif name == "SendChoices":
            self._on_send_choices(p)
        elif name == "ChosenEntities":
            self._on_chosen(p)
        elif name == "ShuffleDeck":
            if self.friendly_key is not None:   # 调度阶段的洗牌是噪音
                self._emit_event({"kind": "shuffle",
                                  "actor": getattr(p, "player_id", None)})
        elif name == "Block":
            self._on_block(p)
        else:
            # 全量收录原则: 未解释的包记 raw(MetaData/Options/SubSpell/ChangeEntity...)
            self._record_raw(name, p)

    def _record_raw(self, ptype: str, p) -> None:
        """raw 事件: 入库(store.unhandled -> JSONL), 照常分发订阅, 但渲染层跳过。"""
        evt = {"kind": "raw", "packet_type": ptype, "payload": packet_payload(p)}
        self.unhandled.append(evt)
        if len(self.unhandled) > 500:
            del self.unhandled[:len(self.unhandled) - 500]
        self._emit_event(evt)

    def _key_of(self, entity) -> PlayerKey | None:
        if hasattr(entity, "player_id") and not isinstance(entity, int):
            return entity.player_id or None
        if isinstance(entity, int):
            g = self.game
            if g is not None and entity == g.id:
                return None                       # GameEntity
            pid = self._ent2pid.get(entity)
            if pid is not None:
                return pid
            e = self.get(entity)
            if isinstance(e, Player):
                return e.player_id
        return None

    def _on_tag_change(self, p, old: dict | None) -> None:
        tag, value, entity = p.tag, p.value, p.entity
        key = self._key_of(entity)
        if key is not None:
            if tag == GameTag.CURRENT_PLAYER and value == 1:
                self._on_turn_start(key)
            elif tag == GameTag.PLAYSTATE and value in _TERMINAL_PLAYSTATE \
                    and not self._ended:
                self._ended = True
                self._emit_event({"kind": "game_end", "actor": key})
            elif tag == GameTag.TURN:
                self.player_turn[key] = int(value or 0)
            elif tag in _MANA_TAGS:
                nm = _MANA_TAGS[tag]
                prev = (old or {}).get(tag)
                cur = int(value or 0)
                if key == self.friendly_key and prev is not None and prev != cur:
                    if nm == "res" and cur > prev:
                        self._emit_event({"kind": "text", "actor": key,
                                          "msg": f"水晶上限 {prev}→{cur}"})
                    elif nm == "temp" and cur > prev:
                        self._emit_event({"kind": "text", "actor": key,
                                          "msg": f"临时水晶 +{cur - prev}"})
            return
        if not isinstance(entity, int):
            return
        e = self.get(entity)
        if e is None:
            return
        if tag == GameTag.ZONE:
            self._on_zone_change(e, old, value)
        elif tag == GameTag.COST:
            self._on_cost_change(e, old, value)
        elif tag in (GameTag.DAMAGE, GameTag.ARMOR, GameTag.HEALTH) and is_hero(e):
            self._on_hero_attr(e, old, tag)

    def _on_zone_change(self, e, old, value) -> None:
        old_zone = (old or {}).get(GameTag.ZONE)
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        cid = e.card_id or self._hint_cid.get(e.id)
        if (value == Zone.DECK.value and old_zone == Zone.SETASIDE.value
                and ctrl == self.friendly_key and cid):
            self._emit_event({"kind": "back_to_deck", "card_id": cid, "actor": ctrl})
        elif (value == Zone.HAND.value and old_zone == Zone.DECK.value
              and ctrl == self.friendly_key and cid):
            # 已揭示实体再次抽到(探底/置底过的牌)——无 SHOW_ENTITY, 只有 ZONE 变更
            self._emit_draw(e.id, cid, ctrl)
        elif (value == Zone.GRAVEYARD.value and old_zone == Zone.PLAY.value
              and e.tags.get(GameTag.CARDTYPE) == CardType.MINION and cid):
            self._emit_event({"kind": "death", "actor": ctrl, "card_id": cid})

    def _on_cost_change(self, e, old, value) -> None:
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        cid = e.card_id or self._hint_cid.get(e.id)
        if ctrl != self.friendly_key or not cid:
            return
        old_cost = (old or {}).get(GameTag.COST)
        base = self.carddb.cost(cid)
        ref = old_cost if old_cost is not None else base   # 首次变动以面板费为基准
        if ref is not None and value != ref:
            self._emit_event({"kind": "cost", "card_id": cid,
                              "old": ref, "new": value, "actor": ctrl})

    def _on_hero_attr(self, e, old, tag) -> None:
        if not old:
            return
        h0 = old.get(GameTag.HEALTH)
        base = h0 if h0 is not None else 30
        d0 = int(old.get(GameTag.DAMAGE, 0) or 0)
        a0 = int(old.get(GameTag.ARMOR, 0) or 0)
        hp_old = max(0, base - d0)
        hp_new = max(0, e.tags.get(GameTag.HEALTH, 30) - e.tags.get(GameTag.DAMAGE, 0))
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        who = "我方英雄" if ctrl == self.friendly_key else "敌方英雄"
        if tag == GameTag.DAMAGE and d0 != e.tags.get(GameTag.DAMAGE, 0):
            self._emit_event({"kind": "text", "actor": ctrl,
                              "msg": f"{who} {hp_old}血→{hp_new}血"
                                     f"(甲{e.tags.get(GameTag.ARMOR, 0)})"})
        elif tag == GameTag.ARMOR and a0 != e.tags.get(GameTag.ARMOR, 0):
            self._emit_event({"kind": "text", "actor": ctrl,
                              "msg": f"{who}护甲 {a0}→{e.tags.get(GameTag.ARMOR, 0)}"
                                     f"(血{hp_new})"})

    def _on_turn_start(self, key: PlayerKey) -> None:
        prev, self.current = self.current, key
        if prev is None:
            # 首个行动方(先手的留牌回合)
            self._emit_event({"kind": "turn_start", "actor": key, "prev": None,
                              "first": True, "my_turn_no": 1, "total_turn": 1})
            return
        if prev == key or self._ended:
            return
        self._emit_event({"kind": "text", "actor": prev, "msg": "结束回合"})
        n = self.player_turn.get(key, 0) + 1   # 玩家级 TURN 标签在切换之后才到
        g = self.game
        fp = g.first_player if g is not None else None
        first_key = fp.player_id if fp is not None else None
        total = 2 * n - (1 if first_key == key else 0)
        self._emit_event({"kind": "turn_start", "actor": key, "prev": prev,
                          "first": False, "my_turn_no": n, "total_turn": total})

    # ---- 实体/揭示挂钩(Task 4 实现块/选择; 这两个在本任务即有行为) ----
    def _on_full_entity(self, p) -> None:
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        if (e is not None and e.zone == Zone.HAND and p.card_id
                and self.ctrl_key(e) == self.friendly_key):
            creator = self.get(e.tags.get(GameTag.CREATOR, 0) or 0)
            self._emit_event({"kind": "gain", "card_id": p.card_id,
                              "actor": self.ctrl_key(e),
                              "creator": getattr(creator, "card_id", None)})

    def _on_show_entity(self, p) -> None:
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        if e is None:
            return
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(p.entity)
        if e.zone == Zone.HAND and ctrl == self.friendly_key and p.card_id:
            self._emit_draw(p.entity, p.card_id, ctrl)

    def _on_hide_entity(self, p) -> None:
        # 库缺口: entity.hide() 只撤 revealed, 不落 ZONE —— 换牌/洗回后
        # "再次抽到"的区域变更识别依赖这里补写(spec §2.2 自研项)
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        zone_val = getattr(p, "zone", None)
        if e is not None and zone_val is not None:
            try:
                e.tags[GameTag.ZONE] = int(zone_val)
            except (TypeError, ValueError):
                pass

    def _emit_draw(self, eid: int, cid: str, actor) -> None:
        now = time.monotonic()
        last = self._draw_ts.get(eid)
        if last is not None and now - last < 0.5:
            return   # 同一实体的多重揭示路径只报一次
        self._draw_ts[eid] = now
        self._emit_event({"kind": "draw", "card_id": cid, "actor": actor})
```

- [ ] **Step 4: 跑测试通过**

```bash
python3 -m pytest tests/test_store_core.py -q
```

Expected: 8 passed。若 `test_turn_start_events` 的 total_turn 断言失败,先查 fixture 是否给玩家 TURN 标签(旧逻辑同样依赖到达顺序)。

- [ ] **Step 5: 全量回归 + Commit**

```bash
python3 -m pytest tests/ -q
git add hsbot/store.py tests/test_store_core.py
git commit -m "feat: GameStore 核心——apply 管线/旧标签对比衍生事件/查询 API"
```

---

### Task 4: GameStore 块/选择/导出(PLAY 延迟 + TRIGGER/疲劳 + 留牌/发现 + to_dict)

**Files:**
- Modify: `hsbot/store.py`(追加块/选择/导出;import 区补 `BlockType, ChoiceType`)
- Modify: `tests/conftest.py`(选择构造器改引用形态)
- Test: `tests/test_store_blocks.py`

**Interfaces:**
- Produces: 事件 kind 补齐 `play/attack/trigger/fatigue/gain/discover/mulligan`;`mulligan_text(key)`、`mana_text(key)`、`to_dict(reason) -> dict`(JSONL 载荷,键结构 = 旧 `GameState.to_dict` + `reason`)。
- 语义:`apply(p, depth)` 遇到不深于挂起 PLAY 的新包先冲刷;`settle()` 在批尾冲刷 `ended` 的挂起 PLAY(与旧 `_process_tree` 逐字对应)。

- [ ] **Step 1: 修 conftest 选择构造器(实体必须是 PlayerReference 形态)**

`tests/conftest.py` 中替换 `mk_choices_mulligan/mk_choices_general` 为:

```python
def _ref(entity_id: int):
    return mk_pm().get_player_by_entity_id(entity_id)


def mk_choices_mulligan(entity_id: int, cid: int, eids: list[int]) -> packets.Choices:
    p = packets.Choices(TS, _ref(entity_id), cid, 1, ChoiceType.MULLIGAN, 0, len(eids))
    p.choices = list(eids)
    return p


def mk_choices_general(entity_id: int, cid: int, eids: list[int]) -> packets.Choices:
    p = packets.Choices(TS, _ref(entity_id), cid, 2, ChoiceType.GENERAL, 1, len(eids))
    p.choices = list(eids)
    return p
```

- [ ] **Step 2: 失败测试 tests/test_store_blocks.py**

```python
"""GameStore 块与选择: PLAY 延迟/attack/trigger/fatigue/留牌/发现/to_dict。"""
from hearthstone.enums import BlockType, CardType, GameTag, Zone

from hsbot.carddb import CardDB
from hsbot.store import GameStore

from .conftest import (EventLog, mk_block, mk_choices_general, mk_choices_mulligan,
                       mk_create_game, mk_full, mk_pm, mk_send_general,
                       mk_send_mulligan, mk_show, mk_tag)


class _Tree:
    def __iter__(self):
        return iter(())


def _store():
    carddb = CardDB("/nonexistent/cards.json")
    st = GameStore(carddb=carddb, battletag="湫然#51704",
                   tree=_Tree(), player_manager=mk_pm())
    log = EventLog()
    st.subscribe(log)
    st.apply(mk_create_game())
    st.note_friendly(1)
    return st, log


def _heroes(st):
    st.apply(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    st.apply(mk_full(5, "HERO_02a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, HEALTH=30))


def test_play_deferred_until_block_settles():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(10, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    b = mk_block(BlockType.PLAY, 10)
    st.apply(b, depth=0)                    # 块开始: 身份未知, 挂起
    assert log.by_kind("play") == []
    st.apply(mk_show(10, "CS2_029", ZONE=Zone.HAND.value), depth=1)
    st.apply(mk_tag(10, GameTag.CARDTYPE, CardType.SPELL.value), depth=1)
    b.end()
    st.settle()
    plays = log.by_kind("play")
    assert plays and plays[0]["card_id"] == "CS2_029"
    assert plays[0]["is_power"] is False


def test_play_flushed_by_shallower_packet():
    st, log = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.CURRENT_PLAYER, 1))
    st.apply(mk_full(10, "CS2_029", ZONE=Zone.HAND.value, CONTROLLER=1))
    b = mk_block(BlockType.PLAY, 10)
    st.apply(b, depth=0)
    st.apply(mk_show(10, "CS2_029", ZONE=Zone.HAND.value), depth=1)
    st.apply(mk_tag(11, GameTag.ZONE, Zone.HAND.value), depth=0)   # 不更深 => 子树结束
    assert log.by_kind("play")


def test_attack_event():
    st, log = _store()
    _heroes(st)
    st.apply(mk_block(BlockType.ATTACK, 4, target=5))
    evt = log.by_kind("attack")[-1]
    assert evt["attacker_card_id"] == "HERO_01a" and evt["attacker_is_hero"]
    assert evt["target_is_hero"] and evt["actor"] == 1


def test_trigger_and_fatigue_events():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(30, "TTN_910", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2))
    st.apply(mk_block(BlockType.TRIGGER, 30))
    st.apply(mk_block(BlockType.FATIGUE, 3))
    assert log.by_kind("trigger")[-1]["card_id"] == "TTN_910"
    assert log.by_kind("fatigue")[-1]["actor"] == 2


def test_mulligan_flow():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(10, "OG_048", ZONE=Zone.DECK.value, CONTROLLER=1, COST=1))
    st.apply(mk_full(11, "GAME_005", ZONE=Zone.DECK.value, CONTROLLER=2))
    st.apply(mk_choices_mulligan(2, 7, [10]))              # 我方(entity2=pid1)起手
    st.apply(mk_send_mulligan(7, [10]))                    # 留下
    m = log.by_kind("mulligan")
    assert m and "留牌: OG_048" in m[0]["msg"]


def test_discover_flow():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(20, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_full(21, None, ZONE=Zone.DECK.value, CONTROLLER=1))
    st.apply(mk_choices_general(2, 9, [20, 21]))
    st.apply(mk_send_general(9, [21]))                     # 选 21, 20 置底
    evt = log.by_kind("discover")[-1]
    # 未揭示实体 cid_of 为 None -> 事件里回退保留实体号
    assert evt["picked"] == [21] and evt["bottom"] == [20]
    assert log.kinds().count("discover") == 1


def test_raw_records_unhandled_packets():
    """全量收录原则: 未解释的包记 raw(不渲染, 入库可查)。"""
    from hslog import packets as P

    from .conftest import TS

    st, log = _store()
    _heroes(st)
    st.apply(P.MetaData(TS, "DAMAGE", 0, 1))               # 未解释 -> raw
    st.apply(mk_block(BlockType.POWER, 4))                 # 未解释块 -> raw
    st.apply(mk_block(BlockType.PLAY, 4))                  # 已解释路径 -> 非 raw
    types = [u["packet_type"] for u in st.unhandled]
    assert "MetaData" in types and "Block:POWER" in types
    assert "Block:PLAY" not in types
    assert log.kinds().count("raw") >= 2


def test_gain_event_with_creator():
    st, log = _store()
    _heroes(st)
    st.apply(mk_full(15, "CS2_013", ZONE=Zone.PLAY.value, CONTROLLER=1))
    st.apply(mk_full(20, "TTN_001", ZONE=Zone.HAND.value, CONTROLLER=1, CREATOR=15))
    evt = log.by_kind("gain")[-1]
    assert evt["card_id"] == "TTN_001" and evt["creator"] == "CS2_013"


def test_to_dict_shape():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_full(10, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1))
    d = st.to_dict("turn_end")
    assert d["reason"] == "turn_end"
    assert set(d) == {"reason", "turn", "friendly_turn", "my_turn", "players", "me", "opp"}
    assert set(d["me"]) == {"hp", "armor", "mana", "deck", "hand", "board"}
    assert set(d["opp"]) == {"hp", "armor", "deck", "hand_n", "board"}
    assert d["me"]["hand"][0]["id"] == "CS2_029" and d["me"]["hand"][0]["pos"] == 0


def test_mana_text_projection():
    st, _ = _store()
    _heroes(st)
    st.apply(mk_tag(2, GameTag.RESOURCES, 2))
    st.apply(mk_tag(2, GameTag.OVERLOAD_OWED, 1))
    assert st.mana_text(1) == "水晶 2/2 (过载-1)"
```

- [ ] **Step 3: 跑测试确认失败**

```bash
python3 -m pytest tests/test_store_blocks.py -q
```

Expected: FAIL(`AttributeError: 'GameStore' object has no attribute '_on_block'` 一类)。

- [ ] **Step 4: 实现(store.py 追加;import 行改为)**

```python
from hearthstone.enums import (BlockType, CardType, ChoiceType, GameTag,
                               PlayState, Zone)
```

在 `GameStore` 内 `_emit_draw` 之后追加:

```python
    # ================= 块(spec §3.4 补全: TRIGGER/疲劳) =================
    def _on_block(self, p) -> None:
        btype = getattr(p, "type", None)
        if btype == BlockType.ATTACK and isinstance(p.entity, int):
            a = self.get(p.entity)
            t = self.get(p.target) if isinstance(p.target, int) else None
            self._emit_event({
                "kind": "attack",
                "actor": (self.ctrl_key(a) if a is not None else None),
                "attacker_card_id": self.cid_of(p.entity),
                "attacker_is_hero": a is not None and is_hero(a),
                "target_card_id": (self.cid_of(p.target)
                                   if isinstance(p.target, int) else None),
                "target_is_hero": t is not None and is_hero(t),
            })
        elif btype == BlockType.TRIGGER and isinstance(p.entity, int):
            e = self.get(p.entity)
            if (e is None or isinstance(e, Player)
                    or (self.game is not None and e is self.game)):
                return          # 玩家/游戏实体上的触发不单独报卡牌事件
            self._emit_event({"kind": "trigger", "actor": self.ctrl_key(e),
                              "card_id": e.card_id or self._hint_cid.get(e.id)})
        elif btype == BlockType.FATIGUE:
            self._emit_event({"kind": "fatigue", "actor": self._key_of(p.entity)})
        elif btype == BlockType.PLAY:
            pass                            # 延迟发由 apply 的挂起机制处理
        else:
            # POWER/DEATHS/JOUST/MOVE_MINION/SUB_SPELL 等块: 全量收录, 记 raw
            self._record_raw(f"Block:{getattr(btype, 'name', btype)}", p)

    def _flush_play(self) -> None:
        """PLAY 块子树结束时发出 —— 块内 SHOW_ENTITY 此时已揭示身份。"""
        _depth, p = self._pending_play
        self._pending_play = None
        eid = p.entity
        if not isinstance(eid, int):
            return
        e = self.get(eid)
        cid = self.cid_of(eid)
        actor = self.ctrl_key(e) if e is not None else None
        if actor is None:
            actor = self.current          # 出牌必然发生在行动方自己的回合
        if not cid or actor is None:
            return
        f = self.mana_fields(actor)
        mana_left = max(0, f["res"] + f["temp"] - f["used"])
        sub = getattr(p, "suboption", None)
        self._emit_event({"kind": "play", "card_id": cid, "actor": actor,
                          "cost_base": self.carddb.cost(cid),
                          "cost_tag": (e.tags.get(GameTag.COST) if e is not None else None),
                          "mana_left": mana_left,
                          "is_power": e is not None and is_hero_power(e),
                          "suboption": sub if isinstance(sub, int) and sub >= 0 else None})
        if actor == self.friendly_key and e is not None:
            # 通用模式判定只看"来自卡组"的牌: 衍生牌/硬币不算卡组不匹配
            if not is_generated(e) and not is_coin(cid):
                self.played_cids.add(cid)

    def mana_text(self, key: PlayerKey) -> str:
        """回合开始时的水晶投影: 上限+1−过载(RESOURCES 标签在切换之后才跳)。"""
        f = self.mana_fields(key)
        res, ol = f["res"], f["overload"]
        cap = max(0, min(10, res + 1 - ol))
        out = f"水晶 {cap}/{cap}"
        if ol:
            out += f" (过载-{ol})"
        return out

    # ================= 选择: 留牌 / 发现 =================
    def _on_choices(self, p) -> None:
        ctype = getattr(p, "type", None)
        if ctype == ChoiceType.MULLIGAN:
            key = getattr(getattr(p, "entity", None), "player_id", None)
            if key is None:
                return
            # 留牌发生在 exporter 友方探测之前 —— 用战网名立刻定主客
            if self.friendly_key is None and self.battletag:
                nm = getattr(p.entity, "name", None)
                if nm and (nm == self.battletag
                           or nm.split("#")[0] == self.battletag):
                    self.friendly_key = key
            self.mulligan[key] = MulliganState(offered=list(p.choices or []))
            self._choice_pid[p.id] = key
            names = []
            for eid in p.choices or []:
                cid = self.cid_of(eid)
                nm = self.carddb.name(cid) if cid else None
                if cid and is_coin(cid):
                    nm = (nm or "") + "(硬币)"
                names.append(nm or "?")
            if all(n == "?" for n in names):
                names = [f"第{i}张" for i in range(1, len(names) + 1)]
            self._emit_event({"kind": "text", "actor": key,
                              "msg": f"起手可留: {'、'.join(names)}"})
        elif ctype == ChoiceType.GENERAL:
            src = getattr(p, "source", None)
            pid = getattr(getattr(p, "entity", None), "player_id", None)
            self._discover[p.id] = {"offered": list(p.choices or []),
                                    "source": src if isinstance(src, int) else None,
                                    "pid": pid}
            self._choice_pid[p.id] = pid

    def _on_send_choices(self, p) -> None:
        ctype = getattr(p, "type", None)
        if ctype == ChoiceType.GENERAL:
            info = self._discover.get(p.id)
            if info is not None and p.id not in self._discover_emitted:
                self._discover_emitted.add(p.id)
                picked = [c for c in (p.choices or []) if isinstance(c, int)]
                bottom = [e for e in info["offered"] if e not in picked]  # 未选项按序置底
                src_cid = self.cid_of(info["source"]) if info.get("source") else None
                self._emit_event({"kind": "discover", "actor": info.get("pid"),
                                  "src_name": self.carddb.name(src_cid),
                                  "picked": [self.cid_of(c) or c for c in picked],
                                  "bottom": [self.cid_of(c) or c for c in bottom]})
        elif ctype == ChoiceType.MULLIGAN:
            self._mulligan_decide(p.id,
                                  [c for c in (p.choices or []) if isinstance(c, int)])

    def _on_chosen(self, p) -> None:
        if getattr(p, "type", None) == ChoiceType.MULLIGAN:
            self._mulligan_decide(p.id,
                                  [c for c in (p.choices or []) if isinstance(c, int)])

    def _mulligan_decide(self, cid: int, kept: list[int]) -> None:
        key = self._choice_pid.get(cid)
        if key is None:
            return
        m = self.mulligan.setdefault(key, MulliganState())
        m.kept = kept
        if key not in self._mulligan_emitted:
            self._mulligan_emitted.add(key)
            self._emit_event({"kind": "mulligan", "actor": key,
                              "msg": self.mulligan_text(key)})

    def mulligan_text(self, key: PlayerKey) -> str:
        m = self.mulligan.get(key) or MulliganState()
        offered, kept = m.offered, m.kept
        coins = {e for e in offered if is_coin(self.cid_of(e))}
        replaced = [e for e in offered if e not in kept and e not in coins]
        if key == self.friendly_key:
            kept_n = [(self.carddb.name(self.cid_of(e)) or "?") for e in kept]
            repl_n = [(self.carddb.name(self.cid_of(e)) or "?") for e in replaced]
            return (f"留牌: {'、'.join(kept_n) or '(无)'} │ "
                    f"换掉: {'、'.join(repl_n) or '(无)'}")
        if kept:
            return f"留牌 {len(kept)} 张 │ 换掉 {len(replaced)} 张(牌名不可见)"
        return f"起手 {len(offered)} 张(留牌细节未广播)"

    # ================= 导出(spec §3.5, JSONL 字段级兼容) =================
    def to_dict(self, reason: str = "") -> dict:
        me, opp = self.friendly_key, self.opponent_key()
        hand = [{"id": e.card_id, "name": None, "pos": zone_pos(e),
                 "cost": e.tags.get(GameTag.COST), "generated": is_generated(e)}
                for e in (self.hand(me) if me is not None else [])]
        return {
            "reason": reason,
            "turn": self.turn,
            "friendly_turn": self.friendly_turn_number(),
            "my_turn": self.is_my_turn(),
            "players": {k: {"name": self.name(k)} for k in self.player_keys()},
            "me": {
                "hp": self.hero_total_hp(me) if me is not None else 0,
                "armor": self.hero_armor(me) if me is not None else 0,
                "mana": self.mana_fields(me) if me is not None else {},
                "deck": self.deck_count(me) if me is not None else 0,
                "hand": hand,
                "board": [{"card_id": e.card_id, "atk": atk(e), "hp": hp_total(e),
                           "taunt": is_taunt(e)}
                          for e in (self.board(me) if me is not None else [])],
            },
            "opp": {
                "hp": self.hero_total_hp(opp) if opp is not None else 0,
                "armor": self.hero_armor(opp) if opp is not None else 0,
                "deck": self.deck_count(opp) if opp is not None else 0,
                "hand_n": len(self.hand(opp)) if opp is not None else 0,
                "board": [{"card_id": e.card_id, "atk": atk(e), "hp": hp_total(e),
                           "taunt": is_taunt(e)}
                          for e in (self.board(opp) if opp is not None else [])],
            },
        }
```

- [ ] **Step 5: 跑测试通过 + 全量回归 + Commit**

```bash
python3 -m pytest tests/ -q
git add hsbot/store.py tests/
git commit -m "feat: GameStore 块/选择/导出——PLAY延迟+TRIGGER/疲劳事件+留牌/发现+to_dict"
```

---

### Task 5: render/knowledge 换源(GameStore)

**Files:**
- Modify: `hsbot/render.py`、`hsbot/knowledge.py`
- Test: `tests/test_render_knowledge.py`

**Interfaces:**
- Consumes: Task 3/4 的查询 API 与模块助手。
- Produces: `chain_line(evt, carddb)` 不变;`snapshot_block(store: GameStore, led, *, knowledge, deck_name, generic, game_no, chain_lines, chain_summary, carddb, reason)`;`game_end_line(store)`;`DeckKnowledge.rebuild(store)`。
- ⚠️ 本任务完成到 Task 6 完成之间,CLI 运行路径暂不可用(渲染层已换源、watcher 未接);两任务须连续执行,中间不做真机验证。

- [ ] **Step 1: 失败测试 tests/test_render_knowledge.py**

```python
"""渲染与知识层换源: snapshot_block/game_end_line/rebuild 吃 GameStore。"""
from collections import Counter

from hearthstone.enums import CardType, GameTag, Zone

from hsbot.carddb import CardDB
from hsbot.knowledge import DeckKnowledge
from hsbot.render import chain_line, game_end_line, snapshot_block
from hsbot.store import GameStore

from .conftest import EventLog, mk_create_game, mk_full, mk_pm, mk_tag


class _Tree:
    def __iter__(self):
        return iter(())


def _store():
    carddb = CardDB("/nonexistent/cards.json")
    st = GameStore(carddb=carddb, battletag="湫然#51704",
                   tree=_Tree(), player_manager=mk_pm())
    st.subscribe(EventLog())
    st.apply(mk_create_game())
    st.note_friendly(1)
    return st, carddb


def _board(st):
    st.apply(mk_full(4, "HERO_01a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=1, HEALTH=30))
    st.apply(mk_full(5, "HERO_02a", CARDTYPE=CardType.HERO.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, HEALTH=30))
    st.apply(mk_full(10, "CS2_029", COST=2, ZONE=Zone.HAND.value, CONTROLLER=1,
                     ZONE_POSITION=1))
    st.apply(mk_full(30, "CS2_120", CARDTYPE=CardType.MINION.value,
                     ZONE=Zone.PLAY.value, CONTROLLER=2, ATK=2, HEALTH=3,
                     ZONE_POSITION=1))
    st.apply(mk_tag(2, GameTag.RESOURCES, 3))
    st.apply(mk_tag(2, GameTag.RESOURCES_USED, 1))


def test_snapshot_block_renders_from_store():
    st, carddb = _store()
    _board(st)
    block = snapshot_block(st, None, knowledge=None, deck_name="奇迹德",
                           generic=True, game_no=1, chain_lines=[],
                           chain_summary=[], carddb=carddb, reason="turn_end")
    assert "完整快照" in block and "回合T0" in block
    assert "我  " in block and "CS2_029(2费)" in block
    assert "对面" in block and "CS2_120(2/3)" in block


def test_game_end_line_and_chain_line_new_kinds():
    st, carddb = _store()
    _board(st)
    assert "对局结束" in game_end_line(st)
    assert "触发 CS2_029" == chain_line(
        {"kind": "trigger", "card_id": "CS2_029", "turn": 3, "friendly": 1}, carddb)
    assert "疲劳" in chain_line(
        {"kind": "fatigue", "actor": 2, "turn": 9, "friendly": 1}, carddb)
    assert "阵亡" in chain_line(
        {"kind": "death", "card_id": "CS2_120", "turn": 5, "friendly": 1}, carddb)


def test_knowledge_rebuild_from_store():
    st, _ = _store()
    _board(st)
    k = DeckKnowledge({"CS2_029": 2}, CardDB("/nonexistent/cards.json"), "测试")
    led = k.rebuild(st)
    assert led.in_hand == Counter({"CS2_029": 1})
    assert led.remaining == Counter({"CS2_029": 1})
    assert led.deck_actual == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_render_knowledge.py -q
```

Expected: FAIL(GameState 无 turn 等/签名不符)。

- [ ] **Step 3: 重写 hsbot/render.py(全文)**

```python
"""渲染层 —— 链路行 / 快照块 / 终局行(纯函数, 不持有状态)。

消费 GameStore(活引用只读); 输出格式与 M1 版一致, 新增 trigger/fatigue/death 行。
"""
from __future__ import annotations

from collections import Counter

from hearthstone.enums import GameTag

from .carddb import CardDB
from .knowledge import DeckKnowledge, Ledger
from .store import (GameStore, atk, hp_total, is_generated, is_taunt,
                    zone_pos)


def _side(actor, friendly) -> str:
    if actor is not None and actor == friendly:
        return "我"
    if friendly is not None and actor is not None:
        return "对面"
    return "?"


def chain_line(evt: dict, carddb: CardDB) -> str:
    head = f"[T{evt.get('turn', 0)}·{_side(evt.get('actor'), evt.get('friendly'))}]"
    kind = evt["kind"]
    if kind == "play":
        name = carddb.name(evt["card_id"])
        base, tag = evt.get("cost_base"), evt.get("cost_tag")
        if base is None and tag is None:
            cost = "(?费)"
        elif base is None:
            cost = f"(实付{tag}费)"
        elif tag is None or tag == base:
            cost = f"({base}费)"
        else:
            cost = f"({base}费→实付{tag})"
        sub = evt.get("suboption")
        if sub is not None:
            cost += f" 抉择{sub + 1}"
        left = evt.get("mana_left")
        left_txt = f" 剩{left}费" if left is not None else ""
        verb = "使用技能" if evt.get("is_power") else "打出"
        return f"{head} {verb} {name} {cost}{left_txt}"
    if kind == "discover":
        picked = "、".join(carddb.name(c) for c in evt.get("picked") or []) or "(无)"
        bottom = " → ".join(carddb.name(c) for c in evt.get("bottom") or []) or "(无)"
        return (f"{head} 发现({evt.get('src_name', '?')}): 取 {picked}"
                f" │ 置底(按序): {bottom}")
    if kind == "draw":
        return f"{head} 抽到 {carddb.name(evt['card_id'])}"
    if kind == "gain":
        creator_name = carddb.name(evt.get("creator")) if evt.get("creator") else None
        gen = f"[衍生·{creator_name}]" if creator_name and creator_name != "?" else "[衍生]"
        return f"{head} 入手 {carddb.name(evt['card_id'])} {gen}"
    if kind == "back_to_deck":
        return f"{head} 置入牌库 {carddb.name(evt['card_id'])}"
    if kind == "cost":
        return f"{head} 手牌费用变动 {carddb.name(evt['card_id'])} {evt['old']}→{evt['new']}费"
    if kind == "attack":
        def hero_txt(cid):
            n = carddb.name(cid)
            return f"英雄({n})" if n != "?" else "英雄"
        atk_txt = (hero_txt(evt.get("attacker_card_id")) if evt.get("attacker_is_hero")
                   else carddb.name(evt.get("attacker_card_id")))
        if evt.get("target_is_hero"):
            tgt = "敌方英雄" if evt.get("actor") == evt.get("friendly") else "我的英雄"
        else:
            tgt = carddb.name(evt.get("target_card_id"))
        return f"{head} 攻击 {atk_txt} → {tgt}"
    if kind == "shuffle":
        return f"{head} 牌库被洗牌(牌位重排)"
    if kind == "trigger":
        return f"{head} 触发 {carddb.name(evt.get('card_id'))}"
    if kind == "fatigue":
        return f"{head} 疲劳"
    if kind == "death":
        return f"{head} 阵亡 {carddb.name(evt.get('card_id'))}"
    return f"{head} {evt.get('msg', '')}"


def _fmt_counts(cnt: Counter, decklist: dict[str, int], carddb: CardDB,
                with_total: bool) -> str:
    parts = []
    for cid, n in sorted(cnt.items(), key=lambda kv: (-kv[1], carddb.name(kv[0]))):
        name = carddb.name(cid)
        parts.append(f"{name}{n}/{decklist[cid]}" if with_total else f"{name}{n}")
    return " · ".join(parts) if parts else "(无)"


def _board_txt(st: GameStore, key, carddb: CardDB) -> str:
    cells = []
    for e in st.board(key):
        t = "嘲讽" if is_taunt(e) else ""
        cells.append(f"{carddb.name(e.card_id)}({atk(e)}/{hp_total(e)}{t})")
    return " ".join(cells) if cells else "(无)"


_REASON_CN = {"turn_end": "回合结束", "game_end": "终局", "flush": "日志截断"}


def snapshot_block(st: GameStore, led: Ledger | None, *, knowledge: DeckKnowledge | None,
                   deck_name: str, generic: bool, game_no: int,
                   chain_lines: list[str], chain_summary: list[str],
                   carddb: CardDB, reason: str) -> str:
    me, opp = st.friendly_key, st.opponent_key()
    generic = generic or knowledge is None
    mode = st.meta.get("GameType", "?")
    fmt = st.meta.get("FormatType", "")
    reason_cn = _REASON_CN.get(reason, reason)
    lines: list[str] = []

    ft = st.friendly_turn_number() if me is not None else 0
    flag = " │ [通用模式: 非所选卡组]" if generic else ""
    lines.append(f"════════ 完整快照 ════════ 回合T{st.turn}(我的第{ft}回合·{reason_cn})"
                 f" │ 第{game_no}局 │ {mode}·{fmt}{flag}")

    if me is not None:
        hero = st.hero(me)
        hname = carddb.name(hero.card_id) if hero else "?"
        f = st.mana_fields(me)
        ol = f" (过载-{f['overload']})" if f["overload"] else ""
        lines.append(f"我  {st.name(me)} [{hname}] {st.hero_hp(me)}血{st.hero_armor(me)}甲"
                     f" │ 水晶 {st.mana_now(me)}/{f['res']} │ 牌库{st.deck_count(me)}"
                     f" │ 本回合已用{f['used']}费")
        lines.append(f"    下一回合: {st.mana_next_turn(me)}费{ol}")
        hs = []
        for i, e in enumerate(st.hand(me), 1):
            base = carddb.cost(e.card_id)
            tag = e.tags.get(GameTag.COST)
            cost = tag if tag is not None else (base if base is not None else "?")
            mark = "⬇" if (tag is not None and base is not None and tag < base) else ""
            gen = "[衍生]" if is_generated(e) else ""
            hs.append(f"{i}{carddb.name(e.card_id)}({cost}费{mark}){gen}")
        lines.append(f"    手牌{len(hs)}(位置序): {' '.join(hs) if hs else '(空)'}")
        lines.append(f"    场面: {_board_txt(st, me, carddb)}"
                     f"   墓地:{st.graveyard_count(me)}张 奥秘:{st.secret_count(me)}")

    if opp is not None:
        hero = st.hero(opp)
        hname = carddb.name(hero.card_id) if hero else "?"
        of = st.mana_fields(opp)
        opp_mana = (f" │ 水晶 {st.mana_now(opp)}/{of['res']}"
                    if st.current_key() == opp else
                    f" │ 下回合水晶 {st.mana_next_turn(opp)}/{of['res']}")
        lines.append(f"对面 {st.name(opp)} [{hname}] {st.hero_hp(opp)}血{st.hero_armor(opp)}甲"
                     f"{opp_mana} │ 牌库{st.deck_count(opp)} │ 手牌{len(st.hand(opp))}(不可见)")
        lines.append(f"    场面: {_board_txt(st, opp, carddb)}")

    if led is not None:
        lines.append("── 牌库序(已知部分) ──")
        top = (f"下一抽: {carddb.name(led.known_top)} [探底置顶]"
               if led.known_top else "下一抽: 未知")
        kb = " ".join(f"[{carddb.name(c)}·底{pos}]" for c, pos in led.known_bottom) or "无"
        lines.append(f"    {top}")
        lines.append(f"    底部已知: {kb}    中段: {led.unknown_middle}张未知")
        if led.known_unpositioned:
            kn = " ".join(f"[{carddb.name(c)}]" for c in led.known_unpositioned)
            lines.append(f"    已见·位置未知(换牌换回等): {kn}")

        if not generic:
            lines.append(f"── {deck_name or '卡组'}组件台账 ──")
            lines.append(f"  在手    : {_fmt_counts(led.in_hand, knowledge.decklist, carddb, True)}")
            lines.append(f"  已消耗  : {_fmt_counts(led.used, knowledge.decklist, carddb, True)}")
            lines.append(f"  牌库剩余: {_fmt_counts(led.remaining, knowledge.decklist, carddb, False)}"
                         f" (实际{led.deck_actual}张)")

    if chain_summary:
        lines.append(f"本回合链路(共{len(chain_summary)}张): {' → '.join(chain_summary)}")
    lines.append("════════════════════")
    return "\n".join(lines)


def game_end_line(st: GameStore) -> str:
    states = " / ".join(f"{st.name(k)}={st.playstate(k)}" for k in st.player_keys())
    return f"──── 对局结束 ──── {states}"
```

- [ ] **Step 4: 改 hsbot/knowledge.py(只动 import 与 rebuild 头部/字段访问)**

import 区:`from .gamestate import GameState` → `from .store import GameStore, is_generated, zone_pos`。
`rebuild` 方法替换为:

```python
    def rebuild(self, st: GameStore) -> Ledger:
        led = Ledger()
        me = st.friendly_key
        if me is not None:
            hand: Counter = Counter()
            used: Counter = Counter()
            for e in st.entities():
                if (st.ctrl_key(e) != me or not e.card_id
                        or e.card_id not in self.decklist
                        or is_generated(e)
                        or e.tags.get(GameTag.CARDTYPE) not in _LEDGER_TYPES):
                    continue
                if e.zone == Zone.HAND:
                    hand[e.card_id] += 1
                elif e.zone in (Zone.PLAY, Zone.GRAVEYARD, Zone.SECRET):
                    used[e.card_id] += 1
            led.in_hand = hand
            led.used = used
            led.remaining = Counter(self.decklist) - hand - used
            deck_ents = st.deck_entities(me)
            led.deck_actual = len(deck_ents)

            revealed = sorted((e for e in deck_ents if e.card_id),
                              key=zone_pos)
            if revealed and zone_pos(revealed[0]) == 1:
                led.known_top = revealed[0].card_id
            pos_known = [e for e in revealed if zone_pos(e) > 0]
            led.known_bottom = [(e.card_id, zone_pos(e)) for e in
                                sorted(pos_known, key=lambda e: -zone_pos(e))[:3]]
            # 换牌换回的牌位置随机(0/未知) —— 不能标成"牌库底"
            led.known_unpositioned = [e.card_id for e in revealed if zone_pos(e) == 0]
            led.unknown_middle = len(deck_ents) - len(pos_known)
        self.ledger = led
        return led
```

import 区需补 `from hearthstone.enums import CardType, GameTag, Zone`(原文件只 import 了 CardType, Zone)。

- [ ] **Step 5: 跑测试 + Commit**

```bash
python3 -m pytest tests/ -q
git add hsbot/render.py hsbot/knowledge.py tests/test_render_knowledge.py
git commit -m "refactor: render/knowledge 换源 GameStore, 新增 trigger/fatigue/death 渲染行"
```

---

### Task 6: watcher 整体重写 + gamestate.py 退役

**Files:**
- Modify(重写): `hsbot/watcher.py`
- Delete: `hsbot/gamestate.py`
- Modify: `hsbot/adapter.py`(删 `export_game_state` 与 gamestate import)
- Test: `tests/test_watcher_integration.py`

**Interfaces:**
- Consumes: 前面全部任务的产出。
- Produces: `Watcher(cfg, carddb, out=, hub=)` 签名不变(main.py 零改动);内部 `self.gs: GameStore`。

- [ ] **Step 1: 失败集成测试 tests/test_watcher_integration.py**

```python
"""端到端: fixture 日志 -> watcher 回放 -> 链路/快照/JSONL 输出。"""
import json
from pathlib import Path

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.watcher import Watcher

FIXTURE = Path(__file__).parent / "fixtures" / "mini_game.log"


def _run(tmp_path):
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path)})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    lines: list[str] = []
    w = Watcher(cfg, carddb, out=lines.append)
    w.run_replay(FIXTURE)
    return "\n".join(lines), tmp_path / "sessions"


def test_replay_produces_chain_snapshot_jsonl(tmp_path):
    text, sessions = _run(tmp_path)
    assert "新对局 #1" in text
    assert "完整快照" in text                      # 回合切换触发了快照
    assert "对局结束" in text                      # 终局行
    assert "快照导出失败" not in text              # 旧路径的失败模式必须消失
    assert "打出 CS2_029" in text                  # PLAY 块延迟发(fixture 友方未解析, 事件仍按 ctrl 发)
    assert "触发" in text                          # 新事件(spec §3.4)
    # 注: 三条抽牌路径均以 friendly 为门, fixture 未解析友方故无"抽到"行 ——
    # 抽牌路径由 test_store_core / test_store_blocks 单测覆盖
    jsonl = sorted(sessions.glob("*/game_001.jsonl"))
    assert jsonl, "JSONL 未落盘"
    payload = json.loads(jsonl[0].read_text(encoding="utf-8").splitlines()[0])
    assert payload["reason"] and payload["players"] and "me" in payload


def test_gamestate_module_deleted():
    import hsbot
    import importlib
    assert not (Path(hsbot.__file__).parent / "gamestate.py").exists()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python3 -m pytest tests/test_watcher_integration.py -q
```

Expected: FAIL(`快照导出失败` 出现或断言不满足;`test_gamestate_module_deleted` FAIL)。

- [ ] **Step 3: 重写 hsbot/watcher.py(全文)**

```python
"""监听层 —— tail / packet 游标 / FSM / 快照触发(spec 2026-09-07 §2-§3)。

对局状态全部在 GameStore(每局一个, 库驱动); 本层只做采集与输出:
括号行扫描喂 store.hint_*, 平铺游标逐包喂 store.apply, store 衍生事件
经订阅路由到渲染/落盘。PLAY 块延迟由 store 内部处理(apply 的 depth 语义)。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .adapter import (feed_line, game_meta, is_block, new_parser,
                      resolve_friendly)
from .carddb import CardDB
from .config import Config
from .knowledge import DeckKnowledge, parse_decks_log
from .persist import SessionStore
from .render import chain_line, game_end_line, snapshot_block
from .store import GameStore

_CREATE_GAME_MARK = "GameState.DebugPrintPower() - CREATE_GAME"
# 日志行括号兜底(hslog 解析不到的): id=...cardId= / id=...player= 温和匹配(不跨下一个 id=, 兼容嵌套括号)
_BRACKET_CID_RE = re.compile(r"id=(\d+)(?:(?!id=)[^\n])*?cardId=([A-Za-z0-9_]+)")
_BRACKET_PLAYER_RE = re.compile(r"id=(\d+)(?:(?!id=)[^\n])*?player=(\d+)")
# hslog 会跳过"括号形式"的 SHOW_ENTITY 行(抽牌揭示的主形态), 行级直接补抽牌事件
_SHOW_DRAW_RE = re.compile(r"id=(\d+)[^\n]*?zone=DECK[^\n]*?player=(\d+)[^\n]*?CardID=([A-Za-z0-9_]+)")


def _walk(node):
    """DFS 前序平铺 packet 树(追加-only, 前缀稳定), 产出 (packet, depth)。"""
    def rec(n, depth):
        for p in n.packets:
            yield p, depth
            if is_block(p):
                yield from rec(p, depth + 1)
    yield from rec(node, 0)


class Watcher:
    def __init__(self, cfg: Config, carddb: CardDB, out=None, hub=None) -> None:
        self.cfg = cfg
        self.carddb = carddb
        self.out = out if out is not None else print
        self.hub = hub

        self.parser = new_parser()
        self.lines = 0
        self.game_count = 0          # 已见过的局数 = len(parser.games)
        self.cursor = 0
        self.mute = False            # 实时追平历史时静音
        self.state = "IDLE"          # IDLE / IN_GAME / GAME_END

        self.game_no = 0
        self.store: SessionStore | None = None   # 持久化(勿与 GameStore 混淆)
        self.gs: GameStore | None = None         # 对局状态仓(每局一个)
        self.knowledge: DeckKnowledge | None = None
        self.generic = False
        self.chain: list[str] = []           # 本回合链路行(输出缓冲, 非游戏状态)
        self.summary: list[str] = []         # 本回合打出的卡名
        self.session_name = ""
        self._live = False                   # 实时来源(回放不自动导训练样本)
        self._corpus = None
        self._title_done = False             # 标题防重发(显示态)
        self._pend_cid: dict[int, str] = {}  # 括号线索缓冲(store 建立前先攒着)
        self._pend_ctrl: dict[int, int] = {}
        self.decks_path: Path | None = None

    # ================= 源 =================
    REPLAY_CHUNK = 40  # 回放批次: 状态新鲜度 vs 速度折衷(实时模式批次天然极小)

    def run_replay(self, path: str | Path) -> None:
        path = Path(path)
        self.parser = new_parser()
        self.decks_path = path.parent / "Decks.log"
        self.session_name = f"replay_{path.parent.name}"
        self.store = SessionStore(self.cfg.sessions_dir)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        # 按 GameState CREATE_GAME 切段, 段内小批量交错"喂入/处理",
        # 保证快照与链路事件反映的是该时刻的状态(而非整局终局)
        marks = [i for i, l in enumerate(lines) if _CREATE_GAME_MARK in l]
        segs = marks + [len(lines)]
        for s, e in zip(segs, segs[1:]):
            for i in range(s, e, self.REPLAY_CHUNK):
                self._feed_many(lines[i:min(i + self.REPLAY_CHUNK, e)])  # 块尾夹到段边界, 防重复喂入
                self._after_batch()
            if self.parser.games:
                self._process_tree(self.parser.games[-1], flush=True)
        if self.state == "IN_GAME":
            self._snapshot("flush")

    def run_live(self) -> None:
        """实时模式: 会话目录按名字排序取最新(字典序=时间序, 与 mtime 无关)。

        支持两种启动顺序:
          * 游戏先开: 直接追平当前会话;
          * bot 先开: 挂在空/不存在的 Power.log 上等待, 游戏启动后自动接管。
        """
        self.store = SessionStore(self.cfg.sessions_dir)
        session: Path | None = None
        log_path: Path | None = None
        offset = 0
        waiting_msg_shown = False
        last_session_check = 0.0
        self._live = True

        while True:
            time.sleep(self.cfg.poll_interval)
            now = time.monotonic()

            # --- 会话发现与切换 ---
            if now - last_session_check >= self.cfg.session_check_interval:
                last_session_check = now
                latest = self._find_latest_session()
                if latest is not None and latest != session:
                    first = session is None
                    session = latest
                    self.session_name = session.name
                    log_path = session / "Power.log"
                    self.decks_path = session / "Decks.log"
                    offset = self._attach(log_path)
                    if first:
                        self._emit(f"监控会话: {session.name} │ {self.cfg.summary()}")
                    else:
                        self._emit(f"!! 切换到新会话: {session.name}")
                    if not log_path.exists() and not waiting_msg_shown:
                        waiting_msg_shown = True
                        self._emit("   该会话还没有 Power.log, 等待对局开始...")

            # --- 增量读取 ---
            if log_path is None:
                continue
            try:
                size = log_path.stat().st_size
            except OSError:
                continue           # Power.log 尚未创建(游戏还没开第一局)
            if size < offset:      # 日志被客户端轮转/截断
                offset = self._attach(log_path)
                continue
            if size == offset:
                continue
            with open(log_path, "rb") as fp:
                fp.seek(offset)
                raw = fp.read()
            offset += len(raw)
            text = self._partial + raw.decode("utf-8", errors="replace")
            if text and not text.endswith("\n"):
                cut = text.rfind("\n") + 1
                self._partial = text[cut:]
                text = text[:cut]
            else:
                self._partial = ""
            if text:
                self._feed_many(text.splitlines())
                self._after_batch()

    _partial = ""

    def _find_latest_session(self) -> Path | None:
        """最新会话目录。目录名 Hearthstone_YYYY_MM_DD_HH_MM_SS, 字典序即时间序,
        不依赖文件系统 mtime(目录 mtime 不随 Power.log 追加更新, 不可靠)。"""
        if not self.cfg.logs_dir.exists():
            return None
        dirs = sorted(self.cfg.logs_dir.glob("Hearthstone_*"), key=lambda d: d.name,
                      reverse=True)
        return dirs[0] if dirs else None

    def _attach(self, log_path: Path) -> int:
        """从头解析当前文件追平历史, 追平前静音(不把历史当实时)。
        文件尚不存在时直接返回 0(等待创建)。"""
        self.parser = new_parser()
        self.game_count = 0
        self.cursor = 0
        self.state = "IDLE"
        self._partial = ""
        self.mute = True
        offset = 0
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            for i in range(0, len(lines), 5000):
                self._feed_many(lines[i:i + 5000])
                self._after_batch()
            if self.state == "IN_GAME":
                self._snapshot("flush")
            offset = len(text.encode("utf-8"))
        self.mute = False
        return offset

    # ================= 消费 =================
    def _feed_many(self, lines) -> None:
        for line in lines:
            self.lines += 1
            if self.gs is not None:
                self.gs.lines_consumed = self.lines
            if "Entity=[" in line:            # hslog 丢弃括号里的 cardId/player, 这里兜底
                if "cardId=" in line:
                    for m in _BRACKET_CID_RE.finditer(line):
                        if m.group(2):
                            self._pend_cid[int(m.group(1))] = m.group(2)
                if "player=" in line:
                    for m in _BRACKET_PLAYER_RE.finditer(line):
                        self._pend_ctrl[int(m.group(1))] = int(m.group(2))
            feed_line(self.parser, line)
            # 括号形式的 SHOW_ENTITY(抽牌揭示主形态) hslog 不解析 —— 行级补抽牌事件
            if (self.gs is not None and "SHOW_ENTITY" in line
                    and "zone=DECK" in line and "GameState." in line):
                m = _SHOW_DRAW_RE.search(line)
                if (m and self.gs.friendly_key is not None
                        and int(m.group(2)) == self.gs.friendly_key):
                    self.gs.hint_draw(int(m.group(1)), m.group(3), int(m.group(2)))

    def _after_batch(self) -> None:
        self._detect_game()
        if self.gs is not None:               # 括号线索刷进状态仓(幂等)
            for eid, cid in self._pend_cid.items():
                self.gs.hint_cid(eid, cid)
            for eid, pid in self._pend_ctrl.items():
                self.gs.hint_ctrl(eid, pid)
        if self.parser.games:
            self._process_tree(self.parser.games[-1])
        if self.gs is not None and self.gs.friendly_key is None:
            self.gs.note_friendly(resolve_friendly(self.parser, self.cfg.battletag))
        if (self.gs is not None and not self._title_done
                and self.gs.friendly_key is not None
                and self.gs.current == self.gs.friendly_key
                and self.state == "IN_GAME"):
            # 先手局: 留牌阶段friendly才解析出来, 而第1回合已在进行 —— 补发标题
            self._title_done = True
            self._emit(f"──── 第{self.game_no}局 · 我的第1回合开始 (T1) │ "
                       f"{self.gs.mana_text(self.gs.friendly_key)} ────")

    def _detect_game(self) -> None:
        n = len(self.parser.games)
        while self.game_count < n:
            if self.game_count > 0:      # 把上一局尾部事件冲完再切
                self._process_tree(self.parser.games[self.game_count - 1], flush=True)
            self.game_count += 1
            self._new_game()

    def _new_game(self) -> None:
        self.game_no += 1
        self.cursor = 0
        self.generic = False
        self.chain = []
        self.summary = []
        self._title_done = False
        self._pend_cid = {}
        self._pend_ctrl = {}
        self.state = "IN_GAME"
        self.gs = GameStore(carddb=self.carddb, battletag=self.cfg.battletag,
                            tree=self.parser.games[-1] if self.parser.games else None,
                            player_manager=self.parser.player_manager)
        self.gs.subscribe(self._route)
        if self.store is not None:
            self.store.new_game()
            if self.hub is not None:      # 悬浮窗内容同步落盘(本地记录)
                self.hub.set_file(self.store.path.with_suffix(".log"))
        self.knowledge = self._load_knowledge()
        note = "" if self.knowledge else " (Decks.log 中未找到卡组代码, 通用模式)"
        self._emit(f"── 新对局 #{self.game_no} ── 所选卡组: {self.cfg.deck_name}{note}")

    def _load_knowledge(self) -> DeckKnowledge | None:
        code = self.cfg.deck_code
        if not code and self.decks_path is not None:
            code = parse_decks_log(self.decks_path).get(self.cfg.deck_name)
        if not code:
            return None
        k = DeckKnowledge.from_code(code, self.carddb, self.cfg.deck_name)
        try:
            import json as _json
            out = self.cfg.data_dir / "decks" / "decklist.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_json.dumps({"name": self.cfg.deck_name, "code": code,
                                        "cards": k.decklist}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        except OSError:
            pass
        return k

    def _process_tree(self, tree, flush: bool = False) -> None:
        if self.gs is None:
            return
        flat = list(_walk(tree))
        i = self.cursor
        # 扣留最后一个包: FULL/SHOW_ENTITY 的子标签(CONTROLLER/ZONE/...)在包注册之后
        # 才陆续到达, 立即处理会拿到空标签。留到下一批(flush 时全量)再处理。
        limit = len(flat) if flush else max(self.cursor, len(flat) - 1)
        while i < limit:
            pkt, depth = flat[i]
            try:
                self.gs.apply(pkt, depth)
            except Exception as exc:  # noqa: BLE001  单包事件失败不拖垮监控
                self._emit(f"! 事件处理异常: {type(exc).__name__}: {exc}")
            i += 1
        self.cursor = limit
        self.gs.settle()               # 块已收口的挂起 PLAY 立即发出

    # ================= store 事件路由(命令式外壳) =================
    def _route(self, evt: dict) -> None:
        kind = evt.get("kind")
        if kind == "turn_start":
            friendly = self.gs.friendly_key if self.gs else None
            if evt["first"]:
                if evt["actor"] == friendly:
                    self._title_done = True
                    self._emit(f"──── 第{self.game_no}局 · 我的第1回合开始 (T1) │ "
                               f"{self.gs.mana_text(evt['actor'])} ────")
                return
            if friendly is not None and evt["actor"] == friendly:
                n, total = evt["my_turn_no"], evt["total_turn"]
                self._title_done = True
                self._emit(f"──── 第{self.game_no}局 · 我的第{n}回合开始 (T{total}) │ "
                           f"{self.gs.mana_text(evt['actor'])} ────")
            else:
                self._snapshot("turn_end")
            return
        if kind == "game_end":
            self.state = "GAME_END"
            self._snapshot("game_end")
            self._export_training()
            return
        if kind == "raw":
            return            # 全量收录: 已入 store.unhandled(随 JSONL 落盘), 不渲染
        if (kind == "play" and self.gs is not None
                and evt.get("actor") == self.gs.friendly_key):
            self.summary.append(self.carddb.name(evt["card_id"]))
        line = chain_line(evt, self.carddb)
        self.chain.append(line)
        self._emit(line)

    def _export_training(self) -> None:
        """每局结束自动导出训练样本(仅实时来源; 回放用 --import-all 批量做)。"""
        if not (self.cfg.auto_training and self._live and self.parser.games):
            return
        try:
            if self._corpus is None:
                from .corpus import CorpusExporter
                self._corpus = CorpusExporter(self.cfg, self.carddb)
            path = self._corpus.export_game(
                self.parser.games[-1], session=self.session_name or "live",
                idx=self.game_no, decks_path=self.decks_path, source="live")
            if path:
                self._emit(f"训练样本已导出: {path}")
        except Exception as exc:  # noqa: BLE001
            self._emit(f"! 训练样本导出失败: {type(exc).__name__}: {exc}")

    def _snapshot(self, reason: str) -> None:
        if self.gs is None:
            return
        self.gs.meta = game_meta(self.parser)
        if self.knowledge is not None \
                and self.knowledge.mismatch_count(self.gs.played_cids) >= 2:
            self.generic = True
        led = self.knowledge.rebuild(self.gs) if self.knowledge else None
        block = snapshot_block(
            self.gs, led, knowledge=self.knowledge, deck_name=self.cfg.deck_name,
            generic=self.generic, game_no=self.game_no, chain_lines=self.chain,
            chain_summary=self.summary, carddb=self.carddb, reason=reason)
        if reason == "game_end":
            block += "\n" + game_end_line(self.gs)
        self._emit(block)
        if self.store is not None:
            payload = self.gs.to_dict(reason)
            if led is not None:
                payload["ledger"] = {
                    "in_hand": dict(led.in_hand), "used": dict(led.used),
                    "remaining": dict(led.remaining), "deck_actual": led.deck_actual,
                    "known_top": led.known_top, "known_bottom": led.known_bottom,
                }
            payload["chain_summary"] = self.summary
            if self.gs.unhandled:
                payload["unhandled"] = self.gs.unhandled[-50:]   # 全量收录台账
            self.store.write_snapshot(payload)
        self.chain, self.summary = [], []

    def _emit(self, msg: str) -> None:
        if not self.mute:
            self.out(msg)
```

- [ ] **Step 4: 删 gamestate.py + 清理 adapter**

```bash
git rm hsbot/gamestate.py
```

adapter.py 中删除:`from .gamestate import Entity, GameState, PlayerInfo, PlayerKey` 整行、`export_game_state` 函数整体;文件头 docstring 改为:

```python
"""适配层 —— hslog/hearthstone 解析类唯一入口(DESIGN.md §3 铁律, 2026-09-07 spec 修订)。

行→packet(LogParser); StoreExporter = 库状态机 + 挂钩(GameStore 用);
友方探测; 语料导出用的 packet 归一化。
"""
```

- [ ] **Step 5: 全量测试 + 冒烟 + Commit**

```bash
python3 -m pytest tests/ -q
python3 -m hsbot --replay tests/fixtures/mini_game.log --no-overlay --data-dir /tmp/hsbot_smoke 2>&1 | head -30
git add -A
git commit -m "refactor!: watcher 重写为采集外壳, GameStore 唯一状态权威; gamestate.py 退役"
```

Expected: 测试全绿;冒烟输出链路行 + 快照块,无"快照导出失败"。

---

### Task 7: 等价验收(基线 diff + 新事件抽查)

**Files:**
- Create: `data/baseline_new/`(验收产物,不入 git 可留本地)

**Interfaces:**
- Consumes: Task 1 的 `data/baseline/mini_game.txt` 与 `scripts/replay_capture.py`。

- [ ] **Step 1: 新架构采集同一 fixture**

```bash
python3 scripts/replay_capture.py tests/fixtures/mini_game.log -o data/baseline_new --data-dir /tmp/hsbot_new
```

Expected: 无"快照失败",退出码 0。

- [ ] **Step 2: 语义 diff——剥离新增事件行后要求零差异**

```bash
python3 - <<'EOF'
import difflib, re, sys
from pathlib import Path

NEW_KIND = re.compile(r"(触发 |疲劳$|阵亡 )")   # spec §3.4 新增事件的渲染行

def norm(p):
    return [l.rstrip() for l in Path(p).read_text(encoding="utf-8").splitlines()
            if l.strip() and not NEW_KIND.search(l.strip())]

old = norm("data/baseline/mini_game.txt")
new = norm("data/baseline_new/mini_game.txt")
if old == new:
    print("既有输出零差异 ✓")
    sys.exit(0)
print("\n".join(difflib.unified_diff(old, new, "old", "new", lineterm="")))
sys.exit(1)
EOF
```

Expected: `既有输出零差异 ✓`。有差异 → 用 systematic-debugging 定位是哪条 packet 路径发散,修复后重跑;**不许改基线**。

- [ ] **Step 3: 新事件抽查**

```bash
grep -c "触发 " data/baseline_new/mini_game.txt   # >= 1
grep -c "阵亡" data/baseline_new/mini_game.txt    # >= 0(fixture 可能无死亡, 允许 0)
```

- [ ] **Step 4: JSONL 结构对照(字段级兼容)**

```bash
python3 - <<'EOF'
import json, sys
from pathlib import Path

def first_snap(root_glob):
    files = sorted(Path(root_glob).glob("*/game_001.jsonl"))
    assert files, "找不到 game_001.jsonl"
    return json.loads(files[-1].read_text(encoding="utf-8").splitlines()[0])

old = first_snap("data/sessions")          # Task 1 基线采集时写入的(时间戳最早那个)
new = first_snap("/tmp/hsbot_new/sessions")
# 兼容 = 旧键全在(新键只增: 本次新增 unhandled, 全量收录台账)
assert set(old) <= set(new), f"旧顶层键丢失: {set(old) - set(new)}"
assert set(old["me"]) == set(new["me"]) and set(old["opp"]) == set(new["opp"])
print("JSONL 字段级兼容 ✓")
EOF
```

注意:Task 1 基线运行与本次运行都在 `data/sessions/` 留了时间戳目录,`files[-1]` 取的是**最新**的——若中间插了别的运行,改用 `files[0]` 取 Task 1 那份。预期 `✓`。

- [ ] **Step 5: 真实日志验收(在有 Power.log 的机器上执行,本地无原始日志)**

真实会话日志在游戏机(Windows, `E:\battle\Hearthstone\Logs`)。在那台机器:

```bash
# 1) 更新代码前, 先在旧 HEAD 采集基线(若同步过仓库)
git log --oneline -1                      # 记下当前提交
python scripts/replay_capture.py "E:\battle\Hearthstone\Logs\Hearthstone_<最新会话>\Power.log" -o data\baseline_real --data-dir C:\temp\hsbot_old
# 2) 拉取新代码后
python scripts/replay_capture.py "E:\battle\Hearthstone\Logs\Hearthstone_<最新会话>\Power.log" -o data\baseline_real_new --data-dir C:\temp\hsbot_new
# 3) 用 Step 2 的语义 diff 脚本比对两份输出(改两个路径)
```

Expected: 剥离新增事件行后零差异。这是 spec §6.1 的"既有事件逐条核对"在真实数据上的执行。

- [ ] **Step 6: 真机人工验收(M1_MONITOR §6)**

打一局真实对局,悬浮窗/控制台人工核对:血/甲、水晶(含过载)、手牌名与位置、牌库数、场面攻血/嘲讽、终局行;确认回合开始生效的卡牌效果出现"触发"链路行。

- [ ] **Step 7: Commit(验收产物不入库,只记录通过)**

```bash
git commit --allow-empty -m "test: GameStore 等价验收通过(fixture 零差异+JSONL 兼容+真实日志)"
```

---

### Task 8: 文档同步(文档不留谎言)

**Files:**
- Modify: `docs/DESIGN.md`、`docs/M1_MONITOR.md`、`docs/superpowers/specs/2026-09-07-gamestore-design.md`

- [ ] **Step 1: DESIGN.md 修订(先读对应章节再替换)**

§3 铁律(原文"adapter 是全项目唯一允许 import hslog/hearthstone 实体类的模块")替换为:

```markdown
- **adapter 是全项目唯一允许 import hslog 解析/导出类的模块**(LogParser、packets、
  exporter 及其子类)。`hearthstone.enums` 与 `hearthstone.entities` 是全项目共享的
  状态模型与枚举;hsbot 的实体状态就维护在 hearthstone.entities 上(库驱动,不造轮子)。
```

§3.1 状态层:开头的"hslog 实体树 → 协议化快照 GameState(Entity=标签桶)"改为
"packet 直投 GameStore(每局一个,库驱动唯一权威);GameState 协议层退役,导出为
`store.to_dict()`(JSONL 字段级兼容)"。

§4.2 主循环:`EntityTreeExporter 全量导出实体树(一局内毫秒级,不做真增量)` 节点改为
`平铺游标逐包喂 GameStore.apply(库 exporter 维护 hearthstone.entities)`。

§5 模式表:备忘录行 `GameState.deepcopy() + JSONL 快照` 改为
`store.to_dict() JSONL + packet 前缀重放(任意历史状态可重建)`。
新增一行:观察者 —— `store.subscribe(回调)` 链路事件由状态迁移衍生(渲染/M2 同轨)。

- [ ] **Step 2: M1_MONITOR.md 修订**

§3 设计要点,替换两个要点为:

```markdown
- **库驱动唯一状态权威**:packet 平铺游标逐包喂 `GameStore.apply()`,实体/标签/区域
  由 adapter.StoreExporter(hslog EntityTreeExporter 容错子类)维护在
  hearthstone.entities 上;shadow 表/`_mana` 等散装字典全部退役。
- **链路事件 = 状态迁移的衍生品**:apply 挂钩对比新旧标签衍生抽牌/出牌/血甲水晶/区域
  事件;PLAY 块延迟到子树结束再发;TRIGGER/疲劳/死亡为补全收录(§2 表)。
```

§2 事件链路表追加三行:

```markdown
| 回合开始生效的卡牌效果 | `BLOCK_START BlockType=TRIGGER Entity=<卡>` | Block(TRIGGER) | 链路行"触发 <卡名>" |
| 疲劳 | `BLOCK_START BlockType=FATIGUE Entity=<玩家>` | Block(FATIGUE) | 链路行"疲劳" |
| 随从死亡 | `TAG_CHANGE tag=ZONE value=GRAVEYARD`(自 PLAY) | TagChange(ZONE) | 链路行"阵亡 <卡名>" |
```

- [ ] **Step 3: spec §3.2 回写入口细化**

在 spec §3.2 代码块后追加一行注记:

```markdown
> 实现细化(2026-09-08 计划采纳):`apply_block_end(block)` 落地为
> `apply(p, depth)`(游标给深度, 遇不深于挂起 PLAY 的包先冲刷)+ `settle()`(批尾冲刷
> ended 块);另补 `hint_draw`(行级抽牌直通)与 `note_friendly`(友方探测回填)。
```

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs: 同步 GameStore 重构——DESIGN/M1_MONITOR 铁律与状态层修订, spec 入口细化回写"
```

---

## 验收清单(spec §6 对应)

1. `python3 -m pytest tests/ -q` 全绿(unit:构造器护栏/exporter 挂钩/store 核心/块与选择/渲染换源/端到端)
2. fixture 语义 diff 零差异(剥离新增事件行)
3. JSONL 顶层与 me/opp 键集与旧版一致
4. 真实日志(Windows 机器)语义 diff 零差异
5. 真机一局人工核对(M1 §6)
6. DESIGN.md / M1_MONITOR.md / spec 已同步

## Self-Review(写计划时已完成)

- **Spec 覆盖**:§2.2 轮子清单→Task 2/3;§3.1 字段→Task 3/4;§3.2 入口→Task 3;§3.3 查询→Task 3;
  §3.4 事件+补全→Task 3/4/5;§3.5 导出→Task 4;§3.6 生命周期→Task 6;§4 M3 边界→无任务(范围外,
  由 store 查询 API 形态保证);§5 文件表→Tasks 2-6;§6 验收→Tasks 1/7;文档同步→Task 8。
- **环境事实**:hslog 1.18 的 `tolerate_missing_entities` TypeError 已核实,Task 0 处理并设停机条件。
- **已知取舍**:`已揭示实体再次抽到`(DECK→HAND 无 SHOW_ENTITY)路径受 0.5s 抽牌去重影响无法在
  快速单测中复现,由真实日志语义 diff 兜底;集成 fixture 不含留牌行(Choices 行格式风险),
  留牌由构造器单测覆盖。




