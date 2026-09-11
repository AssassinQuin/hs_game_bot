"""监听层 —— tail / packet 游标 / FSM / 快照触发(spec 2026-09-07 §2-§3)。

对局状态全部在 GameStore(每局一个, 库驱动); 本层只做采集与输出:
括号行扫描喂 store.hint_*, 平铺游标逐包喂 store.apply, store 衍生事件
经订阅路由到渲染/落盘。PLAY 块延迟由 store 内部处理(apply 的 depth 语义)。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .adapter import (feed_line, game_meta, is_block, new_parser,
                      resolve_friendly)
from .carddb import CardDB
from .config import Config
from .knowledge import DeckKnowledge, parse_decks_log
from .overlay import Msg
from .persist import SessionStore
from .render import chain_line, game_end_line, snapshot_block, snapshot_line
from .store import GameStore

log = logging.getLogger("hsbot.watcher")

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
        self.out = out if out is not None else (lambda m: print(m.ui))
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
        self._pending_game_end = False         # game_end 快照延迟到批尾(对手 LOST 同批稍后才应用)
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
        if self._pending_game_end and self.parser.games:
            # 终局已到: 先冲掉本批扣留的尾包(对手 LOST 同批稍后才应用), 再快照,
            # 保证终局行双方 PLAYSTATE 完整 —— 与旧全量导出路径等价
            self._process_tree(self.parser.games[-1], flush=True)
            self._fire_pending_game_end()

    def _detect_game(self) -> None:
        n = len(self.parser.games)
        while self.game_count < n:
            if self.game_count > 0:      # 把上一局尾部事件冲完再切
                self._process_tree(self.parser.games[self.game_count - 1], flush=True)
                self._fire_pending_game_end()   # 上一局终局快照须在 _new_game 重置标志前发出
            self.game_count += 1
            self._new_game()

    def _new_game(self) -> None:
        self.game_no += 1
        self.cursor = 0
        self.generic = False
        self.chain = []
        self.summary = []
        self._title_done = False
        self._pending_game_end = False
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
                log.exception("事件处理异常")
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
            # 不立即快照: 对手的终局 PLAYSTATE 在同批稍后的包里, 延迟到批尾
            # (全部包应用后)再快照, 保证终局行的胜负双方状态完整 —— 与旧全量导出路径等价
            self.state = "GAME_END"
            self._pending_game_end = True
            return
        if kind == "raw":
            return            # 全量收录: 已入 store.unhandled(随 JSONL 落盘), 不渲染
        if (kind == "play" and self.gs is not None
                and evt.get("actor") == self.gs.friendly_key):
            self.summary.append(self.carddb.name(evt["card_id"]))
        line = chain_line(evt, self.carddb)
        self.chain.append(line)
        self._emit(line, "chain")

    def _fire_pending_game_end(self) -> None:
        """game_end 延迟快照触发点: 树内包全部应用(冲刷)后再快照+导出。"""
        if not self._pending_game_end:
            return
        self._pending_game_end = False
        self._snapshot("game_end")
        self._export_training()

    def _export_training(self) -> None:
        """每局结束自动导出训练样本(仅实时来源; 回放用 import-all 子命令批量做)。"""
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
        except Exception:  # noqa: BLE001
            log.exception("训练样本导出失败")
            self._emit("! 训练样本导出失败")

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
            ui = snapshot_line(self.gs, self.game_no, reason) + "\n" + game_end_line(self.gs)
            self._emit(Msg("game_end", ui=ui, full=block))
        else:
            self._emit(Msg("snapshot", ui=snapshot_line(self.gs, self.game_no, reason),
                           full=block))
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

    def _emit(self, msg: str | Msg, kind: str = "notice") -> None:
        if self.mute:
            return
        self.out(msg if isinstance(msg, Msg) else Msg(kind, msg))
