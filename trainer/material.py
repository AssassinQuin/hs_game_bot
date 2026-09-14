"""素材构建 —— 重放语料原始日志切片 → (回合快照, 本回合动作, 胜负) 流。

输入  = bot 产出的语料目录(data/training/<卡组>/*.power.log 原始切片, 文件接合点);
输出  = trainer/data/<deck>/material.jsonl(每行一个决策点: 我的回合开始时的
        状态快照 + 该回合内实际动作序列 + 终局胜负)。
重放走 hsbot 的状态解释层(store/adapter) —— 状态语义与 bot 单点一致, 不二写。
每次全量重建(幂等): 切片量级下成本恒小, 无增量状态漂移。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from hsbot.adapter import (feed_line, new_parser, reset_player_manager,
                           resolve_friendly, walk_packets)
from hsbot.consts import is_coin
from hsbot.store import GameStore

from .states import snapshot

ACTION_KINDS = {"play", "attack", "hero_power"}   # 计入"本回合动作"的事件
_META = "_meta.json"
_META_V2 = "_meta_v2.json"
_CREATE_GAME_MARK = "GameState.DebugPrintPower() - CREATE_GAME"


def _decklist_for(path: Path) -> dict | None:
    """切片 → 同 stem jsonl 首行 meta 的 decklist(缺文件/坏行 → None)。
    stem 手工剥 .power.log 双后缀(Path.stem 只剥一层, 实测坑 #34)。"""
    name = path.name
    stem = name[:-len(".power.log")] if name.endswith(".power.log") else name
    jl = path.parent / f"{stem}.jsonl"
    if not jl.exists():
        return None
    try:
        meta = json.loads(jl.read_text(encoding="utf-8").split("\n", 1)[0])
        return meta.get("decklist") or None
    except (ValueError, OSError):
        return None


def _replay_slice(path: Path, carddb, battletag: str,
                  collect_v2: bool = False):
    """一个原始切片 → 决策点行。v1(默认): 回合行列表(结构零变化)。
    collect_v2=True: (回合行, 决策行, 结果行) 三类行——回合行带
    drawn_this_turn(我方抽牌, 排除换入窗口), 决策行带重放推导的 replaced_in。

    时序: 回合开始瞬间拍快照, 之后到下个回合开始之间的事件都是"该回合动作";
    对手回合只冲账不立新行(素材 = 我的决策点)。"""
    parser = new_parser()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        # 跨局边界重置 manager(同 watcher 边界协议): 换边局的 名字→pid 映射
        # 会撞 InconsistentPlayerIdError 被逐行吞掉, 友方解析随之下错位
        if _CREATE_GAME_MARK in line and parser.games:
            reset_player_manager(parser)
        feed_line(parser, line)
    done: list[dict] = []                        # 已收口、待补胜负的行
    out_rows: list[dict] = []
    turn_rows: list[dict] = []
    mull_rows: list[dict] = []
    result_rows: list[dict] = []
    pend: dict | None = None                     # 当前累积中的我的回合
    actions: list = []
    drawn: list = []
    st_holder: list = []

    def on_event(evt: dict) -> None:
        nonlocal pend, actions, drawn
        st = st_holder[0]
        kind = evt.get("kind")
        if kind == "turn_start" and st.friendly_key is not None:
            if pend is not None:                 # 收上一段(我的上一回合)
                pend["actions"] = actions
                if collect_v2:
                    pend["drawn_this_turn"] = drawn
                done.append(pend)
            actions = []
            drawn = []
            snap = snapshot(st, carddb)
            pend = ({"snap": snap, "actions": [],
                     "src": f"{path.name}#g{gi}T{snap['turn']}"}
                    if snap is not None and snap["my_turn"] else None)
        elif kind in ACTION_KINDS and pend is not None:
            cid = evt.get("card_id")
            actions.append({"k": kind, "cid": cid, "cost": carddb.cost(cid)})
        elif (collect_v2 and kind == "draw" and pend is not None
                and evt.get("actor") == st.friendly_key):
            # 换入窗口内的抽牌归决策行 replaced_in(mulligan_facts), 不重复计
            m = st.mulligan.get(st.friendly_key)
            if not (m is not None and m.decided and not m.closed):
                drawn.append(evt.get("card_id"))

    for gi, tree in enumerate(parser.games, 1):
        pend = None            # 审计 2026-09-14 中#14: 局间残留会把上局尾
        actions = []           # 回合行挤进下局 done、被下局胜负覆写/重复入列
        drawn = []
        st = GameStore(carddb=carddb, battletag=battletag, tree=tree,
                       player_manager=parser.player_manager)
        # 友方解析与 watcher 同源(战网名优先, 逐局自己的树): 不依赖"恰好
        # 广播过留牌"才定主客, 也不受后续局换边影响
        st.note_friendly(resolve_friendly(parser, battletag, tree=tree))
        st_holder.clear()
        st_holder.append(st)
        done.clear()
        st.subscribe(on_event)
        for pkt, depth in walk_packets(tree):
            st.apply(pkt, depth)
        st.settle()
        if st.friendly_key is None:
            continue
        if pend is not None:                     # 收尾: 终局前最后一个我的回合
            pend["actions"] = actions
            if collect_v2:
                pend["drawn_this_turn"] = drawn
            done.append(pend)
        result = st.playstate(st.friendly_key)
        if result not in ("WON", "LOST"):
            continue
        win = 1 if result == "WON" else 0
        if not collect_v2:
            for row in done:
                row["result"] = win
            out_rows.extend(done)
            continue
        gkey = f"{path.name}#g{gi}"
        for row in done:
            if row["snap"]["turn"] < 1:          # 留牌阶段(T0)不是决策点
                continue
            row.update({"row": "turn", "game": gkey, "result": win})
            turn_rows.append(row)
        mf = st.mulligan_facts().get(st.friendly_key)
        if mf and mf.get("decided") and mf.get("offered"):
            offered_all = [c for c in mf["offered"] if c]
            offered = [c for c in offered_all if not is_coin(c)]
            if offered:
                hero = st.hero(st.opponent_key())
                opp_class = (carddb.card_class(hero.card_id)
                             if hero is not None and hero.card_id
                             else None) or "UNKNOWN"
                mull_rows.append({
                    "row": "mulligan", "game": gkey,
                    "offered": offered,
                    "kept": [c for c in mf.get("kept") or []
                             if c and not is_coin(c)],
                    "replaced_in": [c for c in mf.get("replaced_in") or []
                                    if c and not is_coin(c)],
                    "coin": int(any(is_coin(c) for c in offered_all)),
                    "opp_class": opp_class,
                    "decklist": _decklist_for(path),
                    "result": win})
                result_rows.append({"row": "result", "game": gkey,
                                    "result": win})
    if collect_v2:
        return turn_rows, mull_rows, result_rows
    return out_rows


def build_material_v2(corpus_dir: Path | str, deck: str, out_dir: Path | str,
                      carddb, battletag: str) -> dict:
    """语料切片 → v3 素材 material_v2.jsonl(全量幂等重建)。
    三类行: turn(快照+本回合动作+drawn_this_turn) / mulligan(决策行, 重放
    推导 replaced_in) / result。返回统计。"""
    deck_dir = Path(corpus_dir) / deck
    slices = sorted(deck_dir.glob("*.power.log")) if deck_dir.is_dir() else []
    turn_rows: list[dict] = []
    mull_rows: list[dict] = []
    result_rows: list[dict] = []
    skip = Counter()
    for path in slices:
        try:
            t, m, r = _replay_slice(path, carddb, battletag, collect_v2=True)
        except Exception as exc:  # noqa: BLE001  坏切片: 计数跳过不中断
            skip[f"重放失败({type(exc).__name__})"] += 1
            continue
        if not m:
            skip["主客未定/无结果/未广播决定"] += 1
        turn_rows.extend(t)
        mull_rows.extend(m)
        result_rows.extend(r)
    rows = mull_rows + turn_rows + result_rows
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "material_v2.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    stats = {"slices": len(slices), "rows": len(rows),
             "games": len({r["game"] for r in mull_rows}),
             "wins": sum(r["result"] for r in mull_rows),
             "mulligan_rows": len(mull_rows), "skip": dict(skip)}
    (out / _META_V2).write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return stats


