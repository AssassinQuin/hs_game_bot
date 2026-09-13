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

from hsbot.adapter import feed_line, new_parser, walk_packets
from hsbot.store import GameStore

from .states import snapshot

ACTION_KINDS = {"play", "attack", "hero_power"}   # 计入"本回合动作"的事件
_META = "_meta.json"


def _replay_slice(path: Path, carddb, battletag: str) -> list[dict]:
    """一个原始切片 → 决策点行(主客未定/未出结果的局整局跳过)。

    时序: 回合开始瞬间拍快照, 之后到下个回合开始之间的事件都是"该回合动作";
    对手回合只冲账不立新行(素材 = 我的决策点)。"""
    parser = new_parser()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        feed_line(parser, line)
    done: list[dict] = []                        # 已收口、待补胜负的行
    out_rows: list[dict] = []
    pend: dict | None = None                     # 当前累积中的我的回合
    actions: list = []
    st_holder: list = []

    def on_event(evt: dict) -> None:
        nonlocal pend, actions
        st = st_holder[0]
        kind = evt.get("kind")
        if kind == "turn_start" and st.friendly_key is not None:
            if pend is not None:                 # 收上一段(我的上一回合)
                pend["actions"] = actions
                done.append(pend)
            actions = []
            snap = snapshot(st, carddb)
            pend = ({"snap": snap, "actions": [],
                     "src": f"{path.name}#g{gi}T{snap['turn']}"}
                    if snap is not None and snap["my_turn"] else None)
        elif kind in ACTION_KINDS and pend is not None:
            cid = evt.get("card_id")
            actions.append({"k": kind, "cid": cid, "cost": carddb.cost(cid)})

    for gi, tree in enumerate(parser.games, 1):
        st = GameStore(carddb=carddb, battletag=battletag, tree=tree,
                       player_manager=parser.player_manager)
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
            done.append(pend)
        result = st.playstate(st.friendly_key)
        if result not in ("WON", "LOST"):
            continue
        for row in done:
            row["result"] = 1 if result == "WON" else 0
        out_rows.extend(done)
    return out_rows


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
