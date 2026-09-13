"""训练语料层 —— 把每局对局归一化为 JSONL 事件流, 按卡组分目录。

格式: 每局一个 .jsonl, 首行 _meta(对局元信息+双方英雄/胜负/卡组归因),
其后每行一个归一化 packet 事件(完整保留所有字段 —— 这是"所有信息"的
最高保真结构化形式; 训练视图/决策点/状态向量都从它派生, 不再回头解析日志)。

对局事实(玩家/英雄/胜负/留牌/回合)唯一来源 = GameStore(状态层),
本模块不做平行推导(2026-09-13 分层化)。

目录: <training_dir>/<卡组名>/<会话名>_g<序号>.jsonl (+ 同名 .power.log 原始切片)
增量: data/training/_imported.json 记录已导入 (会话|局号), 重复导入自动跳过。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from .adapter import feed_line, new_parser, packet_payload, walk_packets
from .knowledge import deck_timeline, parse_log_time, session_date
from .persist import atomic_write_text

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r'[\\/:*?"<>|]')
_CREATE_GAME_MARK = "GameState.DebugPrintPower() - CREATE_GAME"


def _safe_dirname(name: str) -> str:
    return _UNSAFE.sub("_", name).strip() or "未知卡组"


class CorpusExporter:
    def __init__(self, cfg, carddb) -> None:
        self.cfg = cfg
        self.carddb = carddb
        self.dir = Path(cfg.training_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "_imported.json"

    # ---------- 已导入索引 ----------
    def _load_index(self) -> set:
        if self.index_path.exists():
            try:
                return set(json.loads(self.index_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
        return set()

    def _save_index(self, done: set) -> None:
        atomic_write_text(self.index_path,
                          json.dumps(sorted(done), ensure_ascii=False, indent=0))

    # ---------- 卡组归因: 时间线解析在 knowledge(唯一解释点) ----------
    def _attribute(self, entries, game_time) -> tuple:
        """该局开始时刻之前最近的一次排队记录; 跨午夜视为昨天。"""
        best = None
        for t, name, code in entries:
            adj = t - timedelta(days=1) if (game_time - t).total_seconds() < -43200 else t
            if adj <= game_time and (best is None or adj >= best[0]):
                best = (adj, name, code)
        if best is None and entries:
            best = entries[0]
        return (best[1], best[2]) if best else (None, None)

    # ---------- 单局导出 ----------
    def export_game(self, tree, *, session: str, idx: int,
                    decks_path=None, source: str = "live",
                    store=None, player_manager=None) -> Path | None:
        """store(唯一状态权威)提供对局事实; 未提供时内部构建一个。

        事件体保留原始 packet payload(最高保真); 对局事实(玩家/英雄/
        胜负/留牌/回合)只从 store 读 —— 不再平行推导。
        """
        st = store
        if st is None:
            from .store import GameStore
            st = GameStore(carddb=self.carddb, tree=tree,
                           player_manager=player_manager)
            for pkt, depth in walk_packets(tree):
                st.apply(pkt, depth)
            st.settle()

        events = []
        pet_eids: set = set()
        start_ts = str(tree.ts)

        for p, depth in walk_packets(tree):
            ev = {"t": type(p).__name__, "d": depth, "ts": str(p.ts),
                  **packet_payload(p)}
            ent = ev.get("entity")
            if isinstance(ent, int) and ent in pet_eids:
                continue          # 宠物等装饰实体: 非对局内容, 不入训练语料
            if ev["t"] in ("FullEntity", "ShowEntity"):
                tags = {k: v for k, v in ev.get("tags", [])}
                cid = ev.get("card_id")
                if ((cid and str(cid).startswith("PET_"))
                        or tags.get("CARDTYPE") == "PET"
                        or tags.get("ZONE") == "COSMETIC"):
                    if isinstance(ent, int):
                        pet_eids.add(ent)
                    continue
            events.append(ev)

        entries = deck_timeline(decks_path, day=session_date(session))
        try:
            # 日期锚 = 会话目录名(跨午夜对局不再归错卡组); 解析失败退回"现在"
            game_time = parse_log_time(start_ts, session_date(session))
        except ValueError:
            game_time = datetime.now()
        deck_name, deck_code = self._attribute(entries, game_time)
        decklist = None
        if deck_code:
            try:
                from hearthstone.deckstrings import parse_deckstring
                cards, _h, _f, _s = parse_deckstring(deck_code)
                decklist = {}
                for dbf, n in cards:
                    cid = self.carddb.id_from_dbf(dbf)
                    if cid:
                        decklist[cid] = decklist.get(cid, 0) + n
            except Exception as exc:  # noqa: BLE001
                log.warning("训练样本 deck code 解析失败: %s", exc)

        # 对局事实唯一来源 = store(玩家/英雄/胜负/留牌/回合), 不再平行推导
        facts = st.mulligan_facts()
        players = {pid: {"name": st.name(pid), "player_id": pid,
                         "hero_power": st.hero_power_cid.get(pid)}
                   for pid in st.player_keys()}
        heroes = st.heroes_facts()
        results = st.playstate_facts()
        mulligan = {pid: facts.get(pid, {"offered": [], "kept": [],
                                         "replaced": [], "decided": False})
                    for pid in facts}

        meta = {
            "_meta": True,
            "source": source, "session": session, "game_index": idx,
            "start_ts": start_ts, "turns": st.turn,
            "deck_name": deck_name or "未知卡组", "deck_code": deck_code,
            "decklist": decklist,
            "players": players, "heroes": heroes, "results": results,
            "mulligan": {str(pid): m for pid, m in mulligan.items()},
            "event_count": len(events),
        }

        out_dir = self.dir / _safe_dirname(meta["deck_name"])
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{_safe_dirname(session)}_g{idx:02d}.jsonl"
        body = [json.dumps(meta, ensure_ascii=False, default=str)]
        body += [json.dumps(ev, ensure_ascii=False, default=str) for ev in events]
        atomic_write_text(path, "\n".join(body) + "\n")
        return path

    # ---------- live 导出去重(审计 中#8) ----------
    def export_if_new(self, tree, *, session: str, idx: int,
                      decks_path=None, store=None) -> Path | None:
        """与 import-all 共用 _imported.json 索引: 已收录的 (session|idx) 跳过,
        重复 attach 同一会话不再重复导出。跳过时返回 None。"""
        done = self._load_index()
        key = f"{session}|{idx}"
        if key in done:
            return None
        path = self.export_game(tree, session=session, idx=idx,
                                decks_path=decks_path, source="live", store=store)
        if path is not None:
            done.add(key)
            self._save_index(done)
        return path

    # ---------- 当局原始日志切片 ----------
    def export_raw_log(self, jsonl_path: str | Path | None,
                       lines: list) -> Path | None:
        """当局 Power.log 原始切片, 与训练样本同目录同名(.power.log)。
        回放验收/复现全靠它 —— 结构化事件再全也不如原始日志可信。"""
        if not jsonl_path or not lines:
            return None
        out = Path(jsonl_path).with_suffix(".power.log")
        atomic_write_text(out, "\n".join(lines) + "\n")
        return out

    # ---------- 批量导入 ----------
    def import_all(self) -> None:
        root = self.cfg.logs_dir
        sessions = sorted(Path(root).glob("Hearthstone_*"), key=lambda d: d.name) \
            if Path(root).exists() else []
        done = self._load_index()
        total = 0
        for sess in sessions:
            plog = sess / "Power.log"
            if not plog.exists():
                continue
            parser = new_parser()
            lines = plog.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                feed_line(parser, line)
            # 当局原始日志切片: 按 CREATE_GAME 切段, 与样本同名落盘
            marks = [i for i, l in enumerate(lines) if _CREATE_GAME_MARK in l]
            segs = marks + [len(lines)]
            for i, tree in enumerate(parser.games, 1):
                key = f"{sess.name}|{i}"
                seg = lines[segs[i - 1]:segs[i]]
                existing = list(self.dir.glob(
                    f"*/{_safe_dirname(sess.name)}_g{i:02d}.jsonl"))
                if key in done and existing:
                    # 旧样本补原始日志切片(切片功能上线前导入的没有)
                    if not existing[0].with_suffix(".power.log").exists():
                        self.export_raw_log(existing[0], seg)
                    continue
                try:
                    path = self.export_game(tree, session=sess.name, idx=i,
                                            decks_path=sess / "Decks.log",
                                            source="import",
                                            player_manager=parser.player_manager)
                except Exception as exc:  # noqa: BLE001
                    log.error("导入失败 %s 第%s局: %s", sess.name, i, exc)
                    continue
                if path:
                    self.export_raw_log(path, seg)
                    done.add(key)
                    total += 1
            self._save_index(done)
        self.report()

    def report(self) -> None:
        print(f"── 训练语料 ({self.dir}) ──")
        for d in sorted(self.dir.iterdir()):
            if d.is_dir():
                n = len(list(d.glob("*.jsonl")))
                size = sum(f.stat().st_size for f in d.glob("*.jsonl")) // 1024
                print(f"  {d.name}: {n} 局, {size} KB")