def load_material_v2(out_dir: Path | str, row: str | None = None) -> list[dict]:
    """v3 素材读取; row 给定时按行类型过滤(turn/mulligan/result)。"""
    path = Path(out_dir) / "material_v2.jsonl"
    if not path.exists():
        raise SystemExit(f"素材不存在: {path}(先运行 python -m trainer material-v2)")
    rows = [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return rows if row is None else [r for r in rows if r.get("row") == row]


def build_material(corpus_dir: Path | str, deck: str, out_dir: Path | str,
                   carddb, battletag: str) -> dict:
    """语料切片 → 素材 jsonl(全量幂等重建)。返回统计。"""
    deck_dir = Path(corpus_dir) / deck
    slices = sorted(deck_dir.glob("*.power.log")) if deck_dir.is_dir() else []
    rows: list[dict] = []
    skip = Counter()
    for path in slices:
        try:
            game_rows = _replay_slice(path, carddb, battletag)
        except Exception as exc:  # noqa: BLE001  坏切片: 计数跳过不中断
            skip[f"重放失败({type(exc).__name__})"] += 1
            continue
        if not game_rows:
            skip["主客未定/无结果"] += 1
        rows.extend(game_rows)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "material.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    stats = {"slices": len(slices), "rows": len(rows),
             "games": len({r["src"].split("#")[0] for r in rows}),
             "wins": sum(r.get("result", 0) for r in rows),
             "skip": dict(skip)}
    (out / _META).write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return stats


def load_material(out_dir: Path | str) -> list[dict]:
    path = Path(out_dir) / "material.jsonl"
    if not path.exists():
        raise SystemExit(f"素材不存在: {path}(先运行 python -m trainer material)")
    return [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]
