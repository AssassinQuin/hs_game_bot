"""监听层 —— tail / packet 游标 / FSM / 快照触发(spec 2026-09-07 §2-§3)。

对局状态全部在 GameStore(每局一个, 库驱动); 本层只做采集与输出:
括号行扫描喂 store.hint_*, 平铺游标逐包喂 store.apply, store 衍生事件
经订阅路由到渲染/落盘。PLAY 块延迟由 store 内部处理(apply 的 depth 语义)。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adapter import (game_meta, new_parser, reset_player_manager,
                      resolve_friendly, walk_packets)
from .analysis import EffectAnalyzer
from .effects import EffectCache
from .carddb import CardDB
from .config import Config
from .knowledge import DeckKnowledge, parse_decks_log
from .mulligan_ai import MulliganAdvisor, models_root_for
from .overlay import (KIND_ADVICE, KIND_CHAIN, KIND_GAME_END, KIND_NOTICE,
                      KIND_SNAPSHOT, Msg, TAG_MY, TAG_OPP, TAG_UNKNOWN)
from .persist import SessionStore, atomic_write_text
from .render import chain_line, game_end_line, snapshot_block, snapshot_line
from .store import GameStore

log = logging.getLogger("hsbot.watcher")

from .pipeline import (StreamContext, build_line_pipeline,
                       _CREATE_GAME_MARK, _tail_state)  # noqa: F401  重导出供测试

# store 事件种类 → 输出 Msg 种类(缺省 chain); advice 走独立分色高亮
_MSG_KIND_BY_EVENT = {"mulligan_offer": KIND_ADVICE}


@dataclass
class GameScope:
    """一局的作用域状态。新局 = 整体替换本对象(重置语义),
    杜绝"逐字段覆写"式重置 —— 漏一个字段就是跨局残留。"""
    no: int
    cursor: int = 0                      # 包游标(packet 树平铺位置)
    gs: GameStore | None = None          # 本局状态仓(每局一个, 局终归档丢弃)
    knowledge: DeckKnowledge | None = None
    generic: bool = False                # 通用模式(非所选卡组)
    chain: list = field(default_factory=list)           # 本回合链路行缓冲
    summary: list = field(default_factory=list)         # 本回合打出的卡名
    title_done: bool = False             # 回合标题防重发
    pending_game_end: bool = False       # 终局快照延迟到批尾
    rich_events: list = field(default_factory=list)  # 富化后事件(随快照持久化)
    last_raw_path: Path | None = None    # 已导出的当局切片路径(收尾覆写用)


class SessionScanner:
    """会话发现: 目录名字典序=时间序(NTFS mtime 不可靠)。"""

    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = Path(logs_dir)

    def latest(self) -> Path | None:
        if not self.logs_dir.exists():
            return None
        dirs = sorted(self.logs_dir.glob("Hearthstone_*"), key=lambda d: d.name,
                      reverse=True)
        return dirs[0] if dirs else None


class SnapshotService:
    """快照构建与分发(渲染走 render, 持久化走 SessionStore)——watcher 只做决策。"""

    def __init__(self, cfg, carddb, emit) -> None:
        self.cfg = cfg
        self.carddb = carddb
        self._emit = emit

    def build_and_emit(self, *, st, led, knowledge, deck_name, generic, game_no,
                       chain, summary, rich_events, persist, reason) -> None:
        block = snapshot_block(
            st, led, knowledge=knowledge, deck_name=deck_name, generic=generic,
            game_no=game_no, chain_lines=chain, chain_summary=summary,
            carddb=self.carddb, reason=reason)
        if reason == "game_end":
            end = game_end_line(st)
            nl = chr(10)
            self._emit(Msg(KIND_GAME_END,
                           ui=snapshot_line(st, game_no, reason) + nl + end,
                           full=block + nl + end))
        else:
            self._emit(Msg(KIND_SNAPSHOT, ui=snapshot_line(st, game_no, reason),
                           full=block))
        if persist is None:
            return
        payload = st.to_dict(reason)
        if led is not None:
            payload["ledger"] = {
                "in_hand": dict(led.in_hand), "used": dict(led.used),
                "remaining": dict(led.remaining), "deck_actual": led.deck_actual,
                "known_top": led.known_top, "known_bottom": led.known_bottom,
            }
        payload["chain_summary"] = summary
        payload["recent_events"] = rich_events[-100:]
        if st.unhandled:
            payload["unhandled"] = list(st.unhandled)[-50:]   # deque 不可切片
        persist.write_snapshot(payload)


class TrainingExporter:
    """训练样本(语料 jsonl + 当局原始切片)的导出与收尾。

    live 导出与 import-all 共用 _imported.json 索引(审计 中#8):
    重复 attach 同一会话时已导过的 (session|局号) 自动跳过。"""

    def __init__(self, cfg, carddb) -> None:
        self.cfg = cfg
        self.carddb = carddb
        self._corpus = None

    def export(self, *, tree, store, session, idx, decks_path):
        """返回 (样本路径|None(已导过/失败), corpus)。"""
        corpus = self._ensure_corpus()
        path = corpus.export_if_new(tree, session=session, idx=idx,
                                    decks_path=decks_path, store=store)
        return path, corpus

    def finalize_slice(self, *, last_raw_path, game_lines) -> None:
        if self._corpus is None or not last_raw_path or not game_lines:
            return
        try:
            self._corpus.export_raw_log(last_raw_path, game_lines)
        except OSError:
            pass

    def _ensure_corpus(self):
        if self._corpus is None:
            from .corpus import CorpusExporter
            self._corpus = CorpusExporter(self.cfg, self.carddb)
        return self._corpus


class AutoModelTrainer:
    """局终后台自动增量训练(留牌三级评分器 + trainer 价值模型)。

    与训练系统解耦: 子进程调用训练入口(python -m trainer mulligan / python -m
    trainer), 失败只进诊断日志不影响监控; 忙则合并为一次待办, 不堆积线程。
    产物按版本落盘, MulliganAdvisor 按 mtime 自动切换 —— 下一局即用新模型。"""

    def __init__(self, cfg, emit=None) -> None:
        self.cfg = cfg
        self._emit = emit
        self._busy = False
        self._pending = False
        self._lock = threading.Lock()

    def commands(self) -> list:
        root = Path(__file__).resolve().parents[1]
        cfg_arg = ["--config", str(self.cfg.source)] \
            if Path(str(self.cfg.source)).exists() else []
        data = ["--data-dir", str(self.cfg.data_dir), "--deck", self.cfg.deck_name]
        mull = ["-m", "trainer", *cfg_arg, *data]
        return [
            [sys.executable, *mull, "mulligan", "train"],
            [sys.executable, *mull, "material"],
            [sys.executable, *mull, "train"],
        ]

    def request(self) -> None:
        if not getattr(self.cfg, "auto_train_models", True):
            return
        with self._lock:
            if self._busy:
                self._pending = True
                return
            self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            versions = []
            for argv in self.commands():
                try:
                    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
                    r = subprocess.run(
                        argv, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=900,
                        cwd=str(Path(__file__).resolve().parents[1]), env=env)
                    if r.returncode != 0:
                        log.warning("自动增量训练失败(%s): %s",
                                    argv[-1], (r.stderr or "")[-300:])
                        continue
                    m = re.search(r"模型已保存: \S*?(v\d+)", r.stdout or "")
                    if m:
                        versions.append(m.group(1))
                except Exception as exc:  # noqa: BLE001
                    log.warning("自动增量训练异常(%s): %s", argv[-1], exc)
            if self._emit is not None:
                msg = ("后台增量训练完成: 留牌模型 "
                       + ("→".join(versions) if versions else "无变化"))
                self._emit(msg)
            with self._lock:
                if self._pending:
                    self._pending = False
                    continue
                self._busy = False
                return


class Watcher:
    def __init__(self, cfg: Config, carddb: CardDB, out=None, hub=None) -> None:
        self.cfg = cfg
        self.carddb = carddb
        # 卡牌/效果解析层(渲染前富化); IR 缓存持久化到缓存目录
        # 留牌建议器(mulligan_advice 开关): 读 LATEST 模型, 富化 mulligan_offer 事件
        self.mulligan_ai = MulliganAdvisor(
            models_root_for(cfg.data_dir, cfg.deck_name), cfg.deck_name, carddb,
            prior_path=Path(cfg.data_dir) / "mulligan_prior.yaml") \
            if getattr(cfg, "mulligan_advice", True) else None
        self.analyzer = EffectAnalyzer(
            carddb, cache=EffectCache(Path(cfg.cache_dir) / "effects.json"),
            mulligan=self.mulligan_ai)
        self.out = out if out is not None else (lambda m: print(m.ui))
        self.hub = hub

        self.parser = new_parser()
        self.lines = 0
        self.game_count = 0          # 已见过的局数 = len(parser.games)
        self.mute = False            # 实时追平历史时静音
        self.state = "IDLE"          # IDLE / IN_GAME / GAME_END

        self.game_no = 0
        self.match: GameScope | None = None      # 当前局作用域(新局整体替换)
        self.store: SessionStore | None = None   # 持久化(勿与 GameStore 混淆)
        self.session_name = ""
        self._live = False                   # 实时来源(回放不自动导训练样本)
        self.scanner = SessionScanner(cfg.logs_dir)            # 会话发现
        self.snapshot_service = SnapshotService(cfg, carddb, self._emit)
        self.exporter = TrainingExporter(cfg, carddb)          # 训练导出
        # 局终后台自动增量训练(留牌评分器 + trainer 价值模型), 子进程解耦
        self.model_trainer = AutoModelTrainer(cfg, self._emit)
        # ---- 日志流状态唯一维护点(责任链的共享上下文) ----
        self.stream = StreamContext()
        self.pipeline = build_line_pipeline(
            parser_getter=lambda: self.parser,
            on_create_boundary=self._on_create_boundary,
            friendly_getter=self._friendly_pid,
            on_draw=self._hint_draw_pid)
        self.decks_path: Path | None = None

    # ---- 责任链回调 ----
    def _on_create_boundary(self) -> None:
        """CREATE_GAME 边界: 结算上一局切片 + 新局切片缓冲 + 重置 player manager。"""
        self._finalize_game_log()
        reset_player_manager(self.parser)

    def _friendly_pid(self):
        if self.match and self.match.gs is not None:
            return self.match.gs.friendly_key
        return None

    def _hint_draw_pid(self, eid: int, cid: str, pid: int) -> None:
        # 行级抽牌 hint 批尾统一派发: 喂行阶段 PLAY 块的包尚未被应用,
        # store 的"play 先发"扣留接不住 hint —— 先攒住, 包处理完再发(顺序归位)
        self.stream.pend_draws.append((eid, cid, pid))

    def _drain_pend_draws(self) -> None:
        if not (self.match and self.match.gs is not None
                and self.stream.pend_draws):
            return
        draws, self.stream.pend_draws = self.stream.pend_draws, []
        for eid, cid, pid in draws:
            self.match.gs.hint_draw(eid, cid, pid)

    # ---- 局作用域只读别名(兼容旧调用点/测试) ----
    @property
    def gs(self) -> GameStore | None:
        return self.match.gs if self.match else None

    @property
    def knowledge(self) -> DeckKnowledge | None:
        return self.match.knowledge if self.match else None

    # ================= 源 =================
    REPLAY_CHUNK = 40  # 回放批次: 状态新鲜度 vs 速度折衷(实时模式批次天然极小)

    def run_replay(self, path: str | Path) -> None:
        path = Path(path)
        self.parser = new_parser()
        self.stream.tail_open = True
        self.match = None
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
            try:
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
                text = self.stream.partial + raw.decode("utf-8", errors="replace")
                if text and not text.endswith("\n"):
                    cut = text.rfind("\n") + 1
                    self.stream.partial = text[cut:]
                    text = text[:cut]
                else:
                    self.stream.partial = ""
                if text:
                    self._feed_many(text.splitlines())
                    self._after_batch()
            except Exception:  # noqa: BLE001  单次轮询失败绝不杀死监控线程(2026-09-13 实测:
                # 追平期终局快照异常曾令线程死亡, 悬浮窗静默假死)
                log.exception("轮询异常(已忽略, 继续监控)")


    def _find_latest_session(self) -> Path | None:
        """最新会话目录。目录名 Hearthstone_YYYY_MM_DD_HH_MM_SS, 字典序即时间序,
        不依赖文件系统 mtime(目录 mtime 不随 Power.log 追加更新, 不可靠)。"""
        return self.scanner.latest()

    def _attach(self, log_path: Path) -> int:
        """从头解析当前文件追平历史, 追平前静音(不把历史当实时)。
        文件尚不存在时直接返回 0(等待创建)。追平失败不抛出——跳过余下历史,
        从文件尾继续实时(线程死亡=悬浮窗静默假死, 比缺一段历史糟得多)。"""
        self.parser = new_parser()
        self.game_count = 0
        self.match = None             # 会话切换: 局作用域整体作废(历史不导训练)
        self.stream.game_lines = None  # 旧切片作废
        self.stream.pend_cid = {}      # 跨会话线索/真名/抽牌hint一并作废
        self.stream.pend_ctrl = {}
        self.stream.pend_names = []
        self.stream.pend_draws = []
        self.state = "IDLE"
        self.stream.partial = ""
        self.stream.tail_open = True
        self.mute = True
        offset = 0
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            failed = False
            for i in range(0, len(lines), 5000):
                try:
                    self._feed_many(lines[i:i + 5000])
                    self._after_batch()
                except Exception:  # noqa: BLE001
                    log.exception("追平历史失败(第 %d 行附近), 跳过余下历史继续实时", i)
                    failed = True
                    break
            if not failed and self.state == "IN_GAME":
                try:
                    self._snapshot("flush")
                except Exception:  # noqa: BLE001
                    log.exception("追平快照失败(忽略)")
            offset = len(text.encode("utf-8"))
        self.mute = False
        return offset

    # ================= 消费 =================
    def _feed_many(self, lines) -> None:
        for line in lines:
            self.lines += 1
            if self.match and self.match.gs is not None:
                self.match.gs.note_progress(self.lines)
            self.pipeline.feed(line, self.stream)

    def _after_batch(self) -> None:
        self._detect_game()
        if self.match and self.match.gs is not None:               # 括号线索刷进状态仓(幂等)
            self.match.gs.apply_hints(self.stream.pend_cid,
                                      self.stream.pend_ctrl)
        if self.parser.games:
            self._process_tree(self.parser.games[-1])
        self._drain_pend_draws()               # 批内包已应用/settle: hint 抽牌此时发不越序
        self.analyzer.cache.save()             # IR 增量回写(dirty 才落盘)
        if self.match and self.match.gs is not None and self.match.gs.friendly_key is None:
            self.match.gs.note_friendly(resolve_friendly(self.parser, self.cfg.battletag))
        if self.match and self.match.gs is not None and self.stream.pend_names:
            # 真名绑定: 本方战网名 → friendly, 其余真名 → 对手
            # (开局对手常为 UNKNOWN HUMAN PLAYER, 真名在留牌确认等后续行才出现)
            me = self.cfg.battletag
            if self.match.gs.friendly_key is not None:
                opp = self.match.gs.opponent_key()
                for nm in self.stream.pend_names:
                    if nm == me:
                        self.match.gs.note_player_name(self.match.gs.friendly_key, nm)
                    elif opp is not None:
                        self.match.gs.note_player_name(opp, nm)
                self.stream.pend_names.clear()
        if (self.match and self.match.gs is not None and not self.match.title_done
                and self.match.gs.friendly_key is not None
                and self.match.gs.current == self.match.gs.friendly_key
                and self.state == "IN_GAME"):
            # 先手局: 留牌阶段friendly才解析出来, 而第1回合已在进行 —— 补发标题
            self.match.title_done = True
            self._emit(f"──── 第{self.game_no}局 · 我的第1回合开始 (T1) │ "
                       f"{self.match.gs.mana_text(self.match.gs.friendly_key)} ────")
        if self.match is not None and self.match.pending_game_end \
                and self.parser.games:
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
        """新局 = 替换整个局作用域(GameScope) —— 重置语义, 不逐字段覆盖。"""
        self.game_no += 1
        self.match = GameScope(no=self.game_no)
        self.stream.pend_cid = {}        # 新局边界: 跨局流缓冲清空
        self.stream.pend_ctrl = {}
        self.stream.pend_draws = []
        self.state = "IN_GAME"
        self.match.gs = GameStore(carddb=self.carddb, battletag=self.cfg.battletag,
                                  tree=self.parser.games[-1] if self.parser.games else None,
                                  player_manager=self.parser.player_manager,
                                  draw_dedup=self.cfg.draw_dedup_seconds)
        self.match.gs.set_meta(game_meta(self.parser))   # 一次会话内不变, 新局注入一次
        self.match.gs.subscribe(self._route)
        if self.store is not None:
            self.store.new_game()
            if self.hub is not None:      # 悬浮窗内容同步落盘(本地记录)
                self.hub.set_file(self.store.path.with_suffix(".log"))
        self.match.knowledge = self._load_knowledge()
        note = "" if self.match.knowledge else " (Decks.log 中未找到卡组代码, 通用模式)"
        self._emit(f"── 新对局 #{self.game_no} ── 所选卡组: {self.cfg.deck_name}{note}")

    def _load_knowledge(self) -> DeckKnowledge | None:
        code = self.cfg.deck_code
        if not code and self.decks_path is not None:
            code = parse_decks_log(self.decks_path).get(self.cfg.deck_name)
        if not code:
            return None
        k = DeckKnowledge.from_code(code, self.carddb, self.cfg.deck_name,
                                    analyzer=self.analyzer)
        try:
            import json as _json
            atomic_write_text(self.cfg.data_dir / "decks" / "decklist.json",
                              _json.dumps({"name": self.cfg.deck_name, "code": code,
                                           "cards": k.decklist},
                                          ensure_ascii=False, indent=1))
        except OSError:
            pass
        return k

    def _process_tree(self, tree, flush: bool = False) -> None:
        if self.match is None or self.match.gs is None:
            return
        flat = list(walk_packets(tree))
        i = self.match.cursor
        # 扣留最后一个包: FULL/SHOW_ENTITY/Choices 的子标签/子行在包注册之后
        # 才陆续到达, 立即处理会拿到空标签。留到"行形证明写完整"(_tail_open=False,
        # 组边界/BLOCK_END/下一包头)或 flush 时再处理。
        if flush or not self.stream.tail_open:
            limit = len(flat)
        else:
            limit = max(self.match.cursor, len(flat) - 1)
        while i < limit:
            pkt, depth = flat[i]
            try:
                self.match.gs.apply(pkt, depth)
            except Exception as exc:  # noqa: BLE001  单包事件失败不拖垮监控
                log.exception("事件处理异常")
                self._emit(f"! 事件处理异常: {type(exc).__name__}: {exc}")
            i += 1
        self.match.cursor = limit
        self.match.gs.settle()               # 块已收口的挂起 PLAY 立即发出

    # ================= store 事件路由(命令式外壳) =================
    def _route(self, evt: dict) -> None:
        kind = evt.get("kind")
        if kind == "turn_start":
            friendly = self.match.gs.friendly_key if self.gs else None
            if evt["first"]:
                if evt["actor"] == friendly:
                    self.match.title_done = True
                    self._emit(f"──── 第{self.game_no}局 · 我的第1回合开始 (T1) │ "
                               f"{self.match.gs.mana_text(evt['actor'])} ────")
                return
            if friendly is not None and evt["actor"] == friendly:
                n, total = evt["my_turn_no"], evt["total_turn"]
                self.match.title_done = True
                self._emit(f"──── 第{self.game_no}局 · 我的第{n}回合开始 (T{total}) │ "
                           f"{self.match.gs.mana_text(evt['actor'])} ────")
            else:
                self._snapshot("turn_end")
            return
        if kind == "game_end":
            # 不立即快照: 对手的终局 PLAYSTATE 在同批稍后的包里, 延迟到批尾
            # (全部包应用后)再快照, 保证终局行的胜负双方状态完整 —— 与旧全量导出路径等价
            self.state = "GAME_END"
            self.match.pending_game_end = True
            return
        if kind == "raw":
            return            # 全量收录: 已入 store.unhandled(随 JSONL 落盘), 不渲染
        if (kind == "play" and self.match and self.match.gs is not None
                and evt.get("actor") == self.match.gs.friendly_key):
            is_my_play = True
        else:
            is_my_play = False
        evt = self.analyzer.enrich(evt, self.gs)   # 解析层: 渲染前实时富化
        if is_my_play:
            # 富化之后才记账: 若将来推断改变 card_id, 摘要与链路行仍一致(审计 低#10)
            self.match.summary.append(self.carddb.name(evt["card_id"]))
        if len(self.match.rich_events) < 400:
            self.match.rich_events.append(dict(evt))   # 富化结果随快照持久化
        line = chain_line(evt, self.carddb)
        self.match.chain.append(line)
        msg_kind = _MSG_KIND_BY_EVENT.get(kind, KIND_CHAIN)
        if msg_kind == KIND_CHAIN:
            actor, friendly = evt.get("actor"), evt.get("friendly")
            tag = TAG_MY if actor is not None and actor == friendly else \
                (TAG_OPP if friendly is not None and actor is not None
                 else TAG_UNKNOWN)
        else:
            tag = ""                     # advice 等专用分色: 不叠加人物色
        self._emit(Msg(msg_kind, line, tag=tag), msg_kind)

    def _fire_pending_game_end(self) -> None:
        """game_end 延迟快照触发点: 树内包全部应用(冲刷)后再快照+导出。"""
        if not self.match.pending_game_end:
            return
        self.match.pending_game_end = False
        self._snapshot("game_end")
        self._export_training()
        if self._live:                       # 语料已落盘 → 后台增量训练(忙则合并)
            self.model_trainer.request()

    def _export_training(self) -> None:
        """每局结束自动导出训练样本(仅实时来源; 回放用 import-all 子命令批量做)。
        已导过的 (session|局号) 静默跳过(_imported.json 只导新语义)。"""
        if not (self.cfg.auto_training and self._live and self.parser.games):
            return
        try:
            path, corpus = self.exporter.export(
                tree=self.parser.games[-1], store=self.match.gs,
                session=self.session_name or "live", idx=self.game_no,
                decks_path=self.decks_path)
            if path is None:
                return                     # 重复 attach: 该局已导过
            raw = corpus.export_raw_log(path, self.stream.game_lines or [])
            if self.match:
                self.match.last_raw_path = raw
            if raw:
                self._emit(f"训练样本已导出: {path} (+{raw.name})")
            else:
                self._emit(f"训练样本已导出: {path}")
        except Exception:  # noqa: BLE001
            log.exception("训练样本导出失败")
            self._emit("! 训练样本导出失败")

    def _finalize_game_log(self) -> None:
        """新局 CREATE_GAME 到来: 上一局切片已含全部收尾行, 覆写一次补完整。"""
        last = self.match.last_raw_path if self.match else None
        gl = self.stream.game_lines
        self.exporter.finalize_slice(last_raw_path=last, game_lines=gl)
        if self.match:
            self.match.last_raw_path = None

    def _snapshot(self, reason: str) -> None:
        if self.match is None or self.match.gs is None:
            return
        if not self.match.gs.meta:
            # DebugPrintGame 在真实日志里位于 CREATE_GAME 块之后(实测 +540 行):
            # 新局注入时可能尚无内容, 此处补注一次; 非空后不再重算 game_meta
            self.match.gs.set_meta(game_meta(self.parser))
        if self.match and self.match.knowledge is not None \
                and self.knowledge.mismatch_count(self.match.gs.played_cids) >= 2:
            self.match.generic = True
        led = self.knowledge.rebuild(self.gs) if self.match.knowledge else None
        # 快照构建/分发/持久化委托给专职服务(含 recent_events 富化字段)
        self.snapshot_service.build_and_emit(
            st=self.gs, led=led, knowledge=self.match.knowledge,
            deck_name=self.cfg.deck_name, generic=self.match.generic,
            game_no=self.game_no, chain=self.match.chain,
            summary=self.match.summary, rich_events=self.match.rich_events,
            persist=self.store, reason=reason)
        self.match.chain, self.match.summary = [], []
        self.match.rich_events = []

    def _emit(self, msg: str | Msg, kind: str = KIND_NOTICE) -> None:
        if self.mute:
            return
        self.out(msg if isinstance(msg, Msg) else Msg(kind, msg))
