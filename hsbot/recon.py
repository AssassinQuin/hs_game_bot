"""预测-实测对账(spec §5): play() 预测转移 vs store 实测转移的纯函数 diff。

零 IO 纪律: 落盘在 watcher.AuditExporter, 本模块只算不写(summarize 同为
纯函数; main 仅作 CLI 入口读文件)。transition 级对账(非整线终态对比):
每条分歧的定位面 = Piece 字段面 —— mana 组↔cost/减费/回费, face↔segments×sp,
hand_n/hand_cards↔engine/draw_n, engines↔engine, sp↔spellpower_gain。

信号豁免(2026-09-17 审计, 免噪声淹没判据):
- face 仅在"结算目标=敌方英雄"时比较(打随从/解场的牌 face 差不是分歧);
  已知盲区(宁漏勿错备案): 无目标的随机伤害牌(复仇之怒类, target=None)
  打脸的真分歧也被门控吞掉; 过杀时 hp_total 的 max(0,·) 钳制使
  actual_face 低报(敌 5 血真打 8 → 记 5) —— 预先存在, 二轮审计备案;
- 实测手牌多出"已知抽牌池之外"的牌 = 未知抽牌入手(预测模型边界: play 对
  未知抽只计数), **且预测侧手牌须被实测完全包含**, 才豁免 hand_n/hand_cards
  —— 预测侧有实测缺失的牌(抽牌通道断/弃牌效应)恒记真分歧。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections import Counter

from planner.pieces import Piece
from planner.simstate import play


def _hand_multiset(hand) -> Counter:
    return Counter(cid for cid, _cost in hand)


def ir_source_hash(carddb, cid: str) -> str:
    """卡表原始记录的短哈希: 分歧直接定位到编译输入(发现→改规则→
    COMPILER_VERSION+1→重编译→分歧消失 的收敛闭环锚点)。"""
    raw = carddb.raw(cid)
    if raw is None:
        return "missing"
    return hashlib.sha1(json.dumps(raw, sort_keys=True,
                                   ensure_ascii=False).encode("utf-8")
                        ).hexdigest()[:12]


def _pick_hand_index(base, cid, cost, carddb) -> int | None:
    """手牌定位: cost_tag 优先精确匹配; None 时优先卡表基础费副本
    (同牌不同减费多副本防错位), 再兜底首个同 cid。"""
    idxs = [i for i, key in enumerate(base.hand) if key[0] == cid]
    if not idxs:
        return None
    if cost is not None:
        for i in idxs:
            if base.hand[i][1] == cost:
                return i
        return None
    want = carddb.cost(cid)
    if want is not None:
        for i in idxs:
            if base.hand[i][1] == want:
                return i
    return idxs[0]


def reconcile(base, pieces: dict, evt: dict, after, carddb,
              *, face_comparable: bool = True) -> dict | None:
    """(PLAY 块开始基态, play 事件, 块后实测态) → 分歧记录; 一致 → None。

    evt 用 play 事件的 card_id/cost_tag(块开始锁定的真实手牌价); 预测 =
    play(base, 手牌中的该牌); 实测 = after 快照同口径投影。基态里没有该牌
    (装配竞态/衍生牌) → None 不对账。face_comparable=False = 结算目标非
    敌方英雄(watcher 按 PLAY 块 target 解析), face 差不构成分歧。
    """
    cid = evt.get("card_id")
    idx = _pick_hand_index(base, cid, evt.get("cost_tag"), carddb)
    if idx is None:
        return None
    key = base.hand[idx]
    p = pieces.get(key)
    if p is None:
        p = Piece(card_id=key[0], cost=key[1])        # inert: 与 play 同降级
    predicted_face = 0
    try:
        pred = play(base, idx, pieces)
    except ValueError:                               # 预测不可支付但实际打出
        pred = base
        diffs = [{"field": "playable", "predicted": False, "actual": True}]
    else:
        predicted_face = pred.face - base.face
        diffs = []
        # mana 对比双侧按 10 封顶(临时水晶/硬币在实测侧可瞬时 >10, 游戏语义
        # 可用水晶恒 ≤10); diffs 条目与 top-level 一致记原值, 判定用封顶值
        pred_mana, after_mana = min(10, pred.mana), min(10, after.mana)
        if pred_mana != after_mana:
            diffs.append({"field": "mana", "predicted": pred.mana,
                          "actual": after.mana})
        # 未知抽牌豁免(2026-09-17 二轮审计收紧): 前提 = 预测侧手牌**全部**
        # 被实测包含(missing 为空) —— pm−am 非空即已知抽牌通道错/弃牌效应
        # 等真分歧, 不豁免(原实现只查实测侧多牌方向, 吞掉此类真分歧, 且与
        # 本 docstring 的"混合保守记"承诺矛盾); 在此前提下, 实测多出的牌
        # 全部不在已知抽牌池 = 未知抽入手(预测侧只计数), 不构成分歧
        extra = _hand_multiset(after.hand) - _hand_multiset(pred.hand)
        missing = _hand_multiset(pred.hand) - _hand_multiset(after.hand)
        fully_unknown_draw = (bool(extra) and not missing and all(
            c not in {kc for kc, _kc in base.known_draws} for c in extra))
        if not fully_unknown_draw:
            pred_hand_n = len(pred.hand)
            after_hand_n = len(after.hand)
            if pred_hand_n != after_hand_n:
                diffs.append({"field": "hand_n", "predicted": pred_hand_n,
                              "actual": after_hand_n})
            pm, am = _hand_multiset(pred.hand), _hand_multiset(after.hand)
            if pm != am:
                diffs.append({"field": "hand_cards",
                              "predicted": sorted(pm - am),
                              "actual": sorted(am - pm)})
        if pred.engines != after.engines:
            diffs.append({"field": "engines", "predicted": pred.engines,
                          "actual": after.engines})
        if pred.sp != after.sp:
            diffs.append({"field": "sp", "predicted": pred.sp, "actual": after.sp})
    actual_face = (base.enemy_total - after.enemy_total
                   if base.enemy_total is not None
                   and after.enemy_total is not None else None)
    if (face_comparable and actual_face is not None
            and predicted_face != actual_face):
        diffs.append({"field": "face", "predicted": predicted_face,
                      "actual": actual_face})
    if not diffs:
        return None
    piece_d = dataclasses.asdict(p)
    piece_d["card_cats"] = sorted(p.card_cats)       # frozenset 不可 json 化
    return {"ts": round(time.time(), 3), "game_id": evt.get("game_id"),
            "card_id": cid, "piece": piece_d,
            "ir_source_hash": ir_source_hash(carddb, cid),
            "predicted": {"mana": pred.mana, "hand_n": len(pred.hand),
                          "engines": pred.engines, "sp": pred.sp,
                          "face": predicted_face},
            "actual": {"mana": after.mana, "hand_n": len(after.hand),
                       "engines": after.engines, "sp": after.sp,
                       "face": actual_face},
            "diffs": diffs}


def summarize(records) -> dict:
    """对账记录 → 汇总(top 分歧卡 / 分歧字段计数)——消费入口 v1, 不做自动
    改规则(spec §5)。"""
    by_card = Counter(r["card_id"] for r in records)
    by_field = Counter(d["field"] for r in records for d in r["diffs"])
    return {"n": len(records), "by_card": by_card.most_common(),
            "by_field": dict(by_field)}


def main(argv=None) -> int:
    import sys
    from pathlib import Path
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if len(argv) != 1:
        print("用法: python -m hsbot.recon <sim_divergence.jsonl>")
        return 2
    path = Path(argv[0])
    records = [json.loads(ln) for ln in
               path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    s = summarize(records)
    print(f"对账分歧 {s['n']} 条 ({path.name})")
    print("字段分布: " + (", ".join(f"{k}×{v}" for k, v in s["by_field"].items())
                        or "无"))
    for cid, n in s["by_card"][:10]:
        print(f"  {cid} ×{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
