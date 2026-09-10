"""训练语料层 —— 把每局对局归一化为 JSONL 事件流, 按卡组分目录。

格式: 每局一个 .jsonl, 首行 _meta(对局元信息+双方英雄/胜负/卡组归因),
其后每行一个归一化 packet 事件(完整保留所有字段 —— 这是"所有信息"的
最高保真结构化形式; 训练视图/决策点/状态向量都从它派生, 不再回头解析日志)。

目录: <training_dir>/<卡组名>/<会话名>_g<序号>.jsonl
增量: data/training/_imported.json 记录已导入 (会话|局号), 重复导入自动跳过。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from hearthstone.enums import GameTag, PlayState

from .adapter import feed_line, new_parser, packet_payload, walk_packets

log = logging.getLogger(__name__)

_TS_RE = re.compile(r"^[IWD] (\d[\d:.]+) ?(.*)$")
_CODE_RE = re.compile(r"^AA[A-Za-z0-9+/=]{30,}$")
_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def _safe_dirname(name: str) -> str:
    return _UNSAFE.sub("_", name).strip() or "未知卡组"


def _parse_log_time(s: str) -> datetime:
    """'20:59:52.6130042' -> 今日该时刻。"""
    s = s.split(".")[0]
    return datetime.combine(datetime.now().date(), datetime.strptime(s, "%H:%M:%S").time())


class CorpusExporter:
    def __init__(self, cfg, carddb) -> None:
        self.cfg = cfg
        self.carddb = carddb
        self.dir = Path(cfg.training_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "_imported.json"

    # ---------- 已导入索引 ----------
    def _load_index(self) -> set[str]:
        if self.index_path.exists():
            try:
                return set(json.loads(self.index_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
        return set()

    def _save_index(self, done: set[str]) -> None:
        self.index_path.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=0),
                                   encoding="utf-8")

    # ---------- Decks.log 时间线 → 卡组归因 ----------
    def _deck_timeline(self, decks_path: str | Path | None) -> list[tuple[datetime, str, str]]:
        """[(时刻, 卡组名, deck code)], 只取每次'Finding Game With Deck'序列。"""
        entries: list[tuple[datetime, str, str]] = []
        if not decks_path or not Path(decks_path).exists():
            return entries
        pending_t: datetime | None = None
        pending_name: str | None = None
        for raw in Path(decks_path).read_text(encoding="utf-8", errors="replace").splitlines():
            m = _TS_RE.match(raw.strip())
            t_str, content = (m.group(1), m.group(2)) if m else (None, raw.strip())
            if content.startswith("Finding Game With Deck") and t_str:
                pending_t, pending_name = _parse_log_time(t_str), None
            elif content.startswith("### ") and pending_t is not None:
                pending_name = content[4:].strip()
            elif pending_t is not None and pending_name and _CODE_RE.match(content):
                entries.append((pending_t, pending_name, content))
                pending_t = pending_name = None
        return entries

    def _attribute(self, entries, game_time: datetime) -> tuple[str | None, str | None]:
        """该局开始时刻之前最近的一次排队记录; 跨午夜视为昨天。"""
        best: tuple[datetime, str, str] | None = None
        for t, name, code in entries:
            adj = t - timedelta(days=1) if (game_time - t).total_seconds() < -43200 else t
            if adj <= game_time and (best is None or adj >= best[0]):
                best = (adj, name, code)
        if best is None and entries:
            best = entries[0]
        return (best[1], best[2]) if best else (None, None)

    # ---------- 单局导出 ----------
    def export_game(self, tree, *, session: str, idx: int,
                    decks_path=None, source: str = "live") -> Path | None:
        events = []
        players: dict[int, dict] = {}
        heroes: dict[int, str] = {}
        results: dict[int, str] = {}
        mulligan: dict[int, dict] = {}
        choice_pid: dict[int, int] = {}
        eid2cid: dict[int, str] = {}
        max_turn = 0
        start_ts = str(tree.ts)

        for p, depth in walk_packets(tree):
            ev = {"t": type(p).__name__, "d": depth, "ts": str(p.ts),
                  **packet_payload(p)}
            events.append(ev)
            if ev["t"] == "CreateGame" and not players:
                for pl in ev.get("players", []):
                    ref = pl.get("entity") or {}
                    pid = pl.get("player_id") or ref.get("player_id")
                    players[pid] = {"name": ref.get("name"), "player_id": pid,
                                    "entity_id": ref.get("entity_id"), "lo": pl.get("lo")}
            elif ev["t"] in ("FullEntity", "ShowEntity"):
                if ev.get("card_id") and isinstance(ev.get("entity"), int):
                    eid2cid[ev["entity"]] = ev["card_id"]
            elif ev["t"] == "Choices" and ev.get("type") == "MULLIGAN":
                ent = ev.get("entity")
                pid = ent.get("player_id") if isinstance(ent, dict) else None
                if pid is not None:
                    mulligan.setdefault(pid, {"offered": list(ev.get("choices") or []),
                                              "kept": [], "decided": False})
                    choice_pid[ev.get("id")] = pid
            elif ev["t"] in ("SendChoices", "ChosenEntities") \
                    and ev.get("type") == "MULLIGAN":
                pid = choice_pid.get(ev.get("id"))
                if pid is not None:
                    m = mulligan.setdefault(pid, {"offered": [], "kept": [],
                                                  "decided": False})
                    m["kept"] = [c for c in (ev.get("choices") or []) if isinstance(c, int)]
                    m["decided"] = True   # 对手的留牌决定不广播, kept=[]≠全换
            elif ev["t"] == "FullEntity":
                tags = {k: v for k, v in ev.get("tags", [])}
                if tags.get("CARDTYPE") == "HERO" and tags.get("CONTROLLER") is not None:
                    heroes.setdefault(tags["CONTROLLER"], ev.get("card_id"))
            elif ev["t"] == "TagChange":
                if ev.get("tag") == "TURN":
                    try:
                        max_turn = max(max_turn, int(ev.get("value") or 0))
                    except (TypeError, ValueError):
                        pass
                elif ev.get("tag") == "PLAYSTATE":
                    ent = ev.get("entity")
                    ent2pid = {v.get("entity_id"): k for k, v in players.items()}
                    pid = ent.get("player_id") if isinstance(ent, dict) \
                        else ent2pid.get(ent) if isinstance(ent, int) else None
                    if pid is not None:
                        val = ev.get("value")
                        try:
                            results[pid] = val if isinstance(val, str) \
                                else PlayState(int(val)).name
                        except (TypeError, ValueError):
                            pass

        entries = self._deck_timeline(decks_path)
        try:
            game_time = datetime.combine(datetime.now().date(),
                                         datetime.strptime(start_ts.split(".")[0], "%H:%M:%S").time())
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

        def _mull_meta(m: dict) -> dict:
            offered, kept = m.get("offered") or [], m.get("kept") or []
            decided = bool(m.get("decided"))

            def is_coin(e: int) -> bool:
                cid = eid2cid.get(e)
                return bool(cid) and ("COIN" in cid.upper() or cid.upper() == "GAME_005")

            # decided=False(对手决定不广播)时不推算 replaced, 避免污染训练数据
            replaced = [e for e in offered if e not in kept and not is_coin(e)] \
                if decided else []
            names = lambda eids: [eid2cid.get(e) for e in eids]  # noqa: E731
            return {"decided": decided,
                    "offered_eids": offered, "kept_eids": kept,
                    "replaced_eids": replaced,
                    "offered": names(offered), "kept": names(kept),
                    "replaced": names(replaced)}

        meta = {
            "_meta": True,
            "source": source, "session": session, "game_index": idx,
            "start_ts": start_ts, "turns": max_turn,
            "deck_name": deck_name or "未知卡组", "deck_code": deck_code,
            "decklist": decklist,
            "players": players, "heroes": heroes, "results": results,
            "mulligan": {str(pid): _mull_meta(m) for pid, m in mulligan.items()},
            "event_count": len(events),
        }

        out_dir = self.dir / _safe_dirname(meta["deck_name"])
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{_safe_dirname(session)}_g{idx:02d}.jsonl"
        with path.open("w", encoding="utf-8") as fp:
            fp.write(json.dumps(meta, ensure_ascii=False, default=str) + "\n")
            for ev in events:
                fp.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        return path

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
            for line in plog.read_text(encoding="utf-8", errors="replace").splitlines():
                feed_line(parser, line)
            for i, tree in enumerate(parser.games, 1):
                key = f"{sess.name}|{i}"
                if key in done:
                    continue
                try:
                    path = self.export_game(tree, session=sess.name, idx=i,
                                            decks_path=sess / "Decks.log", source="import")
                except Exception as exc:  # noqa: BLE001
                    log.error("导入失败 %s 第%s局: %s", sess.name, i, exc)
                    continue
                if path:
                    done.add(key)
                    total += 1
            self._save_index(done)
        self._save_index(done)
        self.report()

    def report(self) -> None:
        print(f"── 训练语料 ({self.dir}) ──")
        for d in sorted(self.dir.iterdir()):
            if d.is_dir():
                n = len(list(d.glob("*.jsonl")))
                size = sum(f.stat().st_size for f in d.glob("*.jsonl")) // 1024
                print(f"  {d.name}: {n} 局, {size} KB")
