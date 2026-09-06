"""监听层 —— tail / packet 游标 / FSM / 快照触发(M1_MONITOR §2-§3)。

设计要点:
  * 不维护增量状态: 快照每次全量导出实体树; 链路事件走"packet 平铺游标"
    (hslog 只追加 packet, 平铺序列前缀稳定, 游标即增量)。
  * shadow 表只是链路显示用的元数据(卡名/控制器/费用), 不是权威状态。
  * PLAY 块延迟到子树结束再发: 出的牌若当回合才摸到, 其 SHOW_ENTITY 在
    PLAY 块内部, 块开始时身份未知。对手的牌另有"日志行括号 cardId/player
    兜底表"(hslog 会丢弃括号里的信息)。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from hearthstone.enums import BlockType, CardType, ChoiceType, GameTag, PlayState, Zone
from hslog import packets

from .adapter import export_game_state, feed_line, new_parser, resolve_friendly
from .carddb import CardDB
from .config import Config
from .knowledge import DeckKnowledge, parse_decks_log
from .persist import SessionStore
from .render import chain_line, game_end_line, snapshot_block

_CREATE_GAME_MARK = "GameState.DebugPrintPower() - CREATE_GAME"
_TERMINAL_PLAYSTATE = {PlayState.WON.value, PlayState.LOST.value, PlayState.TIED.value}
_MANA_TAGS = {
    GameTag.RESOURCES: "res",
    GameTag.TEMP_RESOURCES: "temp",
    GameTag.RESOURCES_USED: "used",
    GameTag.OVERLOAD_OWED: "overload",
    GameTag.OVERLOAD_LOCKED: "overload",
}
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
            if isinstance(p, packets.Block):
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
        self.store: SessionStore | None = None
        self.friendly: int | None = None
        self.knowledge: DeckKnowledge | None = None
        self.generic = False
        self.played_cids: set[str] = set()
        self.session_name = ""
        self._live = False           # 实时来源(回放不自动导训练样本)
        self._corpus = None
        self._ent2pid: dict[int, int] = {}   # 玩家实体id -> PLAYER_KEY(CreateGame 时建立)
        self._player_turn: dict[int, int] = {}  # PLAYER_KEY -> 玩家自己的回合数
        self._mana: dict[int, dict] = {}     # PLAYER_KEY -> {res,temp,used,overload}
        self.mulligan: dict[int, dict] = {}  # PLAYER_KEY -> {offered, kept}
        self._choice_pid: dict[int, int] = {}  # Choices id -> PLAYER_KEY
        self._mulligan_emitted: set[int] = set()
        self._discover: dict[int, dict] = {}   # Choices id -> {offered, source}
        self._discover_emitted: set[int] = set()
        self._pending_play = None      # (depth, Block) 延迟到子树结束再发
        self._hint_cid: dict[int, str] = {}    # 日志行括号兜底: 实体id -> cardId
        self._hint_ctrl: dict[int, int] = {}   # 日志行括号兜底: 实体id -> PLAYER_KEY
        self._ent2pid: dict[int, int] = {}   # 玩家实体id -> PLAYER_KEY(CreateGame 时建立)

        self.shadow: dict[int, dict] = {}   # 实体id -> {cid, ctrl, cost, creator, zone, hero}
        self.turn = 0
        self.current: int | None = None
        self.chain: list[str] = []           # 本回合链路行
        self.summary: list[str] = []         # 本回合打出的卡名(链路摘要用)
        self.snapshot_warned = False

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
            if "Entity=[" in line:            # hslog 丢弃括号里的 cardId/player, 这里兜底
                if "cardId=" in line:
                    for m in _BRACKET_CID_RE.finditer(line):
                        if m.group(2):
                            self._hint_cid[int(m.group(1))] = m.group(2)
                if "player=" in line:
                    for m in _BRACKET_PLAYER_RE.finditer(line):
                        self._hint_ctrl[int(m.group(1))] = int(m.group(2))
            feed_line(self.parser, line)
            # 括号形式的 SHOW_ENTITY(抽牌揭示主形态) hslog 不解析 —— 行级补抽牌事件
            if "SHOW_ENTITY" in line and "zone=DECK" in line and "GameState." in line:
                m = _SHOW_DRAW_RE.search(line)
                if m and self.friendly is not None and int(m.group(2)) == self.friendly:
                    self._chain_event({"kind": "draw", "card_id": m.group(3),
                                       "actor": int(m.group(2))})

    def _after_batch(self) -> None:
        self._detect_game()
        if self.parser.games:
            self._process_tree(self.parser.games[-1])
        if self.friendly is None:
            self.friendly = resolve_friendly(self.parser, self.cfg.battletag)

    def _detect_game(self) -> None:
        n = len(self.parser.games)
        while self.game_count < n:
            if self.game_count > 0:      # 把上一局尾部事件冲完再切
                self._process_tree(self.parser.games[self.game_count - 1])
            self.game_count += 1
            self._new_game()

    def _new_game(self) -> None:
        self.game_no += 1
        self.cursor = 0
        self.shadow = {}
        self.turn = 0
        self.current = None
        self.friendly = None
        self.generic = False
        self.played_cids = set()
        self.chain = []
        self.summary = []
        self.snapshot_warned = False
        self.mulligan = {}
        self._choice_pid = {}
        self._mulligan_emitted = set()
        self._discover = {}
        self._discover_emitted = set()
        self._pending_play = None
        self._hint_cid = {}
        self._hint_ctrl = {}
        self._player_turn = {}
        self._mana = {}
        self.state = "IN_GAME"
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
            out = self.cfg.data_dir / "decks" / "decklist.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"name": self.cfg.deck_name, "code": code,
                                       "cards": k.decklist}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        except OSError:
            pass
        return k

    def _process_tree(self, tree) -> None:
        flat = list(_walk(tree))
        i = self.cursor
        while i < len(flat):
            pkt, depth = flat[i]
            # 挂起的 PLAY: 遇到不比它更深的新包 => 其子树已结束, 身份已由子包/括号揭示
            if self._pending_play is not None and depth <= self._pending_play[0]:
                self._emit_play(self._pending_play[1])
                self._pending_play = None
            try:
                self._on_packet(pkt)
            except Exception as exc:  # noqa: BLE001  单包事件失败不拖垮监控
                self._emit(f"! 事件处理异常: {type(exc).__name__}: {exc}")
            if isinstance(pkt, packets.Block) and pkt.type == BlockType.PLAY:
                self._pending_play = (depth, pkt)   # 子树处理完再发(块内才有身份)
            i += 1
        self.cursor = len(flat)
        if self._pending_play is not None and self._pending_play[1].ended:
            self._emit_play(self._pending_play[1])
            self._pending_play = None

    # ================= packet 分发 =================
    def _sh(self, eid) -> dict:
        return self.shadow.setdefault(eid, {})

    def _on_packet(self, p) -> None:
        if isinstance(p, packets.TagChange):
            self._on_tag(p)
        elif isinstance(p, packets.CreateGame):
            self._ent2pid = {pl.entity.entity_id: pl.player_id
                             for pl in p.players
                             if hasattr(pl.entity, "entity_id") and pl.player_id}
        elif isinstance(p, packets.Choices) and p.type == ChoiceType.GENERAL:
            src = getattr(p, "source", None)
            pid = getattr(p.entity, "player_id", None) if hasattr(p.entity, "player_id") else None
            self._discover[p.id] = {"offered": list(p.choices or []),
                                    "source": src if isinstance(src, int) else None,
                                    "pid": pid}
            self._choice_pid[p.id] = pid
        elif isinstance(p, packets.SendChoices) and p.type == ChoiceType.GENERAL:
            info = self._discover.get(p.id)
            if info is not None and p.id not in self._discover_emitted:
                self._discover_emitted.add(p.id)
                picked = [c for c in (p.choices or []) if isinstance(c, int)]
                bottom = [e for e in info["offered"] if e not in picked]  # 未选项按序置底

                def cid_of(e):
                    return self._sh(e).get("cid") or self._hint_cid.get(e) or e

                src_cid = self._sh(info["source"]).get("cid") if info.get("source") else None
                self._chain_event({"kind": "discover", "actor": info.get("pid"),
                                   "src_name": self.carddb.name(src_cid),
                                   "picked": [cid_of(c) for c in picked],
                                   "bottom": [cid_of(c) for c in bottom]})
        elif isinstance(p, packets.Choices) and p.type == ChoiceType.MULLIGAN:
            key = getattr(p.entity, "player_id", None)
            if key is not None:
                self.mulligan[key] = {"offered": list(p.choices or []), "kept": []}
                self._choice_pid[p.id] = key
                names = []
                for eid in p.choices or []:
                    cid = self._sh(eid).get("cid") or self._hint_cid.get(eid)
                    nm = self.carddb.name(cid) if cid else None
                    if cid and self._is_coin(cid):
                        nm = (nm or "") + "(硬币)"
                    names.append(nm or "?")
                if all(n == "?" for n in names):
                    names = [f"第{i}张" for i in range(1, len(names) + 1)]
                self._chain_event({"kind": "text", "actor": key,
                                   "msg": f"起手可留: {'、'.join(names)}"})
        elif isinstance(p, (packets.SendChoices, packets.ChosenEntities)) \
                and getattr(p, "type", None) == ChoiceType.MULLIGAN:
            key = self._choice_pid.get(p.id)
            if key is not None:
                self.mulligan.setdefault(key, {"offered": [], "kept": []})
                self.mulligan[key]["kept"] = [c for c in (p.choices or [])
                                              if isinstance(c, int)]
                if key not in self._mulligan_emitted:
                    self._mulligan_emitted.add(key)
                    self._chain_event({"kind": "mulligan", "actor": key,
                                       "msg": self._mulligan_text(key)})
        elif isinstance(p, packets.FullEntity):
            self._on_full_entity(p)
        elif isinstance(p, packets.ShowEntity):
            self._on_show_entity(p)
        elif isinstance(p, packets.HideEntity):
            # 换牌/洗回牌库会把实体重新隐藏 —— 不处理会让 shadow 的 zone 停在旧值,
            # 之后"再次抽到"的 ZONE 变更就识别不出 DECK→HAND
            if isinstance(p.entity, int) and getattr(p, "tag", None) == GameTag.ZONE:
                try:
                    self._sh(p.entity)["zone"] = int(p.value)
                except (TypeError, ValueError):
                    pass
        elif isinstance(p, packets.ShuffleDeck):
            if self.friendly is not None:   # 调度阶段的洗牌是噪音
                actor = getattr(p, "entity", None)
                self._chain_event({"kind": "shuffle", "actor": actor})
        elif isinstance(p, packets.Block):
            self._on_block(p)

    def _on_full_entity(self, p) -> None:
        eid = p.entity
        tags = dict(p.tags)
        sh = self._sh(eid)
        if p.card_id:
            sh["cid"] = p.card_id
        ctrl = tags.get(GameTag.CONTROLLER)
        if ctrl is not None:
            sh["ctrl"] = ctrl
        if GameTag.COST in tags:
            sh["cost"] = tags[GameTag.COST]
        if GameTag.CREATOR in tags:
            sh["creator"] = tags[GameTag.CREATOR]
        if GameTag.CARDTYPE in tags:
            sh["ctype"] = tags[GameTag.CARDTYPE]
        if tags.get(GameTag.ZONE) == Zone.HAND.value:
            sh["zone"] = Zone.HAND.value
            if sh.get("ctrl") == self.friendly and p.card_id:
                creator = self.shadow.get(sh.get("creator"), {}).get("cid")
                self._chain_event({"kind": "gain", "card_id": p.card_id,
                                   "actor": sh.get("ctrl"), "creator": creator})

    def _on_show_entity(self, p) -> None:
        eid = p.entity
        tags = dict(p.tags)
        sh = self._sh(eid)
        if p.card_id:
            sh["cid"] = p.card_id
        ctrl = tags.get(GameTag.CONTROLLER)
        if ctrl is not None:
            sh["ctrl"] = ctrl
        if GameTag.COST in tags:
            sh["cost"] = tags[GameTag.COST]
        if GameTag.CARDTYPE in tags:
            sh["ctype"] = tags[GameTag.CARDTYPE]
        zone = tags.get(GameTag.ZONE, sh.get("zone"))
        ctrl = sh.get("ctrl") or self._hint_ctrl.get(p.entity)
        if zone == Zone.HAND.value and ctrl == self.friendly and p.card_id:
            self._chain_event({"kind": "draw", "card_id": p.card_id, "actor": ctrl})

    def _on_tag(self, p) -> None:
        tag, value = p.tag, p.value
        entity = p.entity
        if hasattr(entity, "player_id") and not isinstance(entity, int):
            # 玩家实体(PlayerReference): 回合/胜负都在这
            key = entity.player_id
        elif isinstance(entity, int) and entity in self._ent2pid:
            key = self._ent2pid[entity]      # 部分日志用实体 id 引用玩家
        else:
            key = None

        if key is not None:
            if tag == GameTag.CURRENT_PLAYER and value == 1:
                self._on_turn_start(key)
            elif tag == GameTag.PLAYSTATE and value in _TERMINAL_PLAYSTATE and self.state == "IN_GAME":
                self._on_game_end()
            elif tag == GameTag.TURN:
                self._player_turn[key] = int(value or 0)
            elif tag in _MANA_TAGS:
                name = _MANA_TAGS[tag]
                d = self._mana.setdefault(key, {})
                old = d.get(name)
                d[name] = int(value or 0)
                if key == self.friendly and old is not None and old != d[name]:
                    if name == "res" and d[name] > old:
                        self._chain_event({"kind": "text", "actor": key,
                                           "msg": f"水晶上限 {old}→{d[name]}"})
                    elif name == "temp" and d[name] > old:
                        self._chain_event({"kind": "text", "actor": key,
                                           "msg": f"临时水晶 +{d[name] - old}"})
            return
        if not isinstance(entity, int):
            return
        sh = self._sh(entity)
        if tag == GameTag.ZONE:
            old = sh.get("zone")
            sh["zone"] = value
            ctrl = sh.get("ctrl") or self._hint_ctrl.get(entity)
            if (value == Zone.DECK.value and old == Zone.SETASIDE.value
                    and ctrl == self.friendly and sh.get("cid")):
                self._chain_event({"kind": "back_to_deck", "card_id": sh["cid"],
                                   "actor": ctrl})
            elif (value == Zone.HAND.value and old == Zone.DECK.value
                    and ctrl == self.friendly):
                # 已揭示过的实体再次抽到(探底/置底过的牌)——无 SHOW_ENTITY, 只有 ZONE 变更
                cid = sh.get("cid") or self._hint_cid.get(entity)
                if cid:
                    self._chain_event({"kind": "draw", "card_id": cid, "actor": ctrl})
        elif tag == GameTag.COST:
            old = sh.get("cost")
            sh["cost"] = value
            cid = sh.get("cid") or self._hint_cid.get(entity)
            ctrl = sh.get("ctrl") or self._hint_ctrl.get(entity)
            if ctrl == self.friendly and cid and value != old:
                base = self.carddb.cost(cid)
                ref = old if old is not None else base   # 首次变动以面板费为基准
                if ref is not None and value != ref:
                    self._chain_event({"kind": "cost", "card_id": cid,
                                       "old": ref, "new": value, "actor": ctrl})
        elif tag == GameTag.CREATOR:
            sh["creator"] = value
        elif tag == GameTag.CONTROLLER:
            sh["ctrl"] = value
        elif tag == GameTag.CARDTYPE:
            sh["ctype"] = value
        elif tag == GameTag.TURN:
            self.turn = int(value or 0)   # GameEntity 总回合数, 到即生效
        elif tag in (GameTag.DAMAGE, GameTag.ARMOR, GameTag.HEALTH)                 and self._is_hero(sh):
            key_name = {GameTag.DAMAGE: "damage", GameTag.ARMOR: "armor",
                        GameTag.HEALTH: "health"}[tag]
            old = sh.get(key_name)
            sh[key_name] = int(value or 0)
            hp = max(0, sh.get("health", 30) - sh.get("damage", 0))
            who = "我方英雄" if sh.get("ctrl") == self.friendly else "敌方英雄"
            if tag == GameTag.DAMAGE and old is not None and old != sh[key_name]:
                old_hp = max(0, sh.get("health", 30) - old)
                self._chain_event({"kind": "text", "actor": sh.get("ctrl"),
                                   "msg": f"{who} {old_hp}血→{hp}血"
                                          f"(甲{sh.get('armor', 0)})"})
            elif tag == GameTag.ARMOR and old is not None and old != sh[key_name]:
                self._chain_event({"kind": "text", "actor": sh.get("ctrl"),
                                   "msg": f"{who}护甲 {old}→{sh[key_name]}"
                                          f"(血{hp})"})

    def _emit_play(self, p) -> None:
        """PLAY 块子树结束时发出 —— 块内 SHOW_ENTITY 此时已揭示身份。"""
        eid = p.entity
        if not isinstance(eid, int):
            return
        sh = self._sh(eid)
        cid = sh.get("cid") or self._hint_cid.get(eid) or None
        actor = sh.get("ctrl")
        if actor is None:
            actor = self.current          # 出牌必然发生在行动方自己的回合
        if not cid or actor is None:
            return
        f = self._mana.get(actor) or {}
        mana_left = max(0, f.get("res", 0) + f.get("temp", 0) - f.get("used", 0))
        sub = getattr(p, "suboption", None)
        is_power = self._is_power(sh)
        self._chain_event({"kind": "play", "card_id": cid, "actor": actor,
                           "cost_base": self.carddb.cost(cid),
                           "cost_tag": sh.get("cost"), "mana_left": mana_left,
                           "is_power": is_power,
                           "suboption": sub if isinstance(sub, int) and sub >= 0 else None})
        if actor == self.friendly:
            # 通用模式判定只看"来自卡组"的牌: 衍生牌/硬币不算卡组不匹配
            if not sh.get("creator") and not self._is_coin(cid):
                self.played_cids.add(cid)
            self.summary.append(self.carddb.name(cid))

    def _on_block(self, p) -> None:
        if p.type == BlockType.ATTACK and isinstance(p.entity, int):
            a = self._sh(p.entity)
            t = self._sh(p.target) if isinstance(p.target, int) else {}
            self._chain_event({"kind": "attack", "actor": a.get("ctrl"),
                               "attacker_card_id": a.get("cid"),
                               "attacker_is_hero": self._is_hero(a),
                               "target_card_id": t.get("cid"),
                               "target_is_hero": self._is_hero(t)})

    @staticmethod
    def _is_coin(cid: str | None) -> bool:
        return bool(cid) and ("COIN" in cid.upper() or cid.upper() == "GAME_005")

    def _mulligan_text(self, key: int) -> str:
        m = self.mulligan.get(key) or {}
        offered, kept = m.get("offered") or [], m.get("kept") or []

        def cid_of(eid):
            return self._sh(eid).get("cid")

        coins = {e for e in offered if self._is_coin(cid_of(e))}
        replaced = [e for e in offered if e not in kept and e not in coins]
        if key == self.friendly:
            kept_n = [(self.carddb.name(cid_of(e)) or "?") for e in kept]
            repl_n = [(self.carddb.name(cid_of(e)) or "?") for e in replaced]
            return (f"留牌: {'、'.join(kept_n) or '(无)'} │ "
                    f"换掉: {'、'.join(repl_n) or '(无)'}")
        if kept:
            return f"留牌 {len(kept)} 张 │ 换掉 {len(replaced)} 张(牌名不可见)"
        return f"起手 {len(offered)} 张(留牌细节未广播)"

    @staticmethod
    def _is_power(sh: dict) -> bool:
        ctype = sh.get("ctype")
        return ctype == CardType.HERO_POWER or ctype == CardType.HERO_POWER.value

    @staticmethod
    def _is_hero(sh: dict) -> bool:
        ctype = sh.get("ctype")
        return ctype == CardType.HERO or ctype == CardType.HERO.value

    # ================= 事件 =================
    def _chain_event(self, evt: dict) -> None:
        evt.setdefault("turn", self.turn)
        evt.setdefault("friendly", self.friendly)
        line = chain_line(evt, self.carddb)
        self.chain.append(line)
        self._emit(line)

    def _mana_text(self, key: int) -> str:
        """回合开始时的水晶投影: 上限+1−过载(RESOURCES 标签在切换之后才跳)。"""
        f = self._mana.get(key) or {}
        res, ol = f.get("res", 0), f.get("overload", 0)
        cap = max(0, min(10, res + 1 - ol))
        out = f"水晶 {cap}/{cap}"
        if ol:
            out += f" (过载-{ol})"
        return out

    def _on_turn_start(self, key: int) -> None:
        prev, self.current = self.current, key
        if prev is None or prev == key or self.state != "IN_GAME":
            return
        self._chain_event({"kind": "text", "actor": prev, "msg": "结束回合"})
        if self.friendly is not None and key == self.friendly:
            n = self._player_turn.get(key, 0) + 1  # 玩家级 TURN 标签在切换之后才到
            self._emit(f"──── 第{self.game_no}局 · 我的第{n}回合开始 (T{self.turn}) │ "
                       f"{self._mana_text(key)} ────")
        else:
            self._snapshot("turn_end")

    def _on_game_end(self) -> None:
        self.state = "GAME_END"
        self._snapshot("game_end")
        self._export_training()

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
        gs = export_game_state(self.parser, self.lines, self.cfg.battletag)
        if gs is None:
            if not self.snapshot_warned:
                self._emit("! 快照导出失败(脏行), 本局快照不可用; 链路继续")
                self.snapshot_warned = True
            self.chain, self.summary = [], []
            return
        if gs.friendly_key is not None:
            self.friendly = gs.friendly_key
        if self.knowledge is not None and self.knowledge.mismatch_count(self.played_cids) >= 2:
            self.generic = True
        led = self.knowledge.rebuild(gs) if self.knowledge else None
        block = snapshot_block(
            gs, led, knowledge=self.knowledge, deck_name=self.cfg.deck_name,
            generic=self.generic, game_no=self.game_no, chain_lines=self.chain,
            chain_summary=self.summary, carddb=self.carddb, reason=reason)
        if reason == "game_end":
            block += "\n" + game_end_line(gs)
        self._emit(block)
        if self.store is not None:
            payload = {"reason": reason, **gs.to_dict()}
            if led is not None:
                payload["ledger"] = {
                    "in_hand": dict(led.in_hand), "used": dict(led.used),
                    "remaining": dict(led.remaining), "deck_actual": led.deck_actual,
                    "known_top": led.known_top, "known_bottom": led.known_bottom,
                }
            payload["chain_summary"] = self.summary
            self.store.write_snapshot(payload)
        self.chain, self.summary = [], []

    def _emit(self, msg: str) -> None:
        if not self.mute:
            self.out(msg)
