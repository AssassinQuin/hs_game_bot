"""状态层 —— 每局一个 GameStore: 唯一可变状态权威(spec 2026-09-07 §3)。

纯状态职责: 只维护实体/区域/标签与游戏快照查询, 不做卡牌文本/效果解释
(那属于 analysis 解析层), 更不做输出格式化(那属于 render 层)。

实体/标签/区域由 adapter.StoreExporter(hslog EntityTreeExporter 容错子类)维护在
hearthstone.entities 上(不造轮子); store 只追踪库没有的部分(留牌/发现/PLAY延迟/
回合计数/括号线索), 并从状态迁移衍生链路事件。

变更入口(spec §3.2 细化): apply(p, depth) / settle() / note_friendly(pid)
/ apply_hints(cid, ctrl) / hint_draw(eid, cid, actor) / note_progress(n)
/ set_meta(meta) —— 之外无人可改状态。
契约: 查询返回活引用只读; 单线程(watcher 轮询线程独占);
订阅者同步调用、不得重入 apply。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator

from hearthstone.enums import (BlockType, CardType, ChoiceType, GameTag,
                               PlayState, Zone)

from .adapter import is_play_block, new_store_exporter, packet_payload
from .carddb import CardDB
from .consts import (PET_CARD_PREFIX, UNKNOWN_HUMAN_PLAYER,
                     TAG_START_OF_GAME_KEYWORD, TAG_PREPARE, TAG_PREPARING,
                     is_coin)

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


@dataclass
class MulliganState:
    offered: list = field(default_factory=list)
    kept: list = field(default_factory=list)
    decided: bool = False               # 对手不广播决定 → kept 有值即 decided
    replaced_in: list = field(default_factory=list)  # 换入的牌(决定后、窗口关闭前抽到)
    closed: bool = False                # 换入窗口关闭: 补牌凑满(数量闸)或首回合开始
    expected_in: int | None = None      # 应补张数=换掉张数(留牌决定时定); None=未决定


class GameStore:
    """每局一个; 由 Watcher._new_game 创建, 局终丢弃(spec §3.6)。"""

    def __init__(self, *, carddb: CardDB, battletag: str = "",
                 tree=None, player_manager=None,
                 draw_dedup: float = 0.5) -> None:
        self.carddb = carddb
        self.battletag = battletag
        self.exporter = new_store_exporter(tree, player_manager)
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
        self.last_discover: dict | None = None   # 我方最近一次发现 {source/picked/unpicked}
        self._pending_play: tuple[int, object] | None = None
        self._pending_cost: int | None = None   # 打出块开始时的真实手牌价
        self._deferred: list[dict] = []         # 挂起 PLAY 期间的事件(块尾跟在 play 之后冲出)
        self._ctx_trigger: tuple[int, int] | None = None   # 所在触发块 (depth, 实体号)
        self._pending_prepare: tuple[int, object, dict, int] | None = None
        # 预备块内暂存的"预备完成" (depth, 块包, 事件, 宿主实体号), 等 PREPARING
        # 翻转时随"触发 正在预备"按因果序一起发(见 _on_preparing_start)
        self._prepare_trigger_eid: int | None = None
        # 本回合进战场的实体号(召唤失调判定: 无冲锋则打不了脸; 回合开始清空。
        # JUST_PLAYED 标签的区转兜底信号, 审计 2026-09-14 高#2)
        self._entered_play: set[int] = set()
        # 已按块结构提前报过的"正在预备"触发实体号: 尾随约1秒的真身 TRIGGER 块去重
        self.hero_power: dict[PlayerKey, int] = {}      # pid → 当前技能实体号
        self.hero_power_cid: dict[PlayerKey, str] = {}  # pid → 当前技能 card_id
        self.player_names: dict[PlayerKey, str] = {}   # 行级采集的真名(覆盖 manager)
        self._hint_cid: dict[int, str] = {}
        self._hint_ctrl: dict[int, PlayerKey] = {}
        self._draw_ts: dict[int, float] = {}
        self.stolen_eids: set[int] = set()   # 本体被夺(我方→对手)的实体号
        self._draw_dedup = draw_dedup       # 同实体多重揭示路径的去重窗口(秒)
        self._ent2pid: dict[int, PlayerKey] = {}
        self._ended = False
        self._play_offer_due = False    # 我方打出后批尾重发 play_offer 的挂起标记
        self._subs: list[EventCb] = []
        self.unhandled: deque = deque(maxlen=500)   # raw 台账(spec §3.4 全量收录, 上限 500)

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
        nm = self.player_names.get(key)
        if nm:
            return nm
        g = self.game
        if g is not None:
            p = g.get_player(key)
            if p is not None:
                pname = getattr(p, "name", None)
                if pname and pname != UNKNOWN_HUMAN_PLAYER:
                    return pname
        return f"Player{key}"

    def note_player_name(self, key: PlayerKey, name: str) -> None:
        """watcher 行级采集的真名(开局时对手常是 UNKNOWN HUMAN PLAYER,
        真名在后续行才出现, manager 不一定收录)。"""
        if key is not None and name:
            self.player_names[key] = name   # 与旧 adapter 的无名兜底一致(基线终局行 Player1=...)

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

    def board_attack(self, key: PlayerKey) -> int:
        """场面总攻击力(快照行"对面场攻"/杀伤预估用)。"""
        return sum(atk(e) for e in self.board(key))

    def board_face_attack(self, key: PlayerKey) -> int:
        """可直击脸的场攻(斩杀线口径, 宁漏勿错; 审计 2026-09-14 高#2)。

        敌方场上有嘲讽随从 → 0(必须先解嘲, 无法证明能过墙就当过不去);
        否则只计"确定可攻击"的我方随从攻击力, 剔除:
          冻结(FROZEN) / 本回合已攻击(NUM_ATTACKS_THIS_TURN) /
          本回合进战场且无冲锋(区转信号 _entered_play 或 JUST_PLAYED 标签)。
        标签缺失按不可攻击计 —— 漏报方向安全。"""
        for opp in (p for p in self.player_keys() if p != key):
            if any(e.tags.get(GameTag.TAUNT) for e in self.board(opp)):
                return 0

        def _can_hit_face(e) -> bool:
            if e.tags.get(GameTag.FROZEN):
                return False
            if e.tags.get(GameTag.NUM_ATTACKS_THIS_TURN):
                return False
            if (e.id in self._entered_play or
                    e.tags.get(GameTag.JUST_PLAYED)) \
                    and not e.tags.get(GameTag.CHARGE):
                return False
            return True

        return sum(atk(e) for e in self.board(key) if _can_hit_face(e))

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

    def spellpower(self, key: PlayerKey) -> int:
        """当前场上法强总和(引擎在玩家实体上维护的 CURRENT_SPELLPOWER_BASE)。"""
        e = self._player_entity(key)
        return (e.tags.get(GameTag.CURRENT_SPELLPOWER_BASE, 0)
                if e is not None else 0)

    def is_cosmetic_entity(self, e) -> bool:
        """装饰性实体(宠物等): 非对局内容 —— COSMETIC 区 / PET 类型 / PET_ 卡牌号。"""
        cid = getattr(e, "card_id", None)
        return (e.zone == Zone.COSMETIC
                or e.tags.get(GameTag.CARDTYPE) == CardType.PET
                or (isinstance(cid, str) and cid.startswith(PET_CARD_PREFIX)))

    def enchantment_entities_on(self, eid: int) -> list:
        """挂在实体 eid 上的附魔实体(实体号序≈创建序)。

        附魔实体特征: CARDTYPE=ENCHANTMENT + ATTACHED=宿主; 区=SETASIDE 或
        PLAY(2026-09-13 实测: 手牌减费附魔 TTN_955Be 创建后 TAG_CHANGE
        ZONE→PLAY, 只查 SETASIDE 全漏 → 减费/连锁归因变 ?)。"""
        return [e for e in self.entities()
                if (e.zone in (Zone.SETASIDE, Zone.PLAY)
                    and e.tags.get(GameTag.CARDTYPE) == CardType.ENCHANTMENT
                    and e.tags.get(GameTag.ATTACHED) == eid)]

    def enchantments_on(self, eid: int) -> list[str]:
        """连锁关联: 挂在实体 eid 上的附魔 card_id(去重保序)。

        用途: 解释手牌费用变动的原因(谁减了费)、触发属于哪张牌、快照手牌
        的在身效果、信息区减费来源 —— 训练样本/快照 JSONL 随之持久化。"""
        out: list[str] = []
        for e in self.enchantment_entities_on(eid):
            cid = getattr(e, "card_id", None)
            if cid and cid not in out:
                out.append(cid)
        return out

    def aura_enchantments_on_player(self, key: PlayerKey | None) -> list[str]:
        """挂在玩家实体上的光环附魔 card_id(去重保序; key=None → [])。

        用途: 玩家级光环(2026-09-14 实测 SC_755e2 灵能矩阵"下一张星灵牌-2"
        ATTACHED=玩家实体而非任何卡)不进 enchantments_on(卡) —— 减费来源
        第三级归因(卡级附魔 → 块级触发 → 玩家级光环)的最后一环, 信息区
        "减N(?)"的 ? 由它消除。注意: 本查询**不过滤语义**, 玩家实体上的
        全部附魔都返回 —— 消费侧(render/enrich)必须按 CostDown 过滤
        (二轮审计 F1)。"""
        if key is None:
            return []
        pe = self._player_entity(key)
        if pe is None:
            return []
        out: list[str] = []
        for e in self.enchantment_entities_on(pe.id):
            cid = getattr(e, "card_id", None)
            if cid and cid not in out:
                out.append(cid)
        return out

    def linked_effects(self) -> list[dict]:
        """联动卡效果台账: 全部在身附魔 → 宿主 + 来源卡(对局存档持久化)。

        一条联动 = 附魔实体(CARDTYPE=ENCHANTMENT + ATTACHED=宿主, 区=SETASIDE
        或 PLAY, 见 enchantments_on 的实测) + CREATOR 归因(打出的那张牌,
        实体未揭示时只留实体号)。手牌减费、光环、游客类全局附魔
        (ATTACHED=玩家实体)都在其中 —— 随快照 JSONL 与训练 meta 落盘,
        供训练视图与回放复现, 不再只散落在 packet 流里。"""
        out = []
        for e in self.entities():
            if (e.zone not in (Zone.SETASIDE, Zone.PLAY)
                    or e.tags.get(GameTag.CARDTYPE) != CardType.ENCHANTMENT):
                continue
            host = e.tags.get(GameTag.ATTACHED)
            if not host:
                continue
            creator_eid = e.tags.get(GameTag.CREATOR) or None
            host_e = self.get(host)
            out.append({
                "cid": getattr(e, "card_id", None),
                "host": host,
                "host_cid": getattr(host_e, "card_id", None),
                "creator": self.cid_of(creator_eid) if creator_eid else None,
                "creator_eid": creator_eid,
                "zone": Zone(e.zone).name,     # enum/int 混存: 统一归一为枚举名
            })
        return out

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
        if self._pending_play is not None:
            # 挂起 PLAY 块内衍生的事件(抉择抽牌/减费/触发/阵亡…)先扣留,
            # 块尾 flush 时跟在 play 事件之后按原序冲出 —— 因果顺序:
            # 先"打出 X(抉择N)", 再"抽到 Y"(2026-09-13 实测曾倒置)
            self._deferred.append(evt)
            return
        evt.setdefault("turn", self.turn)
        evt.setdefault("friendly", self.friendly_key)
        for cb in list(self._subs):
            try:
                cb(evt)
            except Exception:  # noqa: BLE001  单订阅者异常不拖垮其他订阅者
                log.exception("事件订阅者异常(已隔离): kind=%s", evt.get("kind"))

    # ================= 变更入口(spec §3.2 细化) =================
    def apply(self, p, depth: int = 0) -> None:
        """游标逐包调用: 先刷挂起 PLAY → 取旧标签 → 库应用 → 衍生。"""
        if self._pending_play is not None and depth <= self._pending_play[0]:
            self._flush_play()
        if self._pending_prepare is not None and depth <= self._pending_prepare[0]:
            self._flush_prepare()
        if self._ctx_trigger and depth <= self._ctx_trigger[0]:
            self._ctx_trigger = None       # 离开该触发块: 上下文作废
        old = self._pre_tags(p)
        self.exporter.export_packet(p)
        self._derive(p, old, depth)
        if is_play_block(p):
            self._pending_play = (depth, p)
            # 真实手牌价必须在块开始时锁定: 手牌费减益的回退(0→2)常写在打出块
            # 内部(实测光子炮台), 块尾 flush 时读标签拿到的是回退后的原费
            pe = self.get(p.entity) if isinstance(p.entity, int) else None
            self._pending_cost = (pe.tags.get(GameTag.COST)
                                  if pe is not None else None)

    def settle(self) -> None:
        """批尾: 块已收口(ended)的挂起 PLAY / 预备 立即发出。"""
        if self._pending_play is not None and getattr(self._pending_play[1], "ended", False):
            self._flush_play()
        if (self._pending_prepare is not None
                and getattr(self._pending_prepare[1], "ended", False)):
            self._flush_prepare()

    def note_friendly(self, pid: PlayerKey | None) -> None:
        if pid is not None:
            self.friendly_key = pid

    def apply_hints(self, cid: dict[int, str] | None = None,
                    ctrl: dict[int, PlayerKey] | None = None) -> None:
        """行级括号线索批量注入(watcher 采集, hslog 丢弃信息的兜底)。"""
        if cid:
            self._hint_cid.update(cid)
        if ctrl:
            self._hint_ctrl.update(ctrl)

    def hint_draw(self, eid: int, cid: str, actor: PlayerKey) -> None:
        """行级括号 SHOW_ENTITY(抽牌揭示主形态)的直通口, 走同一去重。"""
        self._emit_draw(eid, cid, actor)

    def note_progress(self, lines: int) -> None:
        """监控线程行号推进(诊断/进度展示用)。"""
        self.lines_consumed = lines

    def set_meta(self, meta: dict[str, str]) -> None:
        """对局元信息(GameType/FormatType 等, 一次会话内不变), 新局时注入。"""
        self.meta = dict(meta)

    def _pre_tags(self, p) -> dict | None:
        """受影响实体应用前的标签快照(推导 区域/费用/血甲/法强 变化用)。
        玩家级标签变更常用名字形式(Entity=战网名), 也解析回玩家实体。"""
        eid = getattr(p, "entity", None)
        if isinstance(eid, int):
            e = self.get(eid)
        else:
            key = self._key_of(eid)
            e = self._player_entity(key) if key is not None else None
        return dict(e.tags) if e is not None else None

    # ================= 状态迁移 → 事件/自有字段 =================
    def _derive(self, p, old: dict | None, depth: int = 0) -> None:
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
            self.last_discover = None           # 洗牌后牌底构成作废
        elif name == "Block":
            self._on_block(p, depth)
        else:
            # 全量收录原则: 未解释的包记 raw(MetaData/Options/SubSpell/ChangeEntity...)
            self._record_raw(name, p)

    def _record_raw(self, ptype: str, p) -> None:
        """raw 事件: 入库(store.unhandled -> JSONL), 照常分发订阅, 但渲染层跳过。"""
        evt = {"kind": "raw", "packet_type": ptype, "payload": packet_payload(p)}
        self.unhandled.append(evt)          # deque(maxlen) 自动裁剪
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
            if getattr(e, "is_ai", None) is not None:   # 玩家实体(duck 判别)
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
            elif tag == GameTag.FATIGUE:
                # 疲劳计数挂在玩家实体上(新版日志 FATIGUE 块的 Entity 是英雄,
                # 块级解析不出主客; 计数标签自带玩家与次数, 即疲劳伤害值)
                self._emit_event({"kind": "fatigue", "actor": key,
                                  "count": int(value or 0)})
            elif tag == GameTag.CURRENT_SPELLPOWER_BASE:
                # 引擎维护的"当前场上法强总和": 打出/附魔/死亡/沉默都会重算
                cur = int(value or 0)
                prev = (old or {}).get(tag)
                if prev is None or prev != cur:
                    self._emit_event({"kind": "spellpower", "actor": key,
                                      "total": cur, "prev": prev})
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
        elif tag == GameTag.CONTROLLER:
            self._on_controller_change(e, old, value)
        elif tag == GameTag.COST:
            self._on_cost_change(e, old, value)
        elif tag == TAG_PREPARING and value == 1:
            self._on_preparing_start(entity)
        elif tag in (GameTag.DAMAGE, GameTag.ARMOR, GameTag.HEALTH) and is_hero(e):
            self._on_hero_attr(e, old, tag)

    def _on_zone_change(self, e, old, value) -> None:
        old_zone = (old or {}).get(GameTag.ZONE)
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        cid = self.cid_of(e.id)
        if value == Zone.PLAY.value:
            self._entered_play.add(e.id)   # 召唤失调信号(回合开始清空)
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
        self._note_hero_power(e)

    # ---- 英雄技能(灌注/替换/升级) ----
    def _note_hero_power(self, e) -> None:
        """技能实体进 PLAY 或被揭示时登记; 更换或牌名变化即发事件。

        去重语义(审计 2026-09-13 中#4): 未知名(card_id=None)只登记不发声,
        延迟到可知名再报 —— 同实体先隐后揭不会连发 "#126" 与真名两条。"""
        if (e is None or e.zone != Zone.PLAY
                or e.tags.get(GameTag.CARDTYPE) != CardType.HERO_POWER):
            return
        pid = self.ctrl_key(e)
        if pid is None:
            return
        cid = getattr(e, "card_id", None) or None
        prev_eid = self.hero_power.get(pid)
        known = self.hero_power_cid.get(pid)
        if prev_eid is not None and cid and (prev_eid != e.id or cid != known):
            self._emit_event({"kind": "hero_power", "actor": pid,
                              "card_id": cid, "eid": e.id})
        if cid:
            self.hero_power_cid[pid] = cid
        self.hero_power[pid] = e.id

    def _on_controller_change(self, e, old, value) -> None:
        """本体被夺(嫉妒收割者"使用对手卡的复制后偷取本体"等): 我方实体控制权
        转归对手 —— 记实体号, 知识层台账按其揭示的卡号扣除(被偷后相关数据
        要减少, 2026-09-13 用户要求)。实体此刻多半未揭示(牌库暗牌), 卡号
        延迟到揭示后由 knowledge.rebuild 解析。"""
        try:
            prev = (old or {}).get(GameTag.CONTROLLER)
            new = int(value)
        except (TypeError, ValueError):
            return
        if prev is not None and prev == self.friendly_key and new != prev:
            self.stolen_eids.add(e.id)

    def _on_cost_change(self, e, old, value) -> None:
        ctrl = self.ctrl_key(e) or self._hint_ctrl.get(e.id)
        cid = self.cid_of(e.id)
        if ctrl != self.friendly_key or not cid:
            return
        old_cost = (old or {}).get(GameTag.COST)
        base = self.carddb.cost(cid)
        ref = old_cost if old_cost is not None else base   # 首次变动以面板费为基准
        if ref is not None and value != ref:
            # 纯状态事实: via(归因)由解析层依据 eid/ctx_eid 在渲染前富化
            self._emit_event({"kind": "cost", "card_id": cid,
                              "old": ref, "new": value, "actor": ctrl,
                              "eid": e.id,
                              "ctx_eid": (self._ctx_trigger[1]
                                          if self._ctx_trigger else None)})

    def _on_preparing_start(self, eid: int) -> None:
        """预备块内宿主翻 PREPARING=1 —— 此刻"正在预备"附魔已挂上(SHOW_ENTITY
        先于翻转, 实测 20_48_57_g03), 触发行按因果序提前到块内发出:
        触发 正在预备 → 预备完成 → 块内费用变化。尾随约1秒的真身 TRIGGER 块
        按 _prepare_trigger_eid 去重(允许按块结构提前发事件, 见 hslog 坑 29)。"""
        if self._pending_prepare is None or eid != self._pending_prepare[3]:
            return
        _depth, _block, evt, host = self._pending_prepare
        self._pending_prepare = None
        ench = next(iter(self.enchantment_entities_on(host)), None)
        if ench is not None and getattr(ench, "card_id", None):
            self._prepare_trigger_eid = ench.id
            actor = (self.ctrl_key(ench) or self._hint_ctrl.get(ench.id))
            self._emit_event({"kind": "trigger", "eid": ench.id,
                              "actor": actor,
                              "card_id": ench.card_id,
                              "keyword": "", "effect_index": None,
                              "actor_deck_count": (self.deck_count(actor)
                                                   if actor is not None else None),
                              "host_eid": host, "host": self.cid_of(host)})
        self._emit_event(evt)

    def _flush_prepare(self) -> None:
        """预备块收口仍未见 PREPARING 翻转(变体形态): 按原行为补发预备完成。"""
        _depth, _block, evt, _host = self._pending_prepare
        self._pending_prepare = None
        self._emit_event(evt)

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
        self._prepare_trigger_eid = None   # 提前报过的"正在预备"触发只在当回合内去重
        self._entered_play = set()        # 新回合: 上回合进场的随从不再失调
        m = self.mulligan.get(key)
        if m is not None and m.decided:
            m.closed = True                # 决定后的首回合开始: 换入窗口关闭
                                           # (数量闸缺额时的兜底, 防 T1 后误记)
        if prev is None:
            # 首个行动方(先手的留牌回合)
            self._emit_event({"kind": "turn_start", "actor": key, "prev": None,
                              "first": True, "my_turn_no": 1, "total_turn": 1})
            if key == self.friendly_key:
                self._emit_event({"kind": "play_offer", "actor": key})
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
        if key == self.friendly_key:
            # T2 出牌建议时机(我方 turn_start 出主建议): 事件只报时机,
            # 排序事实由 analysis.enrich 挂建议器产出, 措辞归 render
            self._emit_event({"kind": "play_offer", "actor": key})

    # ---- 实体/揭示衍生(Task 4 实现块/选择; 这两个在本任务即有行为) ----
    def _on_full_entity(self, p) -> None:
        e = self.get(p.entity) if isinstance(p.entity, int) else None
        self._note_hero_power(e)
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
        self._note_hero_power(e)
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
        if last is not None and now - last < self._draw_dedup:
            return   # 同一实体的多重揭示路径只报一次
        self._draw_ts[eid] = now
        self._note_mulligan_replacement(cid, actor)
        self._emit_event({"kind": "draw", "card_id": cid, "actor": actor})

    def _note_mulligan_replacement(self, cid: str, actor) -> None:
        """换牌换入的牌: 该玩家留牌决定之后、换入窗口关闭之前的抽牌。
        最终手牌事实, 随留牌训练样本落盘(2026-09-13 用户要求)。
        关窗双闸(gotcha 40): 数量闸——补牌凑满应补数即关(先手局的
        "决定后首回合开始"失效, 见 _mulligan_decide); turn_start 兜底——
        补牌缺额(隐藏/引擎异常)时不死锁, 仍按首回合开始关闭。"""
        if not cid or is_coin(cid):
            return
        m = self.mulligan.get(actor)
        if m is not None and m.decided and not m.closed:
            if m.expected_in is not None and len(m.replaced_in) >= m.expected_in:
                m.closed = True        # 已凑满/应补为0: 关窗, 后续抽牌不入
                return
            m.replaced_in.append(cid)
            if (m.expected_in is not None
                    and len(m.replaced_in) >= m.expected_in):
                m.closed = True        # 补牌到齐: 即刻关窗

    # ================= 块(spec §3.4 补全: TRIGGER/疲劳) =================
    def _on_block(self, p, depth: int = 0) -> None:
        btype = getattr(p, "type", None)
        if btype == BlockType.ATTACK and isinstance(p.entity, int):
            a = self.get(p.entity)
            t = self.get(p.target) if isinstance(p.target, int) else None
            self._emit_event({
                "kind": "attack",
                "actor": (self.ctrl_key(a) if a is not None else None)
                         or self._hint_ctrl.get(p.entity),
                "attacker_card_id": self.cid_of(p.entity),
                "attacker_is_hero": a is not None and is_hero(a),
                "target_card_id": (self.cid_of(p.target)
                                   if isinstance(p.target, int) else None),
                "target_is_hero": t is not None and is_hero(t),
            })
        elif btype == BlockType.TRIGGER and isinstance(p.entity, int):
            e = self.get(p.entity)
            # 玩家实体按鸭子判别(is_ai 为 Player 独有): 不 import
            # hearthstone.entities —— 实体类只许 adapter 碰(铁律 1, 审计 2026-09-14)
            if (e is None or getattr(e, "is_ai", None) is not None
                    or (self.game is not None and e is self.game)):
                return          # 玩家/游戏实体上的触发不单独报卡牌事件
            if self.is_cosmetic_entity(e):
                return          # 宠物等装饰实体: 非对局内容, 不报事件
            tk = getattr(p, "trigger_keyword", None)
            kw_name = getattr(tk, "name", None) or ""
            if not kw_name and tk == TAG_START_OF_GAME_KEYWORD:
                # 旧版 hearthstone 枚举缺名, 日志写数字 —— 常量在 consts.py
                kw_name = "START_OF_GAME_KEYWORD"
            actor = (self.ctrl_key(e) or self._hint_ctrl.get(p.entity))
            host_eid = e.tags.get(GameTag.ATTACHED)
            try:
                ei = int(getattr(p, "effectindex", None))
            except (TypeError, ValueError):
                ei = None
            # 纯状态事实: card_id 只来自解析链内回填(实体揭示/行级括号线索),
            # 不做任何猜测; 未命名的开局触发由渲染层判弃, 命名后自然以真名出
            if p.entity != self._prepare_trigger_eid:
                self._emit_event({"kind": "trigger", "eid": p.entity,
                                  "actor": actor,
                                  "card_id": self.cid_of(p.entity),
                                  "keyword": kw_name,
                                  "effect_index": ei,
                                  "actor_deck_count": (self.deck_count(actor)
                                                       if actor is not None else None),
                                  "host_eid": host_eid if isinstance(host_eid, int) else None,
                                  "host": (self.cid_of(host_eid)
                                           if isinstance(host_eid, int) else None)})
            # 预备块内已提前报过的"正在预备"真身(尾随约1秒)不再重复出链路行,
            # 但块级位置上下文照设: 供后续费用归因
            self._ctx_trigger = (depth, p.entity)
        # FATIGUE 块不在此报事件: 新版日志块 Entity 是英雄实体(主客解析不出),
        # 疲劳行由块内的 FATIGUE 标签变更发出(玩家+次数), 掉血由 _on_hero_attr 衔接
        elif btype == BlockType.PLAY:
            pass                            # 延迟发由 apply 的挂起机制处理
        elif btype == BlockType.DECK_ACTION and isinstance(p.entity, int):
            # 预备完成动作: "预备完成"先暂存, 等块内宿主 PREPARING=1 翻转时
            # 随提前的"触发 正在预备"按因果序发出(触发→预备→块内费用变化);
            # 变体形态无翻转则块尾补发(_flush_prepare)。PREPARE=1 的手牌才
            # 认定(防其他 DECK_ACTION 误报)。
            e = self.get(p.entity)
            if (e is not None and e.zone == Zone.HAND
                    and e.tags.get(TAG_PREPARE) and self.cid_of(p.entity)):
                if self._pending_prepare is not None:
                    self._flush_prepare()   # 防御: 未决先补发(块不嵌套)
                self._pending_prepare = (
                    depth, p,
                    {"kind": "prepare", "card_id": self.cid_of(p.entity),
                     "actor": (self.ctrl_key(e)
                               or self._hint_ctrl.get(p.entity)),
                     "eid": p.entity},
                    p.entity)
        else:
            # POWER/DEATHS/JOUST/MOVE_MINION/SUB_SPELL 等块: 全量收录, 记 raw
            self._record_raw(f"Block:{getattr(btype, 'name', btype)}", p)

    def _flush_play(self) -> None:
        """PLAY 块子树结束时发出 —— 块内 SHOW_ENTITY 此时已揭示身份。
        扣留的块内事件在 play 之后立即冲出(见 _emit_event)。"""
        _depth, p = self._pending_play
        self._pending_play = None
        cost_tag = self._pending_cost      # 块开始时锁定的真实手牌价
        self._pending_cost = None
        try:
            self._emit_play(p, cost_tag)
        finally:
            self._drain_deferred()
            if self._play_offer_due:
                # T2 出牌建议时机(每次我方打出后批尾重发): 抽牌/回费事件已
                # 到齐(块内扣留事件已冲出), 建议按打完后的局面重算
                self._play_offer_due = False
                self._emit_event({"kind": "play_offer",
                                  "actor": self.friendly_key})

    def _emit_play(self, p, cost_tag: int | None) -> None:
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
                          "eid": eid,
                          "cost_base": self.carddb.cost(cid),
                          "cost_tag": cost_tag,
                          "mana_left": mana_left,
                          "is_power": e is not None and is_hero_power(e),
                          "suboption": sub if isinstance(sub, int) and sub >= 0 else None,
                          "spellpower": self.spellpower(actor)})
        if actor == self.friendly_key and e is not None:
            # 通用模式判定只看"来自卡组"的牌: 衍生牌/硬币不算卡组不匹配
            if not is_generated(e) and not is_coin(cid):
                self.played_cids.add(cid)
        self._play_offer_due = actor == self.friendly_key

    def choose_one_buttons(self, eid: int | None) -> list:
        """抉择按钮实体(PARENT_CARD=主卡), 按实体号排序 —— 实体号序即抉择顺序。
        纯状态查询: 下标→所选子卡的解析在 analysis 层(与 via/牌名推断同模式)。"""
        if eid is None:
            return []
        return sorted((e for e in self.entities()
                       if e.tags.get(GameTag.PARENT_CARD) == eid),
                      key=lambda e: e.id)

    def _drain_deferred(self) -> None:
        while self._deferred:
            batch, self._deferred = self._deferred, []
            for evt in batch:
                self._emit_event(evt)

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
            self._emit_event({"kind": "mulligan_offer", "actor": key,
                              "msg": f"起手可留: {'、'.join(names)}",
                              "offered": [c for c in
                                          (self.cid_of(e) for e in p.choices or [])
                                          if c]})
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
                # 牌底构成事实(浅→深): 置底机制/探底的未选项即当前牌库底;
                # 选项实体是隐藏真身的替身, 台账从实体状态看不见 —— 记下供
                # 知识层并入(是否采信由知识层按来源卡机制判定)
                if (info.get("pid") == self.friendly_key and src_cid
                        and picked and bottom):
                    bottoms = [self.cid_of(c) for c in bottom]
                    if all(bottoms):
                        self.last_discover = {"source": src_cid,
                                              "picked": [self.cid_of(c) for c in picked],
                                              "unpicked": bottoms}
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
        m.decided = True
        # 数量闸(gotcha 40): 先手局的 T1 turn_start 由 CURRENT_PLAYER 翻转驱动,
        # 发生在留牌决定之前, "决定后首回合开始"关闸对先手失效。改按数量关窗:
        # 换几张补几张(补牌紧随 SendChoices 成批到达), 应补数=换掉张数
        # =offered−kept−硬币(gotcha 9, 硬币不属于补牌)。用引擎实体/数量关系,
        # 不逐卡硬编码。
        coins = {e for e in m.offered if is_coin(self.cid_of(e))}
        m.expected_in = len([e for e in m.offered
                             if e not in kept and e not in coins])
        if m.expected_in == 0:
            m.closed = True            # 无可补(全留): 决定即关窗
        if key not in self._mulligan_emitted:
            self._mulligan_emitted.add(key)
            self._emit_event({"kind": "mulligan", "actor": key,
                              "msg": self.mulligan_text(key)})

    def mulligan_facts(self) -> dict:
        """留牌事实(唯一推导): {pid: {offered/kept/replaced/replaced_in/decided}}。
        replaced_in = 换牌换入(决定后、换入窗口关闭前的抽牌), 即最终手牌的补充。"""
        out = {}
        for pid, m in self.mulligan.items():
            def names(eids):
                return [self.cid_of(e) or f"#{e}" for e in eids]
            offered, kept = names(m.offered), names(m.kept)
            coin = [c for c in offered if is_coin(c)]
            # decided=False(对手不广播决定)时不推算 replaced, 避免污染训练数据
            replaced = [c for c in offered if c not in kept and c not in coin]                 if m.decided else []
            replaced_in = [c for c in m.replaced_in if not is_coin(c)] \
                if m.decided else []          # 已是 card_id, 不过 cid_of
            out[pid] = {"offered": offered, "kept": kept,
                        "replaced": replaced, "replaced_in": replaced_in,
                        "decided": m.decided}
        return out

    def heroes_facts(self) -> dict:
        out = {}
        for pid in self.player_keys():
            h = self.hero(pid)
            if h is not None and h.card_id:
                out[pid] = h.card_id
        return out

    def playstate_facts(self) -> dict:
        return {pid: self.playstate(pid) for pid in self.player_keys()}

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
        hand = [{"id": e.card_id, "pos": zone_pos(e),
                 "cost": e.tags.get(GameTag.COST), "generated": is_generated(e),
                 "effects": self.enchantments_on(e.id)}
                for e in (self.hand(me) if me is not None else [])]
        return {
            "reason": reason,
            "turn": self.turn,
            "friendly_turn": self.friendly_turn_number(),
            "my_turn": self.is_my_turn(),
            "linked_effects": self.linked_effects(),
            "players": {k: {"name": self.name(k),
                            "hero_power": self.hero_power_cid.get(k)}
                        for k in self.player_keys()},
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
